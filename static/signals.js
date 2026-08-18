/*
  Technical Signals Layer widget — docs/technical-signals-layer-scope.md §9.

  §9's element list, and where each is implemented below:

    Header: title, equity/crypto regime chips, last-run stamp, ticker search
        → renderHeader(), renderRegimeChips(), bindSearch()
    Intake strip (only visible when pending), Approve/Review/Dismiss, paste
        → renderIntake(), bindPaste()                          (§7.4, §7.5)
    Ranked watchlist table: ticker · class+source · entry score as segmented
    component bar · band badge · signal badges · exit score (held) · 90-day
    sparkline · days since last signal
        → renderTable(), componentBar(), sparkline()
    Row expand / lookup card: component breakdown, mini chart 21E/50/200,
    "Firing now" list, signal history, data-freshness line, Open in TradingView
        → renderCard()                                         (§6.3, §9)
    Filters: asset class, band, held-only, source
        → applyFilters()

  Matches the conventions in static/steno.js: IIFE, a single `state` object,
  `fetchJSON` helper, delegated event handlers, no framework and no build step.

  Charts are hand-rolled inline SVG. The dashboard has no charting dependency
  and §2 rules out adding integrations for v1; three moving averages over 90
  closes is well within what a 40-line SVG helper handles.
*/
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    watchlist: [],
    intake: [],
    sources: [],
    status: null,
    lookup: null,
    expanded: null,  // symbol whose row is expanded
    reviewing: null, // §7.4: intake id currently open in the Review panel
    // Caches of per-symbol detail and price bars, so collapsing and re-expanding
    // a row doesn't re-fetch. Both cleared on refresh().
    details: {},
    bars: {},
    filters: { asset: "", band: "", source: "", heldOnly: false },
  };

  // §5 band order, weakest→strongest. Mirrors config.BAND_ORDER server-side.
  const BAND_ORDER = ["No signal", "Watch", "Strong", "Prime"];

  document.addEventListener("DOMContentLoaded", () => {
    bindSearch();
    bindFilters();
    bindPaste();
    bindTableEvents();
    bindRefresh();
    const activeNav = document.querySelector(".djg-nav a.active");
    if (activeNav && activeNav.scrollIntoView) {
      activeNav.scrollIntoView({ behavior: "instant", inline: "center", block: "nearest" });
    }
    boot().catch((err) => {
      console.error("Signals boot failed", err);
      toast("Failed to load signals. Check console.");
    });
  });

  async function boot() {
    await refresh();
    // Scores only change once a day (§3.3 nightly run), so a 5-minute poll is
    // ample and matches the cadence the other dashboard pages use.
    setInterval(() => refresh().catch(console.error), 5 * 60 * 1000);
  }

  async function refresh() {
    const [watchlist, status, sources, intake] = await Promise.all([
      fetchJSON("/api/signals/watchlist"),
      fetchJSON("/api/signals/status").catch(() => null),
      fetchJSON("/api/signals/sources").catch(() => ({ sources: [] })),
      // §7 intake is a later build chunk; tolerate its absence so the widget
      // works before that endpoint exists.
      fetchJSON("/api/signals/intake/pending").catch(() => ({ pending: [], silent_sources: [] })),
    ]);
    state.watchlist = watchlist.watchlist || [];
    state.status = status;
    if (status && typeof status.autocomplete === "boolean") state.autocomplete = status.autocomplete;
    state.lastScoredBar = watchlist.last_scored_bar;
    state.staleCount = watchlist.stale_count || 0;
    state.sources = sources.sources || [];
    state.intake = intake.pending || [];
    state.silentSources = intake.silent_sources || [];
    state.details = {};
    state.bars = {};
    render();
  }

  function render() {
    renderHeader();
    renderRegimeChips();
    renderIntake();
    renderSilence();
    renderFilterOptions();
    renderTable();
  }

  // ── Header (§9) ──────────────────────────────────────────────────────────

  // §3.1: true until the reference-chart reconciliation signs off. Drives the
  // "?" on TD badges and the caveat line under the component breakdown.
  function tdUnverified() {
    return !!(state.status && state.status.td_counts_verified === false);
  }

  function renderHeader() {
    $("[data-last-run]").textContent = state.lastScoredBar || "—";

    // §3.3: staleness must be visible on the dashboard, not just in logs.
    const notice = $("[data-stale-notice]");
    if (state.staleCount > 0) {
      notice.textContent = `⚠ ${state.staleCount} on stale data`;
      notice.hidden = false;
    } else {
      notice.hidden = true;
    }

    // §6.1: the affordance matches what the search provider can actually do,
    // rather than offering a dropdown that can never populate. Seeded from
    // /status on first paint, then kept current by each search response.
    const hint = $("[data-search-hint]");
    hint.textContent = state.autocomplete
      ? "Type to search · ↵ to look up"
      : "Exact symbol · autocomplete needs a Twelve Data key";
  }

  function renderRegimeChips() {
    // §9: "equity/crypto regime chips". Aggregated from the scored watchlist by
    // asset class — the modal regime among that class's names. This is a summary
    // of the names we track, not an independent index-level regime call; with a
    // small watchlist it can be dominated by a few names, which is why the chip
    // shows the sample size.
    const host = $("[data-regime-chips]");
    const byClass = {};
    state.watchlist.forEach((row) => {
      if (!row.regime) return;
      byClass[row.asset_class] = byClass[row.asset_class] || {};
      byClass[row.asset_class][row.regime] = (byClass[row.asset_class][row.regime] || 0) + 1;
    });

    const chips = Object.keys(byClass).sort().map((cls) => {
      const counts = byClass[cls];
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      const modal = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
      return `<span class="sig-chip" data-regime="${esc(modal)}">${esc(cls)} ${esc(modal)} <em>${counts[modal]}/${total}</em></span>`;
    });

    host.innerHTML = chips.length ? chips.join("") : `<span class="sig-chip">No scored names yet</span>`;
  }

  // ── Intake strip (§7.4) ──────────────────────────────────────────────────

  function renderIntake() {
    const strip = $("[data-intake-strip]");
    // §9: "Intake strip (only visible when pending)".
    if (!state.intake.length) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    $("[data-intake-list]").innerHTML = state.intake.map((item) => {
      const reviewing = state.reviewing === item.id;
      return `
        <div class="sig-intake-item" data-intake-id="${item.id}">
          <div>
            <span class="sig-intake-source">${esc(item.source_tag)}</span>
            <span class="sig-intake-summary">${esc(item.subject || "note")} · ${esc(formatIntakeDate(item.published_date))} — ${actionSummary(item.actions)}</span>
          </div>
          <div class="sig-intake-actions">
            <button class="sig-btn sig-btn-primary" data-approve="${item.id}">Approve all</button>
            <button class="sig-btn sig-btn-ghost" data-review="${item.id}">${reviewing ? "Close" : "Review"}</button>
            <button class="sig-btn sig-btn-ghost sig-btn-danger" data-dismiss="${item.id}">Dismiss</button>
          </div>
        </div>
        ${reviewing ? reviewPanel(item) : ""}`;
    }).join("");
  }

  // §7.4's example renders as "+HPQ, +S, trim ONDS" — the verb is spelled out
  // for trim/exit/reiterate rather than symbolised, because "~ONDS" does not
  // read as anything and this line is the whole basis for a one-tap approval.
  function actionSummary(actions) {
    return (actions || []).map((a) => {
      if (a.action === "add") return `+${esc(a.ticker)}`;
      return `${esc(a.action)} ${esc(a.ticker)}`;
    }).join(", ");
  }

  // §7.4's example uses "26 Jul" rather than an ISO date.
  function formatIntakeDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso + "T00:00:00Z");
    if (isNaN(d)) return iso;
    return `${d.getUTCDate()} ${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getUTCMonth()]}`;
  }

  // §7.4 "[Review]" — inspect the extracted names before approving, and approve
  // a subset. This is the check on LLM extraction errors that §7.4 exists for:
  // "Approve all" is the fast path, Review is the one that keeps a wrong ticker
  // out without discarding the whole note.
  function reviewPanel(item) {
    const rows = (item.actions || []).map((a) => `
      <label class="sig-review-row">
        <input type="checkbox" data-review-ticker="${esc(a.ticker)}" checked />
        <span class="sig-review-verb" data-action="${esc(a.action)}">${esc(a.action)}</span>
        <span class="sig-review-sym">${esc(a.ticker)}</span>
        <span class="sig-review-note">${esc(a.instrument_note || "")}</span>
        ${a.is_removal_suggestion ? `<span class="sig-sigbadge" data-kind="ct">on watchlist — removal</span>` : ""}
        ${a.on_watchlist && !a.is_removal_suggestion ? `<span class="sig-sigbadge" data-kind="held">already tracked</span>` : ""}
      </label>`).join("");
    return `
      <div class="sig-review" data-review-panel="${item.id}">
        ${rows || `<span class="sig-firing-empty">No actions extracted.</span>`}
        <div class="sig-paste-actions">
          <button class="sig-btn sig-btn-primary" data-approve-selected="${item.id}">Approve selected</button>
          <span class="sig-search-hint">Unticked names are skipped; the note is resolved either way.</span>
        </div>
      </div>`;
  }

  function renderSilence() {
    // §7: "if no email arrives from a source for 21 days, the dashboard shows a
    // quiet 'no Steno note received in 3 weeks' notice". Deliberately understated
    // styling — the scope doc says "without nagging".
    const host = $("[data-silence-notice]");
    if (!state.silentSources || !state.silentSources.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    host.innerHTML = state.silentSources
      .map((s) => `No ${esc(s.label || s.tag)} note received in ${Math.floor(s.days_silent / 7)} weeks`)
      .join(" · ");
  }

  function bindPaste() {
    // §7.5: "Manual intake fallback: a 'Paste a note' box".
    const box = $("[data-paste-box]");
    $("[data-paste-toggle]").addEventListener("click", () => {
      box.hidden = !box.hidden;
      if (!box.hidden) $("[data-paste-input]").focus();
    });
    $("[data-paste-cancel]").addEventListener("click", () => { box.hidden = true; });
    // §7.5 PDF upload — same extraction, same queue as pasted text.
    $("[data-pdf-input]").addEventListener("change", async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const source = $("[data-paste-source]").value;
      const form = new FormData();
      form.append("file", file);
      toast(`Extracting ${file.name}…`);
      try {
        const result = await fetchJSON(
          `/api/signals/intake/upload?source_tag=${encodeURIComponent(source)}`,
          { method: "POST", body: form },
        );
        box.hidden = true;
        toast(
          result.status === "queued"
            ? `Extracted ${file.name} — check the review queue.`
            : `${file.name}: ${result.reason || result.status}`,
        );
        await refresh();
      } catch (err) {
        console.error(err);
        toast("PDF extraction failed. Check console.");
      } finally {
        event.target.value = "";   // allow re-selecting the same file
      }
    });

    $("[data-paste-submit]").addEventListener("click", async () => {
      const text = $("[data-paste-input]").value.trim();
      if (!text) { toast("Paste some note text first."); return; }
      const source = $("[data-paste-source]").value;
      try {
        await fetchJSON(`/api/signals/intake/paste?source_tag=${encodeURIComponent(source)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        $("[data-paste-input]").value = "";
        box.hidden = true;
        toast("Note extracted — check the review queue.");
        await refresh();
      } catch (err) {
        console.error(err);
        toast("Extraction failed. Check console.");
      }
    });
  }

  // ── Filters (§9) ─────────────────────────────────────────────────────────

  function renderFilterOptions() {
    const assetSel = $("[data-filter-asset]");
    const classes = [...new Set(state.watchlist.map((r) => r.asset_class))].sort();
    setOptions(assetSel, classes, "All assets", state.filters.asset);

    const sourceSel = $("[data-filter-source]");
    const tags = state.sources.length
      ? state.sources
      : [...new Set(state.watchlist.map((r) => r.source_tag))].sort();
    setOptions(sourceSel, tags, "All sources", state.filters.source);
  }

  function setOptions(select, values, allLabel, current) {
    select.innerHTML =
      `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    select.value = current || "";
  }

  function bindFilters() {
    $("[data-filter-asset]").addEventListener("change", (e) => { state.filters.asset = e.target.value; renderTable(); });
    $("[data-filter-band]").addEventListener("change", (e) => { state.filters.band = e.target.value; renderTable(); });
    $("[data-filter-source]").addEventListener("change", (e) => { state.filters.source = e.target.value; renderTable(); });
    $("[data-filter-held]").addEventListener("change", (e) => { state.filters.heldOnly = e.target.checked; renderTable(); });
  }

  function applyFilters(rows) {
    const f = state.filters;
    return rows.filter((r) => {
      if (f.asset && r.asset_class !== f.asset) return false;
      if (f.band && r.entry_band !== f.band) return false;
      if (f.source && r.source_tag !== f.source) return false;
      if (f.heldOnly && !r.held) return false;
      return true;
    });
  }

  function bindRefresh() {
    $("[data-refresh]").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Scoring…";
      try {
        await fetchJSON("/api/signals/run", { method: "POST" });
        await refresh();
        toast("Scoring complete.");
      } catch (err) {
        console.error(err);
        toast("Scoring failed. Check console.");
      } finally {
        btn.disabled = false;
        btn.textContent = "Re-run scoring";
      }
    });
  }

  // ── Ranked table (§9) ────────────────────────────────────────────────────

  function renderTable() {
    const rows = applyFilters(state.watchlist);
    const body = $("[data-watchlist-body]");
    $("[data-watchlist-count]").textContent = `${rows.length} name${rows.length === 1 ? "" : "s"}`;
    $("[data-watchlist-empty]").hidden = rows.length > 0 || state.watchlist.length > 0;

    body.innerHTML = rows.map((row) => {
      const expanded = state.expanded === row.symbol;
      return rowHtml(row) + (expanded ? expandHtml(row) : "");
    }).join("");
  }

  function rowHtml(row) {
    const band = row.entry_band || "No signal";
    const entry = row.entry_score;
    const exitWarn = (row.exit_score || 0) >= 70;

    return `
      <div class="sig-row sig-row-body"
           data-symbol="${esc(row.symbol)}"
           data-band="${esc(band)}"
           data-counter-trend="${!!row.counter_trend}"
           data-stale="${!!row.stale}">
        <span class="sig-col-sym">${esc(row.symbol)}</span>
        <span class="sig-col-tags">${esc(row.asset_class || "—")} · ${esc(row.source_tag || "—")}</span>
        <span class="sig-col-bar">${componentBar(row)}</span>
        <span class="sig-col-band"><span class="sig-badge" data-band="${esc(band)}">${esc(band)}</span></span>
        <span class="sig-col-badges">${signalBadges(row)}</span>
        <span class="sig-col-exit" data-warn="${exitWarn}">${row.held && row.exit_score != null ? row.exit_score : "—"}</span>
        <span class="sig-col-spark">${sparkline(row.sparkline || [])}</span>
        <span class="sig-col-age">${row.days_since_last_signal == null ? "—" : row.days_since_last_signal + "d"}</span>
      </div>`;
  }

  // §9: "entry score as segmented component bar". One segment per §5 component,
  // segment width proportional to that component's max (25/25/20/15/10/5), fill
  // proportional to points actually scored. This makes *why* a score is what it
  // is legible at a glance from the table, without expanding the row.
  const COMPONENT_MAXES = [
    ["td_sequential", 25],
    ["regime", 25],
    ["stretch", 20],
    ["trend", 15],
    ["rsi", 10],
    ["volume", 5],
  ];

  function componentBar(row) {
    if (row.entry_score == null) {
      return `<span class="sig-bar"><span class="sig-bar-track"></span><span class="sig-bar-score">—</span></span>`;
    }
    // Component points come from the ranked-table payload itself (the persisted
    // score's stored breakdown), so every row's bar is filled on first paint.
    // Deriving them from an expand-time fetch instead left un-expanded rows
    // looking like they had no data.
    const comps = row.components || {};

    const segs = COMPONENT_MAXES.map(([key, max]) => {
      const scored = comps[key];
      const pct = scored == null ? 0 : Math.max(0, Math.min(100, (scored / max) * 100));
      return `<span class="sig-bar-seg" style="flex:${max}" title="${esc(key)} ${scored == null ? "" : fmt(scored)}/${max}">
                <span class="sig-bar-seg-fill" style="width:${pct}%"></span>
              </span>`;
    }).join("");

    return `<span class="sig-bar">
              <span class="sig-bar-track">${segs}</span>
              <span class="sig-bar-score">${row.entry_score}</span>
            </span>`;
  }

  function signalBadges(row) {
    // §9: "signal badges". Derived from the drivers list the scoring layer
    // already produced, so the badges can never disagree with the breakdown.
    const badges = [];
    const drivers = (row.drivers || []).join(" ");
    // §3.1's acceptance test has not run, so TD counts carry a "?" until
    // sign-off — Fred should check the chart rather than trust the badge.
    // See docs/td-sequential-verification.md.
    const q = tdUnverified() ? "?" : "";
    if (/TD\s*(buy|sell)?\s*countdown 13|countdown 13/i.test(drivers)) badges.push(["td", `TD13${q}`]);
    else if (/setup 9/i.test(drivers)) badges.push(["td", `TD9${q}`]);
    if (/flip/i.test(drivers)) badges.push(["regime", "FLIP"]);
    if (row.counter_trend) badges.push(["ct", "CTR-TREND"]);
    if (row.held) badges.push(["held", "HELD"]);
    if (row.stale) badges.push(["stale", "STALE"]);
    return badges.map(([kind, label]) => `<span class="sig-sigbadge" data-kind="${kind}">${label}</span>`).join("");
  }

  // §9: "90-day sparkline" of the entry score. Fixed 0-100 y-domain rather than
  // auto-scaled: the score IS a 0-100 quantity, and auto-scaling would make a
  // flat 40-45 series look as dramatic as a 10-90 move.
  function sparkline(values) {
    if (!values || values.length < 2) return `<span class="sig-col-age">—</span>`;
    const w = 90, h = 22, pad = 2;
    const pts = values.map((v, i) => {
      const x = pad + (i / (values.length - 1)) * (w - pad * 2);
      const y = h - pad - (Math.max(0, Math.min(100, v)) / 100) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    const last = values[values.length - 1];
    const stroke = last >= 85 ? "var(--sig-prime)" : last >= 70 ? "var(--sig-strong)" : last >= 50 ? "var(--sig-watch)" : "var(--sig-none)";
    return `<svg class="sig-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
              <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.2" />
            </svg>`;
  }

  // ── Row expand (§9) ──────────────────────────────────────────────────────

  function bindTableEvents() {
    $("[data-watchlist-table]").addEventListener("click", async (event) => {
      const approve = event.target.closest("[data-approve]");
      const dismiss = event.target.closest("[data-dismiss]");
      if (approve || dismiss) return; // intake buttons live outside the table

      const action = event.target.closest("[data-action]");
      if (action) {
        event.stopPropagation();
        await handleRowAction(action.dataset.action, action.dataset.symbol);
        return;
      }

      const row = event.target.closest(".sig-row-body");
      if (!row) return;
      const symbol = row.dataset.symbol;
      state.expanded = state.expanded === symbol ? null : symbol;
      renderTable();
      if (state.expanded) await loadDetail(symbol);
    });

    // Intake actions are delegated from the strip, not the table.
    $("[data-intake-strip]").addEventListener("click", async (event) => {
      // §7.4 [Review] — local toggle, no request.
      const review = event.target.closest("[data-review]");
      if (review) {
        const id = Number(review.dataset.review);
        state.reviewing = state.reviewing === id ? null : id;
        renderIntake();
        return;
      }

      // §7.4 Review → "Approve selected": a subset of the note's names.
      const selected = event.target.closest("[data-approve-selected]");
      if (selected) {
        const id = selected.dataset.approveSelected;
        const panel = document.querySelector(`[data-review-panel="${id}"]`);
        const tickers = Array.from(panel.querySelectorAll("[data-review-ticker]"))
          .filter((cb) => cb.checked)
          .map((cb) => cb.dataset.reviewTicker);
        if (!tickers.length) { toast("Nothing selected — dismiss the note instead."); return; }
        try {
          await fetchJSON(
            `/api/signals/intake/${id}/approve?tickers=${encodeURIComponent(tickers.join(","))}`,
            { method: "POST" },
          );
          state.reviewing = null;
          toast(`Approved ${tickers.length} name${tickers.length === 1 ? "" : "s"}.`);
          await refresh();
        } catch (err) {
          console.error(err);
          toast("Approve failed. Check console.");
        }
        return;
      }

      const approve = event.target.closest("[data-approve]");
      const dismiss = event.target.closest("[data-dismiss]");
      if (!approve && !dismiss) return;
      const id = (approve || dismiss).dataset.approve || (approve || dismiss).dataset.dismiss;
      const verb = approve ? "approve" : "dismiss";
      try {
        await fetchJSON(`/api/signals/intake/${id}/${verb}`, { method: "POST" });
        state.reviewing = null;
        toast(approve ? "Approved — added to watchlist." : "Dismissed.");
        await refresh();
      } catch (err) {
        console.error(err);
        toast("Action failed. Check console.");
      }
    });
  }

  async function handleRowAction(action, symbol) {
    try {
      if (action === "relookup") {
        // Explicit opt-in to a live recompute (§6), surfaced in the lookup card
        // so it sits visibly apart from the persisted row score rather than
        // overwriting it.
        $("[data-search-input]").value = symbol;
        state.forceFresh = true;   // §3.3 cache bypass — "live" must mean live
        $("[data-search-form]").requestSubmit();
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      if (action === "hold") {
        const row = state.watchlist.find((r) => r.symbol === symbol);
        await fetchJSON(`/api/signals/watchlist/${symbol}/held?held=${!row.held}`, { method: "POST" });
      } else if (action === "remove") {
        if (!window.confirm(`Remove ${symbol} from the watchlist?`)) return;
        await fetchJSON(`/api/signals/watchlist/${symbol}`, { method: "DELETE" });
      }
      await refresh();
    } catch (err) {
      console.error(err);
      toast("Action failed. Check console.");
    }
  }

  async function loadDetail(symbol) {
    if (state.details[symbol]) { renderTable(); return; }
    try {
      // §9 row-expand reads the PERSISTED score, not a live recompute. See the
      // /detail endpoint docstring: recomputing on expand can differ from the
      // row's own number by a few points once the session's volume moves, and
      // showing two different scores for one name destroys trust in the rest.
      state.details[symbol] = { detail: await fetchJSON(`/api/signals/detail/${encodeURIComponent(symbol)}`) };
    } catch (err) {
      console.error("Detail load failed", err);
      state.details[symbol] = { error: String(err) };
    }
    renderTable();
    // Bars are a second request so the breakdown paints immediately rather than
    // waiting on the chart; the chart slot fills in place when it arrives.
    await loadBars(symbol);
  }

  async function loadBars(symbol) {
    if (state.bars[symbol]) { paintChart(symbol); return; }
    try {
      state.bars[symbol] = await fetchJSON(`/api/signals/bars/${encodeURIComponent(symbol)}?limit=120`);
    } catch (err) {
      console.error("Bars load failed", err);
      state.bars[symbol] = { error: "no price history" };
    }
    paintChart(symbol);
  }

  function paintChart(symbol) {
    // Targeted DOM update rather than a full renderTable(): re-rendering would
    // rebuild the expanded row and lose scroll position for no benefit.
    const host = document.querySelector(`[data-price-chart="${cssEscape(symbol)}"]`);
    if (host) host.innerHTML = priceChart(state.bars[symbol]);
  }

  function cssEscape(value) {
    // Symbols contain '.', '-', '=' and '^' (BRK.B, BTC-USD, GC=F, ^GSPC), all of
    // which are meaningful in a selector.
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : String(value).replace(/[^\w-]/g, "\\$&");
  }

  function expandHtml(row) {
    const cached = state.details[row.symbol];
    if (!cached) return `<div class="sig-expand"><span class="sig-firing-empty">Loading…</span></div>`;
    if (cached.error) {
      const notScored = /404/.test(cached.error);
      return `<div class="sig-expand"><span class="sig-${notScored ? "firing-empty" : "warnings"}">${
        notScored
          ? "Not scored yet — re-run scoring, or look the ticker up above for a live score."
          : esc(cached.error)
      }</span></div>`;
    }
    return `<div class="sig-expand">${persistedCard(cached.detail, row)}</div>`;
  }

  // §9 row-expand card, rendered from the persisted score.
  function persistedCard(detail, row) {
    const score = detail.score || {};
    const comps = (score.components && score.components.entry) || [];
    const drivers = (score.drivers && score.drivers.entry) || [];

    return `
      <div class="sig-card-head">
        <div>
          <span class="sig-card-sym">${esc(row.symbol)}</span>
          <span class="sig-badge" data-band="${esc(score.entry_band || "No signal")}">${esc(score.entry_band || "No signal")}</span>
          ${score.counter_trend ? `<span class="sig-sigbadge" data-kind="ct">counter-trend</span>` : ""}
        </div>
        <div class="sig-card-scores">
          <span>Entry <strong>${score.entry_score}</strong></span>
          <span>Exit <strong>${score.exit_score}</strong></span>
          <span>Regime <strong>${esc(score.regime || "—")}</strong></span>
        </div>
        <div class="sig-card-actions">
          <button class="sig-btn sig-btn-ghost" data-action="hold" data-symbol="${esc(row.symbol)}">${row.held ? "Unmark held" : "Mark held"}</button>
          <button class="sig-btn sig-btn-ghost" data-action="relookup" data-symbol="${esc(row.symbol)}">Live re-score</button>
          <button class="sig-btn sig-btn-ghost sig-btn-danger" data-action="remove" data-symbol="${esc(row.symbol)}">Remove</button>
          <a class="sig-btn sig-btn-ghost" href="${esc(detail.tradingview_url)}" target="_blank" rel="noopener">Open in TradingView</a>
        </div>
      </div>

      <div class="sig-card-grid">
        <div>
          <p class="sig-subhead">COMPONENT BREAKDOWN · ENTRY</p>
          ${comps.map(componentRow).join("")}
          <p class="sig-subhead" style="margin-top:0.9rem">FIRING NOW</p>
          ${firingList(drivers)}
        </div>
        <div>
          <p class="sig-subhead">PRICE · 21 EMA · 50 · 200</p>
          <div data-price-chart="${esc(row.symbol)}">${priceChart(state.bars[row.symbol])}</div>
          <p class="sig-subhead" style="margin-top:0.9rem">ENTRY SCORE · 90 DAYS</p>
          ${scoreChart(detail.scores || [])}
          <p class="sig-subhead" style="margin-top:0.9rem">SIGNAL HISTORY</p>
          ${historyList({ signals: detail.signals })}
        </div>
      </div>

      <div class="sig-freshness" data-stale="${!!score.stale}">
        Scored on ${esc(score.bar_date)}${score.stale ? " · STALE — alerts suppressed" : ""}
      </div>`;
  }

  // ── Card body — shared by row-expand (§9) and lookup (§6.3) ──────────────

  function cardBody(detail, row) {
    const ev = detail.lookup;
    const entry = ev.entry;
    const exit = ev.exit;
    const daily = (ev.indicators && ev.indicators.daily) || {};

    return `
      <div class="sig-card-head">
        <div>
          <span class="sig-card-sym">${esc(ev.symbol)}</span>
          <span class="sig-badge" data-band="${esc(entry.band)}">${esc(entry.band)}</span>
          ${entry.badge ? `<span class="sig-sigbadge" data-kind="ct">${esc(entry.badge)}</span>` : ""}
        </div>
        <div class="sig-card-scores">
          <span>Entry <strong>${entry.score}</strong></span>
          <span>Exit <strong>${exit.score}</strong></span>
          <span>Regime <strong>${esc(ev.regime || "—")}</strong></span>
        </div>
        <div class="sig-card-actions">
          ${row ? `
            <button class="sig-btn sig-btn-ghost" data-action="hold" data-symbol="${esc(ev.symbol)}">${row.held ? "Unmark held" : "Mark held"}</button>
            <button class="sig-btn sig-btn-ghost sig-btn-danger" data-action="remove" data-symbol="${esc(ev.symbol)}">Remove</button>
          ` : `
            <select class="sig-select" data-add-source>
              <option value="OWN">OWN</option>
              <option value="STENO">STENO</option>
              <option value="RV">RV</option>
              <option value="COWAN">COWAN</option>
            </select>
            <button class="sig-btn sig-btn-primary" data-add-symbol="${esc(ev.symbol)}">Add to watchlist</button>
          `}
          <a class="sig-btn sig-btn-ghost" href="${esc(ev.tradingview_url)}" target="_blank" rel="noopener">Open in TradingView</a>
        </div>
      </div>

      <div class="sig-card-grid">
        <div>
          <p class="sig-subhead">COMPONENT BREAKDOWN · ENTRY</p>
          ${(entry.components || []).map(componentRow).join("")}
          <p class="sig-subhead" style="margin-top:0.9rem">FIRING NOW</p>
          ${firingList(entry.drivers)}
        </div>
        <div>
          <p class="sig-subhead">PRICE · 21 EMA · 50 · 200</p>
          <div data-price-chart="${esc(ev.symbol)}">${priceChart(state.bars[ev.symbol])}</div>
          <p class="sig-subhead" style="margin-top:0.9rem">SIGNAL HISTORY</p>
          ${historyList(detail.history)}
        </div>
      </div>

      ${freshnessLine(ev, daily)}
      ${(ev.warnings || []).length ? `<div class="sig-warnings">${ev.warnings.map((w) => `<div>⚠ ${esc(w)}</div>`).join("")}</div>` : ""}
    `;
  }

  function componentRow(c) {
    const pct = c.max_points ? Math.max(0, Math.min(100, (c.points / c.max_points) * 100)) : 0;
    const scored = c.points > 0;
    // Only the TD row carries the §3.1 caveat — the other five components are
    // computed from published formulas with nothing to reconcile against.
    const caveat = c.key === "td_sequential" && tdUnverified() && scored
      ? `<div class="sig-comp-detail sig-unverified">Counts not yet reconciled against the TradingView indicator — confirm on the chart.</div>`
      : "";
    return `
      <div class="sig-comp">
        <span class="sig-comp-label">${esc(c.label)}${c.key === "td_sequential" && tdUnverified() ? " <span class='sig-unverified-mark' title='Not yet reconciled against TradingView'>?</span>" : ""}</span>
        <span class="sig-comp-track"><span class="sig-comp-fill" data-scored="${scored}" style="width:${pct}%"></span></span>
        <span class="sig-comp-num">${fmt(c.points)}/${c.max_points}</span>
      </div>
      <div class="sig-comp-detail">${esc(c.detail)}</div>${caveat}`;
  }

  function firingList(drivers) {
    // §6.3: the "Firing now" list, in plain terms. The scoring layer only puts
    // components that actually scored into `drivers`, so an empty list genuinely
    // means nothing is firing.
    if (!drivers || !drivers.length) return `<span class="sig-firing-empty">Nothing firing.</span>`;
    return `<ul class="sig-firing">${drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>`;
  }

  function historyList(history) {
    const signals = (history && history.signals) || [];
    if (!signals.length) return `<span class="sig-firing-empty">No recorded signals yet.</span>`;
    return `<div class="sig-history">${signals.slice(0, 8).map((s) => `
      <div class="sig-history-row">
        <span>${esc(s.bar_date)}</span>
        <span>${esc(s.event_type.replace(/_/g, " "))}</span>
        <span>${esc(s.direction)}</span>
        <span>${esc(s.timeframe)}</span>
      </div>`).join("")}</div>`;
  }


  // §6.3 / §9: "mini chart with 21E/50/200".
  //
  // Four aligned series from /api/signals/bars. Unlike the score charts, the
  // y-domain is auto-scaled to the union of all four series — price has no
  // natural domain, and clipping a 200 SMA that sits below the visible window
  // would silently misrepresent the trend structure the chart exists to show.
  function priceChart(bars) {
    if (!bars) return `<span class="sig-firing-empty">Loading chart…</span>`;
    if (bars.error) return `<span class="sig-firing-empty">Chart unavailable (${esc(bars.error)}).</span>`;
    const rows = bars.bars || [];
    if (rows.length < 2) return `<span class="sig-firing-empty">Not enough bars to chart.</span>`;

    const w = 300, h = 90, pad = 6;
    const series = [
      { key: "close", color: "var(--djg-text)", width: 1.3 },
      { key: "ema21", color: "var(--djg-accent)", width: 1 },
      { key: "sma50", color: "var(--djg-info)", width: 1 },
      { key: "sma200", color: "var(--djg-neg)", width: 1 },
    ];

    // Shared domain across every series that has data, so the lines are
    // comparable — scaling each independently would make crossovers meaningless.
    const all = [];
    series.forEach((s) => rows.forEach((r) => { if (r[s.key] != null) all.push(r[s.key]); }));
    if (!all.length) return `<span class="sig-firing-empty">No chartable data.</span>`;
    const lo = Math.min(...all), hi = Math.max(...all);
    const span = hi - lo || 1;

    const x = (i) => pad + (i / (rows.length - 1)) * (w - pad * 2);
    const y = (v) => h - pad - ((v - lo) / span) * (h - pad * 2);

    const paths = series.map((s) => {
      // Break the polyline where the average is not yet defined rather than
      // interpolating across the warmup gap — a straight line into bar 200 of a
      // 200 SMA would draw a trend that does not exist.
      const segments = [];
      let current = [];
      rows.forEach((r, i) => {
        if (r[s.key] == null) {
          if (current.length > 1) segments.push(current);
          current = [];
        } else {
          current.push(`${x(i).toFixed(1)},${y(r[s.key]).toFixed(1)}`);
        }
      });
      if (current.length > 1) segments.push(current);
      return segments
        .map((pts) => `<polyline points="${pts.join(" ")}" fill="none" stroke="${s.color}" stroke-width="${s.width}" />`)
        .join("");
    }).join("");

    const last = rows[rows.length - 1];
    return `
      <svg class="sig-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Price with 21 EMA, 50 and 200 SMA">
        ${paths}
      </svg>
      <div class="sig-chart-legend">
        <span class="sig-legend-price">close ${fmt(last.close)}</span>
        <span class="sig-legend-ema">21E ${fmt(last.ema21)}</span>
        <span class="sig-legend-50">50 ${fmt(last.sma50)}</span>
        <span class="sig-legend-200">200 ${fmt(last.sma200)}</span>
      </div>`;
  }

  // Entry-score history chart for the §9 row-expand card, with the §5 band
  // thresholds drawn as reference lines. Fixed 0-100 y-domain for the same
  // reason as the sparkline: the score IS a 0-100 quantity.
  function scoreChart(scoreRows) {
    const scores = (scoreRows || []).map((s) => s.entry_score).filter((v) => v != null);
    if (scores.length < 2) {
      return `<span class="sig-firing-empty">Only ${scores.length} scored day${scores.length === 1 ? "" : "s"} so far — the chart fills in as the nightly run accumulates history.</span>`;
    }
    const w = 300, h = 90, pad = 6;
    const pts = scores.map((v, i) => {
      const x = pad + (i / (scores.length - 1)) * (w - pad * 2);
      const y = h - pad - (Math.max(0, Math.min(100, v)) / 100) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    const bandLine = (score, color) => {
      const y = h - pad - (score / 100) * (h - pad * 2);
      return `<line x1="${pad}" y1="${y}" x2="${w - pad}" y2="${y}" stroke="${color}" stroke-width="0.5" stroke-dasharray="2 3" opacity="0.5" />`;
    };
    return `
      <svg class="sig-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Entry score history">
        ${bandLine(85, "var(--sig-prime)")}
        ${bandLine(70, "var(--sig-strong)")}
        ${bandLine(50, "var(--sig-watch)")}
        <polyline points="${pts}" fill="none" stroke="var(--djg-text)" stroke-width="1.3" />
      </svg>
      <div class="sig-chart-legend">
        <span class="sig-legend-price">entry score</span>
        <span class="sig-legend-ema">85 Prime</span>
        <span class="sig-legend-50">70 Strong</span>
        <span class="sig-legend-200">50 Watch</span>
      </div>`;
  }

  function freshnessLine(ev, daily) {
    // §9: "data-freshness line".
    const stale = !!ev.stale;
    const bits = [`Last bar ${esc(ev.bar_date)}`, `${ev.bars_loaded} bars loaded`];
    if (ev.thin_history) bits.push("thin history");
    if (stale) bits.push("STALE — alerts suppressed");
    return `<div class="sig-freshness" data-stale="${stale}">${bits.join(" · ")}</div>`;
  }

  // ── Lookup (§6) ──────────────────────────────────────────────────────────

  function bindSearch() {
    // §6.1 autocomplete. Debounced so a fast typist costs one request, not ten —
    // Twelve Data's basic plan allows 8/minute.
    let suggestTimer = null;
    const input = $("[data-search-input]");
    input.addEventListener("input", () => {
      clearTimeout(suggestTimer);
      const q = input.value.trim();
      if (q.length < 2) { renderSuggestions([]); return; }
      suggestTimer = setTimeout(async () => {
        try {
          const body = await fetchJSON(`/api/signals/search?q=${encodeURIComponent(q)}&limit=6`);
          state.autocomplete = body.autocomplete;
          renderSuggestions(body.autocomplete ? body.results : []);
        } catch (_) { renderSuggestions([]); }
      }, 250);
    });
    input.addEventListener("blur", () => setTimeout(() => renderSuggestions([]), 150));

    $("[data-suggestions]").addEventListener("mousedown", (event) => {
      const item = event.target.closest("[data-suggest-symbol]");
      if (!item) return;
      input.value = item.dataset.suggestSymbol;
      renderSuggestions([]);
      $("[data-search-form]").requestSubmit();
    });

    $("[data-search-form]").addEventListener("submit", async (event) => {
      renderSuggestions([]);
      event.preventDefault();
      const symbol = $("[data-search-input]").value.trim().toUpperCase();
      if (!symbol) return;
      const card = $("[data-lookup-card]");
      card.hidden = false;
      card.innerHTML = `<span class="sig-firing-empty">Looking up ${esc(symbol)}…</span>`;
      try {
        const fresh = state.forceFresh ? "?fresh=true" : "";
        state.forceFresh = false;
        const lookup = await fetchJSON(`/api/signals/lookup/${encodeURIComponent(symbol)}${fresh}`);
        const history = await fetchJSON(`/api/signals/history/${encodeURIComponent(symbol)}`).catch(() => ({ signals: [], scores: [] }));
        const onList = state.watchlist.find((r) => r.symbol === symbol) || null;
        card.innerHTML = cardBody({ lookup, history }, onList);
      } catch (err) {
        console.error(err);
        const msg = /404/.test(String(err))
          ? `No usable history for ${esc(symbol)} — it may be too new or the symbol may be wrong.`
          : `Lookup failed for ${esc(symbol)}. The price API may be rate-limiting.`;
        card.innerHTML = `<span class="sig-warnings">${msg}</span>`;
      }
    });

    // §6.4: "Add to watchlist (with source = Own or chosen)".
    $("[data-lookup-card]").addEventListener("click", async (event) => {
      const add = event.target.closest("[data-add-symbol]");
      if (add) {
        const symbol = add.dataset.addSymbol;
        const sourceSel = $("[data-add-source]");
        const source = sourceSel ? sourceSel.value : "OWN";
        try {
          await fetchJSON(`/api/signals/watchlist?symbol=${encodeURIComponent(symbol)}&source_tag=${encodeURIComponent(source)}`, { method: "POST" });
          toast(`${symbol} added (${source}).`);
          await refresh();
        } catch (err) {
          console.error(err);
          toast("Add failed. Check console.");
        }
        return;
      }
      const action = event.target.closest("[data-action]");
      if (action) await handleRowAction(action.dataset.action, action.dataset.symbol);
    });
  }

  function renderSuggestions(results) {
    const host = $("[data-suggestions]");
    if (!results.length) { host.hidden = true; host.innerHTML = ""; return; }
    host.hidden = false;
    host.innerHTML = results.map((r) => `
      <div class="sig-suggest" data-suggest-symbol="${esc(r.symbol)}">
        <span class="sig-suggest-sym">${esc(r.symbol)}</span>
        <span class="sig-suggest-name">${esc(r.name)}</span>
        <span class="sig-suggest-cls">${esc(r.asset_class)}</span>
      </div>`).join("");
  }

  // ── Helpers ──────────────────────────────────────────────────────────────

  async function fetchJSON(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
    return response.json();
  }

  function esc(value) {
    if (value == null) return "";
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function fmt(value) {
    if (value == null) return "—";
    const n = Number(value);
    if (!isFinite(n)) return "—";
    if (Math.abs(n) >= 1000) return n.toFixed(0);
    if (Math.abs(n) >= 10) return n.toFixed(1);
    return n.toFixed(2);
  }

  let toastTimer = null;
  function toast(message) {
    const host = $("[data-toast]");
    host.textContent = message;
    host.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { host.hidden = true; }, 3200);
  }
})();
