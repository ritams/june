/* DJG Steno Mirror dashboard. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    mirror: null,
    portfolio: null,
  };

  document.addEventListener("DOMContentLoaded", () => {
    bindActions();
    bindDrawer();
    // Scroll the active nav pill into view if it's clipped on mobile.
    const activeNav = document.querySelector(".djg-nav a.active");
    if (activeNav && activeNav.scrollIntoView) {
      activeNav.scrollIntoView({ behavior: "instant", inline: "center", block: "nearest" });
    }
    boot().catch((err) => {
      console.error("Steno boot failed", err);
      toast("Failed to load. Check console.");
    });
  });

  async function boot() {
    showSpinner("Loading Steno mirror…");
    try {
      await refresh();
    } finally {
      hideSpinner();
    }
    // If a refresh is already running server-side (e.g. scheduled daily pull),
    // resume the progress UI on page load so the user sees what's happening.
    try {
      const s = await fetchJSON("/api/steno/refresh-status");
      if (s && s.status === "running") {
        setOpsNotice("Resuming in-flight Steno refresh…");
        pollStenoRefreshStatus();
      }
    } catch (_) {}
    setInterval(() => refresh().catch(console.error), 5 * 60 * 1000);
  }

  async function refresh() {
    const [mirror, portfolio] = await Promise.all([
      fetchJSON("/api/mirror"),
      fetchJSON("/api/steno/portfolio").catch(() => ({ available: false })),
    ]);
    state.mirror = mirror;
    state.portfolio = portfolio;
    render();
  }

  function render() {
    renderHeadline();
    renderActionBreakdown();
    renderSignals();
    renderMacroNotes();
    renderUpdates();
    renderFooter();
  }

  function renderHeadline() {
    const m = state.mirror || {};
    const p = state.portfolio?.portfolio || {};

    const tone = m.steno_risk_tone || "—";
    const card = $("[data-steno-card]");
    if (card) card.dataset.tone = tone;
    $("[data-steno-tone]").textContent = (tone || "—").toString().toUpperCase();
    $("[data-steno-summary]").textContent = m.steno_summary || p.summary || "Waiting for Steno data…";
    $("[data-steno-date]").textContent = m.steno_report_date || p.report_date || "—";
    $("[data-steno-count]").textContent = (m.total_buckets || (p.positions?.length || 0));
    const um = m.universe_meta;
    const aside = $("[data-universe-meta]");
    if (aside) {
      aside.textContent = um && um.reports_used && um.reports_used.length
        ? `· rolling ${um.reports_used.length}-report universe (core ${um.core_model_date || '—'})`
        : "";
    }
    const cash = m.steno_cash_weight_pct;
    $("[data-steno-cash]").innerHTML = cash != null ? `Cash <em>${cash.toFixed(1)}%</em>` : "";

    const align = m.alignment_pct != null ? `${m.alignment_pct.toFixed(0)}%` : "—";
    $("[data-alignment-pct]").textContent = align;
    const card2 = $("[data-mirror-card]");
    // Use neutral/yellow even for low alignment — red is reserved for action badges (Sell/Remove)
    if (m.alignment_pct != null) {
      card2.dataset.tone = m.alignment_pct >= 80 ? "positive" : "neutral";
    }
    const aligned = m.action_counts?.Hold || 0;
    const totalBuckets = m.total_buckets || 0;
    const needAction = totalBuckets - aligned + (m.off_thesis_count || 0);
    const alignedLabel = pluralize(aligned, "bucket");
    const actionLabel = pluralize(needAction, "signal");
    $("[data-alignment-detail]").textContent =
      m.available && m.ibkr_connected
        ? `${aligned} ${alignedLabel} aligned · ${needAction} ${actionLabel} need action`
        : "Connect IBKR to compute alignment.";
    const cap = m.total_capital_to_move;
    $("[data-mirror-capital]").textContent = cap ? `≈ ${fmtMoney(cap, m.base_currency)} to move` : "";
    $("[data-nav]").textContent = m.nav ? fmtMoney(m.nav, m.base_currency, 0) : "—";
    $("[data-ibkr-state]").textContent = m.ibkr_connected ? "connected" : "no snapshot";
    $("[data-tolerance]").textContent = m.tolerance_pct != null ? `TOL ±${m.tolerance_pct.toFixed(2)}%` : "TOL —";
  }

  function renderActionBreakdown() {
    const m = state.mirror || {};
    const counts = m.action_counts || {};
    const order = ["Buy", "Add", "Hold", "Trim", "Sell", "Remove", "Missing"];
    const root = $("[data-action-tiles]");
    root.innerHTML = "";
    order.forEach((action) => {
      const tile = document.createElement("div");
      tile.className = "steno-action-tile";
      tile.dataset.action = action;
      tile.innerHTML = `<span class="label">${action.toUpperCase()}</span><span class="count">${counts[action] || 0}</span>`;
      root.appendChild(tile);
    });
  }

  function renderSignals() {
    const root = $("[data-signals-table]");
    const empty = $("[data-signals-empty]");
    Array.from(root.querySelectorAll(".steno-row:not(.steno-header), .steno-offthesis-header, .steno-offthesis-row")).forEach((n) => n.remove());

    const m = state.mirror || {};
    const buckets = m.buckets || [];
    const offThesis = m.off_thesis || [];
    const totalRows = buckets.length + offThesis.length;
    $("[data-signal-count]").textContent = `${buckets.length} buckets · ${offThesis.length} off-thesis`;

    if (!m.available) {
      empty.textContent = m.reason || "No mirror data yet. Refresh Steno + IBKR below.";
      empty.style.display = "block";
      return;
    }
    if (totalRows === 0) {
      empty.textContent = "No positions to compare yet.";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";

    buckets.forEach((b) => root.appendChild(renderBucketRow(b, m)));

    if (offThesis.length > 0) {
      const header = document.createElement("div");
      header.className = "steno-offthesis-header";
      header.innerHTML = `<span>Off-thesis holdings</span><span class="sub">${offThesis.length} position${offThesis.length === 1 ? "" : "s"} not classified into any Steno bucket — candidates for removal.</span>`;
      root.appendChild(header);
      offThesis.forEach((o) => root.appendChild(renderOffThesisRow(o, m)));
    }
  }

  function renderBucketRow(b, m) {
    const row = document.createElement("div");
    row.className = "steno-row steno-bucket-row";
    row.dataset.action = b.action;
    row.tabIndex = 0;
    const isShort = b.direction === "short";
    const displayGap = isShort ? -b.gap_pct : b.gap_pct;
    const gapClass = displayGap > 0 ? "gap-pos" : displayGap < 0 ? "gap-neg" : "";
    const dirBadge = isShort ? '<span class="short-tag" title="short position">SHORT</span>' : "";
    const aiBadge = b.ticker_source === "perplexity"
      ? '<span class="ai-tag" title="Proxy ticker proposed by Perplexity — Steno didn\'t name a specific symbol">AI</span>'
      : "";
    const subTicker = b.ticker && b.ticker !== b.name ? b.ticker + " · " : "";
    const memberCount = (b.members || []).length;
    const memberLine = memberCount
      ? (b.members.slice(0, 4).map((mm) => `${mm.symbol} ${fmtPct(Math.abs(mm.weight_pct))}`).join(" · ")
         + (memberCount > 4 ? ` · +${memberCount - 4} more` : ""))
      : "no holdings in this bucket";
    const warnBadge = (b.warnings && b.warnings.length)
      ? `<span class="warn-tag" title="${escapeAttr(b.warnings.join(' · '))}">⚠ ${b.warnings.length}</span>`
      : "";
    // Core vs tactical chip — explains where this bucket came from in the rolling universe
    const themeBadge = b.is_tactical
      ? `<span class="theme-tag tactical" title="Tactical overlay — added in ${b.source_report_date || 'a recent update'}">TACTICAL</span>`
      : b.is_core
        ? `<span class="theme-tag core" title="Core model theme — first seen ${b.first_seen || '—'}, last mentioned ${b.last_seen || '—'}">CORE</span>`
        : "";
    const stenoDisplay = fmtPct(Math.abs(b.steno_weight_pct));
    const danDisplay = fmtPct(Math.abs(b.dan_weight_pct));
    const gapDisplay = b.action === "Hold" ? "—" : fmtSignedPct(displayGap);
    const capDisplay = b.action === "Hold" ? "—" : fmtMoney(b.capital_amount || 0, m.base_currency, 0);
    row.innerHTML = `
      <span class="col-action">${b.action}</span>
      <span class="col-name">
        <strong>${b.name}${dirBadge}${themeBadge}${aiBadge}${warnBadge}</strong>
        <span class="sub">${subTicker}${b.asset_class} · ${memberCount} holding${memberCount === 1 ? "" : "s"}</span>
        <span class="sub members">${memberLine}</span>
      </span>
      <span class="col-num">${stenoDisplay}</span>
      <span class="col-num">${danDisplay}</span>
      <span class="col-num ${gapClass}">${gapDisplay}</span>
      <span class="col-num">${capDisplay}</span>
    `;
    row.addEventListener("click", () => openBucketDrawer(b));
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") openBucketDrawer(b); });
    return row;
  }

  function renderOffThesisRow(o, m) {
    const row = document.createElement("div");
    row.className = "steno-row steno-offthesis-row";
    row.dataset.action = "Remove";
    row.tabIndex = 0;
    const weight = fmtPct(Math.abs(o.weight_pct));
    const cap = fmtMoney(o.market_value || 0, m.base_currency, 0);
    row.innerHTML = `
      <span class="col-action">Remove</span>
      <span class="col-name">
        <strong>${o.symbol}</strong>
        <span class="sub">${o.description ? o.description + " · " : ""}off-thesis</span>
        <span class="sub members">${(o.rationale || "").slice(0, 110)}</span>
      </span>
      <span class="col-num">—</span>
      <span class="col-num">${weight}</span>
      <span class="col-num gap-neg">${fmtSignedPct(-o.weight_pct)}</span>
      <span class="col-num">${cap}</span>
    `;
    row.addEventListener("click", () => openOffThesisDrawer(o));
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") openOffThesisDrawer(o); });
    return row;
  }

  function renderUpdates() {
    const list = $("[data-updates-list]");
    const aside = $("[data-updates-aside]");
    if (!list) return;
    list.innerHTML = "";
    const updates = state.portfolio?.updates || [];
    const modelDate = state.portfolio?.portfolio?.report_date || "—";
    if (aside) aside.textContent = updates.length
      ? `${updates.length} since ${modelDate}`
      : `none since ${modelDate}`;
    if (!updates.length) {
      const li = document.createElement("li");
      li.className = "steno-updates-empty";
      li.textContent = "No commentary or tactical updates since the current model portfolio.";
      list.appendChild(li);
      return;
    }
    updates.forEach((u) => {
      const li = document.createElement("li");
      li.className = "steno-update";
      const tone = (u.risk_tone || "").toLowerCase();
      li.dataset.tone = tone;
      const posCount = (u.positions || []).length;
      const posHint = posCount
        ? `<span class="sub">${posCount} tactical position${posCount === 1 ? "" : "s"}</span>`
        : `<span class="sub">commentary only</span>`;
      li.innerHTML = `
        <div class="steno-update-head">
          <strong>${u.report_date || "—"}</strong>
          <span class="tone">${(u.risk_tone || "—").toUpperCase()}</span>
          ${posHint}
        </div>
        <p class="steno-update-summary">${escapeHtml((u.summary || "").slice(0, 280))}</p>
      `;
      list.appendChild(li);
    });
  }

  function renderMacroNotes() {
    const list = $("[data-macro-notes]");
    list.innerHTML = "";
    const notes = state.portfolio?.portfolio?.macro_notes || [];
    if (!notes.length) {
      const li = document.createElement("li");
      li.className = "steno-macro-empty";
      li.textContent = "No macro notes extracted from the latest report.";
      list.appendChild(li);
      return;
    }
    notes.forEach((n) => {
      const li = document.createElement("li");
      li.textContent = n;
      list.appendChild(li);
    });
  }

  function renderFooter() {
    const m = state.mirror || {};
    $("[data-footer-steno]").textContent = m.steno_report_date || "—";
    $("[data-footer-ibkr]").textContent = m.ibkr_fetched_at ? new Date(m.ibkr_fetched_at).toLocaleString() : "—";
  }

  // ─── Drawer ────────────────────────────────────────────────
  function bindDrawer() {
    $$("[data-drawer-close]").forEach((el) => el.addEventListener("click", closeDrawer));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  }
  function openBucketDrawer(b) {
    const drawer = $("[data-drawer]");
    drawer.hidden = false;
    document.body.style.overflow = "hidden";
    $("[data-drawer-action]").textContent = b.action.toUpperCase();
    $("[data-drawer-title]").textContent = b.name + (b.ticker && b.ticker !== b.name ? ` · ${b.ticker}` : "");
    $("[data-drawer-sub]").textContent = `${b.asset_class}${b.direction === "short" ? " · short" : ""}${b.change_vs_prior ? ` · ${b.change_vs_prior}` : ""} · ${(b.members || []).length} holding${(b.members||[]).length === 1 ? "" : "s"}`;
    const stats = $("[data-drawer-stats]");
    const cur = state.mirror?.base_currency || "USD";
    stats.innerHTML = `
      <div class="djg-drawer-stat"><span>Steno target</span><strong>${fmtPct(b.steno_weight_pct)}</strong></div>
      <div class="djg-drawer-stat"><span>Your current</span><strong>${fmtPct(b.dan_weight_pct)}</strong></div>
      <div class="djg-drawer-stat"><span>Gap</span><strong>${fmtSignedPct(b.gap_pct)}</strong></div>
      <div class="djg-drawer-stat"><span>Capital to move</span><strong>${b.capital_amount ? fmtMoney(b.capital_amount, cur, 0) : "—"}</strong></div>
    `;
    let commentary = b.commentary || "No commentary in the report.";
    if (b.ticker_source === "perplexity" && b.ticker) {
      commentary += `  (Proxy ticker ${b.ticker} proposed by Perplexity — Steno didn't name a specific symbol.)`;
    }
    $("[data-drawer-commentary]").textContent = commentary;
    const warnEl = $("[data-drawer-warnings]");
    if (warnEl) {
      const ws = b.warnings || [];
      if (ws.length) {
        warnEl.style.display = "block";
        warnEl.innerHTML = ws.map((w) => `<div class="warn-row">⚠ ${escapeHtml(w)}</div>`).join("");
      } else {
        warnEl.style.display = "none";
        warnEl.innerHTML = "";
      }
    }
    renderMembersList(b);
  }

  function openOffThesisDrawer(o) {
    const drawer = $("[data-drawer]");
    drawer.hidden = false;
    document.body.style.overflow = "hidden";
    $("[data-drawer-action]").textContent = "REMOVE";
    $("[data-drawer-title]").textContent = o.symbol;
    $("[data-drawer-sub]").textContent = `${o.description || ""} · off-thesis`;
    const cur = state.mirror?.base_currency || "USD";
    $("[data-drawer-stats]").innerHTML = `
      <div class="djg-drawer-stat"><span>Steno target</span><strong>—</strong></div>
      <div class="djg-drawer-stat"><span>Your current</span><strong>${fmtPct(o.weight_pct)}</strong></div>
      <div class="djg-drawer-stat"><span>Gap</span><strong>${fmtSignedPct(-o.weight_pct)}</strong></div>
      <div class="djg-drawer-stat"><span>Position value</span><strong>${fmtMoney(o.market_value || 0, cur, 0)}</strong></div>
    `;
    $("[data-drawer-commentary]").textContent = o.rationale || "Not classified into any current Steno bucket.";
    const warnEl = $("[data-drawer-warnings]");
    if (warnEl) { warnEl.style.display = "none"; warnEl.innerHTML = ""; }
    renderOffThesisActions(o);
  }

  function renderMembersList(bucket) {
    const wrap = $("[data-drawer-members]");
    if (!wrap) return;
    wrap.innerHTML = "";
    const members = bucket.members || [];
    if (!members.length) {
      wrap.innerHTML = `<div class="drawer-empty">No Dan holdings classified into this bucket yet. Pin a ticker via the off-thesis list below.</div>`;
      return;
    }
    const buckets = (state.mirror?.buckets || []).filter((b) => !!b.name);
    const optionsHtml = buckets.map((b) => `<option value="${escapeAttr(b.name)}">${escapeHtml(b.name)}</option>`).join("");
    members.forEach((m) => {
      const div = document.createElement("div");
      div.className = "drawer-member";
      const srcBadge = m.source === "equivalence" ? '<span class="member-src eq">EQ</span>'
                       : m.source === "override" ? '<span class="member-src ov">PIN</span>'
                       : '<span class="member-src ai">AI</span>';
      const conf = m.confidence != null ? Math.round(m.confidence * 100) : null;
      const confBadge = conf != null
        ? `<span class="member-conf ${conf >= 80 ? 'high' : conf >= 50 ? 'mid' : 'low'}" title="AI confidence">${conf}%</span>`
        : "";
      const dirWarn = m.direction_match === false
        ? `<span class="member-warn" title="Direction mismatch — your long position is in a short Steno bucket (or vice versa)">⚠ DIR</span>`
        : "";
      div.innerHTML = `
        <div class="drawer-member-head">
          <strong>${m.symbol}</strong>
          <span class="weight">${fmtPct(Math.abs(m.weight_pct))}</span>
          ${srcBadge}${confBadge}${dirWarn}
        </div>
        <div class="drawer-member-rationale">${escapeHtml(m.rationale || "")}</div>
        <div class="drawer-member-actions">
          <select data-reassign-target>
            <option value="">— reassign bucket —</option>
            ${optionsHtml}
            <option value="__off">Off-thesis (explicit)</option>
            <option value="__clear">Clear override (auto)</option>
          </select>
          <button class="djg-pill djg-pill-tiny" data-apply-reassign>Apply</button>
        </div>
      `;
      const select = div.querySelector("[data-reassign-target]");
      const btn = div.querySelector("[data-apply-reassign]");
      btn.addEventListener("click", () => applyReassign(m.symbol, select.value));
      wrap.appendChild(div);
    });
  }

  function renderOffThesisActions(o) {
    const wrap = $("[data-drawer-members]");
    if (!wrap) return;
    wrap.innerHTML = "";
    const buckets = (state.mirror?.buckets || []).filter((b) => !!b.name);
    const optionsHtml = buckets.map((b) => `<option value="${escapeAttr(b.name)}">${escapeHtml(b.name)}</option>`).join("");
    const div = document.createElement("div");
    div.className = "drawer-member";
    div.innerHTML = `
      <div class="drawer-member-head"><strong>Pin ${o.symbol} to a Steno bucket</strong></div>
      <div class="drawer-member-rationale">Auto-classifier returned no match. If you think this ticker fits a Steno theme, pin it here — the override is saved and persists across refreshes.</div>
      <div class="drawer-member-actions">
        <select data-reassign-target>
          <option value="">— choose bucket —</option>
          ${optionsHtml}
          <option value="__off">Off-thesis (explicit)</option>
          <option value="__clear">Clear override (auto)</option>
        </select>
        <button class="djg-pill djg-pill-tiny" data-apply-reassign>Apply</button>
      </div>
    `;
    const select = div.querySelector("[data-reassign-target]");
    const btn = div.querySelector("[data-apply-reassign]");
    btn.addEventListener("click", () => applyReassign(o.symbol, select.value));
    wrap.appendChild(div);
  }

  async function applyReassign(ticker, value) {
    if (!value) return;
    const params = new URLSearchParams({ ticker });
    if (value === "__clear") params.set("clear", "true");
    else if (value === "__off") {} // no bucket param = off-thesis
    else params.set("bucket", value);
    try {
      const r = await fetch("/api/mirror/override?" + params.toString(), { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      toast(`Pinned ${ticker}`);
      closeDrawer();
      await refresh();
    } catch (err) {
      console.error(err);
      toast(`Failed: ${err.message || err}`);
    }
  }

  function escapeHtml(s) { return String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
  function escapeAttr(s) { return String(s || "").replace(/"/g, "&quot;"); }
  function closeDrawer() {
    $("[data-drawer]").hidden = true;
    document.body.style.overflow = "";
  }

  // ─── Operations ────────────────────────────────────────────
  function bindActions() {
    $("[data-action-ibkr-refresh]").addEventListener("click", () => triggerOp("/api/ibkr/refresh", "Refreshing IBKR snapshot…"));
    $("[data-action-steno-refresh]").addEventListener("click", () => triggerStenoRefresh());
    $("[data-action-steno-cached]").addEventListener("click", () => triggerOp("/api/steno/ingest-cached", "Re-ingesting cached PDFs…"));
    $("[data-action-resolve-tickers]").addEventListener("click", () => triggerOp("/api/steno/resolve-tickers", "Asking Perplexity to resolve ambiguous theme tickers…"));
  }
  async function triggerStenoRefresh() {
    setOpsNotice("Kicking off Steno refresh (async)…");
    try {
      const r = await fetch("/api/steno/refresh", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      if (body.started === false) {
        setOpsNotice(`Refresh already running (${body.state?.stage || "in progress"}). Waiting…`);
      }
      pollStenoRefreshStatus();
    } catch (err) {
      console.error(err);
      setOpsNotice(`Error starting refresh: ${err.message || err}`);
      toast(`Op failed: ${err.message || err}`);
    }
  }

  async function pollStenoRefreshStatus() {
    const start = Date.now();
    const MAX_MS = 30 * 60 * 1000; // hard ceiling so we never poll forever
    while (Date.now() - start < MAX_MS) {
      try {
        const r = await fetch("/api/steno/refresh-status");
        const s = await r.json();
        renderRefreshProgress(s);
        if (s.status === "complete" || s.status === "error" || s.status === "idle") {
          if (s.status === "complete") setOpsNotice(stenoDoneMessage(s));
          else if (s.status === "error") setOpsNotice(`Refresh error: ${(s.errors || []).slice(-1)[0] || "unknown"}`);
          await refresh();
          return;
        }
      } catch (err) {
        console.warn("status poll failed", err);
      }
      await sleep(3000);
    }
    setOpsNotice("Refresh polling timed out — check /api/steno/refresh-status manually.");
  }

  function renderRefreshProgress(s) {
    if (s.status !== "running") return;
    const stage = s.stage || "running";
    const total = s.ingest_total || 0;
    const done = s.ingest_done || 0;
    const cov = s.coverage_after || s.coverage_before;
    const covMsg = cov ? `${(cov.have_in_window || []).length}/${cov.target_weeks} weeks` : "";
    let msg = `Steno refresh · ${stage}`;
    if (total) msg += ` · ingest ${done}/${total}`;
    if (covMsg) msg += ` · coverage ${covMsg}`;
    if (s.current) msg += ` · ${s.current}`;
    setOpsNotice(msg);
  }

  function stenoDoneMessage(s) {
    const ing = (s.ingested || []).length;
    const dl = (s.downloaded || []).length;
    const err = (s.errors || []).length;
    const cov = s.coverage_after;
    let m = `Done · downloaded ${dl} · ingested ${ing}`;
    if (cov) m += ` · coverage ${(cov.have_in_window || []).length}/${cov.target_weeks} weeks`;
    if (err) m += ` · ${err} warning${err === 1 ? "" : "s"}`;
    return m;
  }

  function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }

  async function triggerOp(path, msg) {
    setOpsNotice(msg);
    showSpinner(msg);
    try {
      const r = await fetch(path, { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      setOpsNotice("Done. Refreshing dashboard…");
      await refresh();
      setOpsNotice("");
    } catch (err) {
      console.error(err);
      setOpsNotice(`Error: ${err.message || err}`);
      toast(`Op failed: ${err.message || err}`);
    } finally {
      hideSpinner();
    }
  }
  function setOpsNotice(text) {
    const el = $("[data-ops-notice]");
    if (el) el.textContent = text || "";
  }

  // ─── Spinner / toast / formatters ──────────────────────────
  function showSpinner(text) {
    const el = $("[data-spinner]");
    if (!el) return;
    el.hidden = false;
    if (text) $("[data-spinner-text]").textContent = text;
  }
  function hideSpinner() {
    const el = $("[data-spinner]");
    if (el) el.hidden = true;
  }
  function toast(text) {
    const el = $("[data-toast]");
    el.textContent = text;
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 2600);
  }
  async function fetchJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
    return r.json();
  }
  function fmtPct(v) {
    if (v == null || !Number.isFinite(v)) return "—";
    return `${v.toFixed(1)}%`;
  }
  function fmtSignedPct(v) {
    if (v == null || !Number.isFinite(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(1)}%`;
  }
  function fmtMoney(v, ccy, digits = 0) {
    if (v == null || !Number.isFinite(v)) return "—";
    const cur = (ccy || "USD").toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: cur,
        maximumFractionDigits: digits,
      }).format(v);
    } catch {
      return `${cur} ${v.toLocaleString(undefined, { maximumFractionDigits: digits })}`;
    }
  }
  function pluralize(n, word) {
    return Math.abs(n) === 1 ? word : `${word}s`;
  }
})();
