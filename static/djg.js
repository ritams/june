/* DJG Advisory dashboard. Mobile-first dense layout.
   Keeps the existing /api/snapshot, /api/scenario, /api/scenario/presets contract.
*/
(() => {
  "use strict";

  const FACTOR_LABELS = {
    risk_on_off: "Risk",
    growth: "Growth",
    inflation: "Inflation",
    short_rates: "Rates",
    liquidity: "Liquidity",
    dollar: "Dollar",
    oil: "Oil",
  };
  const FACTOR_ORDER = ["growth", "inflation", "liquidity", "short_rates", "dollar", "oil", "risk_on_off"];

  const state = {
    snapshot: null,
    presets: null,
    scenario: null,
    factorStats: {},
    matrixCache: null,
    heatmapMode: "composite", // composite | t_hac | weight
    sliderValues: {},
    pendingSliderTimer: null,
    lastWeightsByKey: {},
    autoMode: true,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  // ─── Boot ────────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    bindHeatmapToggles();
    bindScenarioActions();
    bindDrawerCloseHandlers();
    boot().catch((err) => {
      console.error("DJG boot failed", err);
      toast("Failed to load. Check console.");
    });
  });

  async function boot() {
    await refreshAll();
    // Update every 5 minutes once loaded — daily data refresh server-side anyway.
    setInterval(() => refreshAll().catch(console.error), 5 * 60 * 1000);
  }

  async function refreshAll() {
    const [snapshot, presets] = await Promise.all([
      fetchJSON("/api/snapshot"),
      fetchJSON("/api/scenario/presets").catch(() => null),
    ]);
    state.snapshot = snapshot;
    state.presets = presets;
    renderHeadline(snapshot);
    renderUpdatedAt(snapshot);
    populatePresets(presets);
    initSlidersIfNeeded(presets);
    renderFactorGrid(snapshot);
    if (state.autoMode) {
      // Pull the auto scenario (live-state derived) for the allocation engine
      await runScenario({ auto: true });
    } else {
      await runScenario(state.sliderValues);
    }
  }

  // ─── Network ─────────────────────────────────────────────────────────────
  async function fetchJSON(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  async function postJSON(path) {
    const res = await fetch(path, { method: "POST" });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  // ─── Headline ────────────────────────────────────────────────────────────
  function renderHeadline(snapshot) {
    const phaseCard = $("[data-phase-card]");
    const phase = snapshot.phase || { key: "unknown", label: "Unknown", blurb: "—" };
    phaseCard.dataset.phase = phase.key;
    $("[data-phase-badge]").textContent = phase.label || "Unknown";
    $("[data-phase-blurb]").textContent = phase.blurb || "";
    $("[data-phase-growth]").textContent = formatDir(phase.growth_dir, phase.growth_z);
    $("[data-phase-inflation]").textContent = formatDir(phase.inflation_dir, phase.inflation_z);
    $("[data-phase-liquidity]").textContent = formatDir(phase.liquidity_dir, null);
    const conf = $("[data-phase-confirmation]");
    if (phase.confirmed && phase.months_in_phase) {
      conf.innerHTML = `Confirmed · <em>${phase.months_in_phase}m</em>`;
    } else if (phase.proposed_phase && phase.proposed_phase !== phase.key) {
      conf.innerHTML = `Pending · proposed <em>${cap(phase.proposed_phase)}</em>`;
    } else {
      conf.textContent = "";
    }

    const signal = snapshot.signal || {};
    const sig = $("[data-signal-card]");
    sig.dataset.tone = signal.tone || "neutral";
    $("[data-signal-badge]").textContent = signal.label || "—";
    $("[data-signal-summary]").textContent = signal.summary || "";
    $("[data-signal-action]").textContent = signal.action || "";
  }

  function renderUpdatedAt(snapshot) {
    const generatedAt = snapshot.generated_at;
    if (generatedAt) {
      const dt = new Date(generatedAt);
      $("[data-updated-at]").textContent = `${dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${dt.toLocaleDateString()}`;
    }
    if (snapshot.backtest && snapshot.backtest.last_calculated) {
      $("[data-backtest-date]").textContent = snapshot.backtest.last_calculated;
    }
  }

  // ─── Factor grid ─────────────────────────────────────────────────────────
  function renderFactorGrid(snapshot) {
    const grid = $("[data-factor-grid]");
    grid.innerHTML = "";
    const phase = snapshot.phase || {};
    // Build a dict of {factor_key: {z, dir}} from phase + sliders fallback.
    const zMap = {
      growth:    { z: phase.growth_z,    dir: phase.growth_dir },
      inflation: { z: phase.inflation_z, dir: phase.inflation_dir },
      liquidity: { z: null,              dir: phase.liquidity_dir },
    };
    // Backfill remaining factors from auto-scenario if we have it
    if (state.scenario && state.scenario.scenario) {
      Object.entries(state.scenario.scenario).forEach(([k, v]) => {
        if (zMap[k] === undefined) zMap[k] = { z: typeof v === "number" ? v : null, dir: directionFromValue(v) };
      });
    }
    FACTOR_ORDER.forEach((key) => {
      const data = zMap[key] || { z: null, dir: null };
      const cell = document.createElement("div");
      cell.className = "djg-factor-cell";
      const zClass = data.z == null ? "flat" : data.z > 0.25 ? "pos" : data.z < -0.25 ? "neg" : "flat";
      cell.innerHTML = `
        <div class="djg-factor-label">${FACTOR_LABELS[key] || key}</div>
        <div class="djg-factor-z ${zClass}">${data.z == null ? "—" : signed(data.z, 2)}</div>
        <div class="djg-factor-trend ${data.dir || ""}">${data.dir ? data.dir.toUpperCase() : "—"}</div>`;
      grid.appendChild(cell);
    });
  }

  function directionFromValue(v) {
    if (typeof v !== "number") return null;
    if (v > 0.25) return "up";
    if (v < -0.25) return "down";
    return null;
  }

  // ─── Allocation ──────────────────────────────────────────────────────────
  function renderAllocation(scenario) {
    const alloc = scenario.allocation || {};
    const tableEl = $("[data-allocation-top]");
    const emptyEl = $("[data-allocation-empty]");
    // Clear existing rows
    Array.from(tableEl.querySelectorAll(".djg-alloc-row")).forEach((n) => n.remove());

    const top = (alloc.top_assets || []).slice(0, 5);
    if (!alloc.available || top.length === 0) {
      emptyEl.textContent = alloc.notes && alloc.notes[0] ? alloc.notes[0] : "No positive-score assets in this scenario.";
      emptyEl.style.display = "block";
    } else {
      emptyEl.style.display = "none";
      top.forEach((asset, idx) => {
        const row = document.createElement("div");
        row.className = "djg-alloc-row";
        row.dataset.assetKey = asset.key;
        const conf = asset.confidence || {};
        const tHac = asset.disclosures
          ? avg(asset.disclosures.map((d) => Math.abs(d.t_stat_hac || 0)).filter(Boolean))
          : asset.avg_t_stat;
        row.innerHTML = `
          <span>${idx + 1}</span>
          <span class="ticker">${asset.ticker || asset.key}<span class="label-sub">${asset.label || ""}</span></span>
          <span class="num">${signed(asset.weight_pct, 1)}%</span>
          <span class="num">${signed(asset.expected_return, 1)}%</span>
          <span class="num">${formatT(tHac)}</span>
          <span><span class="djg-conf-badge" data-tier="${conf.key || 'noise'}">${(conf.label || '—').toUpperCase()}</span></span>
        `;
        row.addEventListener("click", () => openDrawer(asset.key));
        tableEl.appendChild(row);
      });
    }

    $("[data-allocation-cash]").textContent = `${(alloc.cash_weight_pct ?? 100).toFixed(1)}%`;
    $("[data-allocation-tau]").textContent = alloc.tau != null ? `τ ${alloc.tau.toFixed(2)}` : "τ —";

    // Bottom 5
    const bottomEl = $("[data-allocation-bottom]");
    bottomEl.innerHTML = "";
    (alloc.bottom_assets || []).slice(0, 5).forEach((asset) => {
      const row = document.createElement("div");
      row.className = "djg-bottom-row";
      const conf = asset.confidence || {};
      row.innerHTML = `
        <span class="ticker">${asset.ticker || asset.key}<span class="label-sub">${asset.label || ""}</span></span>
        <span class="num">${signed(asset.composite_score, 1)}</span>
        <span class="num">${signed(asset.expected_return, 1)}%</span>
        <span><span class="djg-conf-badge" data-tier="${conf.key || 'noise'}">${(conf.label || '—').toUpperCase()}</span></span>
      `;
      row.style.cursor = "pointer";
      row.addEventListener("click", () => openDrawer(asset.key));
      bottomEl.appendChild(row);
    });

    // Cache for later weight-aware heatmap mode
    state.lastWeightsByKey = {};
    (alloc.top_assets || []).forEach((asset) => { state.lastWeightsByKey[asset.key] = asset.weight; });
  }

  function avg(xs) {
    const valid = xs.filter((x) => Number.isFinite(x));
    return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null;
  }

  // ─── Heat map ────────────────────────────────────────────────────────────
  function bindHeatmapToggles() {
    $$("[data-heatmap-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.heatmapMode = btn.dataset.heatmapToggle;
        renderHeatmapModeButtons();
        if (state.scenario) renderHeatmap(state.scenario);
      });
    });
    renderHeatmapModeButtons();
  }

  function renderHeatmapModeButtons() {
    $$("[data-heatmap-toggle]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.heatmapToggle === state.heatmapMode);
    });
    const label = { composite: "Composite score", t_hac: "T-stat (HAC, avg)", weight: "Allocation weight" }[state.heatmapMode];
    $("[data-heatmap-mode]").textContent = label;
  }

  function renderHeatmap(scenario) {
    const grid = $("[data-heatmap-grid]");
    grid.innerHTML = "";
    const buckets = scenario.buckets || {};
    const allAssets = [];
    Object.values(buckets).forEach((b) => (b.assets || []).forEach((a) => allAssets.push(a)));
    // Order: bucket → label
    const bucketOrder = ["asset_class","equity_region","equity_sector","style","fixed_income","commodity","currency","crypto"];
    allAssets.sort((a, b) => {
      const ba = bucketOrder.indexOf(a.bucket);
      const bb = bucketOrder.indexOf(b.bucket);
      if (ba !== bb) return ba - bb;
      return (a.label || "").localeCompare(b.label || "");
    });

    const mode = state.heatmapMode;
    const values = allAssets.map((a) => valueForMode(a, mode)).filter((v) => Number.isFinite(v));
    const maxAbs = Math.max(0.01, ...values.map(Math.abs));

    allAssets.forEach((asset) => {
      const cell = document.createElement("button");
      cell.type = "button";
      const v = valueForMode(asset, mode);
      cell.className = "djg-heat-cell " + colorClassFor(v, maxAbs, mode);
      if (asset.low_history) cell.classList.add("low-history");
      cell.dataset.assetKey = asset.key;
      cell.title = `${asset.label} · ${asset.ticker || asset.key} · ${describeMode(asset, mode)}`;
      cell.innerHTML = `<span class="ticker">${asset.ticker || asset.key}</span><span class="val">${formatModeValue(v, mode)}</span>`;
      cell.addEventListener("click", () => openDrawer(asset.key));
      grid.appendChild(cell);
    });
  }

  function valueForMode(asset, mode) {
    if (mode === "composite") return asset.composite_score;
    if (mode === "t_hac") return asset.avg_t_stat_hac ?? asset.avg_t_stat;
    if (mode === "weight") return (state.lastWeightsByKey[asset.key] || 0) * 100;
    return 0;
  }

  function describeMode(asset, mode) {
    if (mode === "composite") return `composite ${signed(asset.composite_score, 2)}`;
    if (mode === "t_hac") return `t-HAC ${signed(asset.avg_t_stat_hac ?? asset.avg_t_stat, 2)}`;
    if (mode === "weight") return `weight ${((state.lastWeightsByKey[asset.key] || 0) * 100).toFixed(1)}%`;
    return "";
  }

  function formatModeValue(v, mode) {
    if (!Number.isFinite(v)) return "—";
    if (mode === "weight") return `${v.toFixed(0)}%`;
    return v.toFixed(1);
  }

  function colorClassFor(v, maxAbs, mode) {
    if (!Number.isFinite(v)) return "flat";
    if (mode === "weight") {
      if (v >= 15) return "pos-strong";
      if (v >= 5) return "pos";
      return "flat";
    }
    if (mode === "t_hac") {
      const abs = Math.abs(v);
      if (abs >= 3) return v > 0 ? "pos-strong" : "neg-strong";
      if (abs >= 2) return v > 0 ? "pos" : "neg";
      if (abs < 1) return "flat";
      return v > 0 ? "pos" : "neg";
    }
    // composite
    const r = v / (maxAbs || 1);
    if (r >= 0.66) return "pos-strong";
    if (r >= 0.20) return "pos";
    if (r <= -0.66) return "neg-strong";
    if (r <= -0.20) return "neg";
    return "flat";
  }

  // ─── Sliders ─────────────────────────────────────────────────────────────
  function initSlidersIfNeeded(presets) {
    if (!presets || !presets.factor_keys) return;
    const wrap = $("[data-scenario-sliders]");
    if (wrap.children.length > 0) return;
    presets.factor_keys.forEach((key) => {
      state.sliderValues[key] = state.sliderValues[key] ?? 0;
      const row = document.createElement("label");
      row.className = "djg-slider-row";
      row.innerHTML = `
        <span class="label">${FACTOR_LABELS[key] || key}</span>
        <span class="value flat" data-slider-value="${key}">0.00</span>
        <input type="range" min="-1" max="1" step="0.05" value="0" data-slider-input="${key}" />`;
      wrap.appendChild(row);
    });
    $$("[data-slider-input]").forEach((input) => {
      input.addEventListener("input", (e) => {
        const key = e.target.dataset.sliderInput;
        const value = parseFloat(e.target.value);
        state.sliderValues[key] = value;
        const valueEl = $(`[data-slider-value="${key}"]`);
        valueEl.textContent = signed(value, 2);
        valueEl.className = `value ${value > 0.05 ? "pos" : value < -0.05 ? "neg" : "flat"}`;
        state.autoMode = false;
        scheduleScenarioUpdate();
        $("[data-scenario-mode]").textContent = "Manual";
      });
    });
  }

  function scheduleScenarioUpdate() {
    if (state.pendingSliderTimer) clearTimeout(state.pendingSliderTimer);
    state.pendingSliderTimer = setTimeout(() => {
      state.pendingSliderTimer = null;
      runScenario(state.sliderValues).catch(console.error);
    }, 250);
  }

  function bindScenarioActions() {
    $("[data-scenario-auto]").addEventListener("click", async () => {
      state.autoMode = true;
      $("[data-scenario-mode]").textContent = "Live";
      await runScenario({ auto: true });
      // Push live values back into the sliders for transparency
      if (state.scenario && state.scenario.scenario) {
        Object.entries(state.scenario.scenario).forEach(([k, v]) => {
          state.sliderValues[k] = v;
          const input = $(`[data-slider-input="${k}"]`);
          const valueEl = $(`[data-slider-value="${k}"]`);
          if (input) input.value = v;
          if (valueEl) {
            valueEl.textContent = signed(v, 2);
            valueEl.className = `value ${v > 0.05 ? "pos" : v < -0.05 ? "neg" : "flat"}`;
          }
        });
      }
    });
    $("[data-scenario-reset]").addEventListener("click", () => {
      state.autoMode = false;
      $("[data-scenario-mode]").textContent = "Manual";
      Object.keys(state.sliderValues).forEach((k) => (state.sliderValues[k] = 0));
      $$("[data-slider-input]").forEach((el) => (el.value = 0));
      $$("[data-slider-value]").forEach((el) => { el.textContent = "0.00"; el.className = "value flat"; });
      runScenario(state.sliderValues).catch(console.error);
    });
    const presetSel = $("[data-scenario-preset]");
    presetSel.addEventListener("change", () => {
      const presetKey = presetSel.value;
      if (!presetKey || !state.presets) return;
      const preset = state.presets.presets[presetKey];
      if (!preset) return;
      state.autoMode = false;
      $("[data-scenario-mode]").textContent = `Preset: ${preset.label}`;
      Object.keys(state.sliderValues).forEach((k) => {
        const v = typeof preset[k] === "number" ? preset[k] : 0;
        state.sliderValues[k] = v;
        const input = $(`[data-slider-input="${k}"]`);
        const valueEl = $(`[data-slider-value="${k}"]`);
        if (input) input.value = v;
        if (valueEl) {
          valueEl.textContent = signed(v, 2);
          valueEl.className = `value ${v > 0.05 ? "pos" : v < -0.05 ? "neg" : "flat"}`;
        }
      });
      runScenario(state.sliderValues).catch(console.error);
    });
  }

  function populatePresets(presets) {
    const sel = $("[data-scenario-preset]");
    if (!presets || sel.options.length > 1) return;
    Object.entries(presets.presets || {}).forEach(([key, preset]) => {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = preset.label;
      sel.appendChild(opt);
    });
  }

  async function runScenario(input) {
    const params = new URLSearchParams();
    if (input && input.auto) {
      params.set("auto", "true");
    } else if (input) {
      Object.entries(input).forEach(([k, v]) => {
        if (typeof v === "number") params.set(k, v.toFixed(3));
      });
    }
    const url = `/api/scenario?${params.toString()}`;
    let scenario;
    try {
      scenario = await fetchJSON(url);
    } catch (err) {
      console.error("scenario fetch", err);
      toast("Scenario failed");
      return;
    }
    state.scenario = scenario;
    if (scenario.last_calculated) $("[data-matrix-date]").textContent = scenario.last_calculated;
    if (scenario.caveat) $("[data-caveat]").textContent = scenario.caveat;

    renderAllocation(scenario);
    renderHeatmap(scenario);
    renderConviction(scenario);
    if (state.autoMode) renderFactorGrid(state.snapshot);
  }

  // ─── Conviction list ─────────────────────────────────────────────────────
  function renderConviction(scenario) {
    const list = $("[data-conviction-list]");
    list.innerHTML = "";
    const buckets = scenario.buckets || {};
    const candidates = [];
    Object.values(buckets).forEach((b) => {
      (b.assets || []).forEach((a) => {
        (a.disclosures || []).forEach((d) => {
          const tHac = Math.abs(d.t_stat_hac || 0);
          if (tHac >= 3.0) {
            candidates.push({ asset: a, factor: d.factor, t: d.t_stat_hac, n: d.n, hit: d.hit_rate, contribution: d.expected_contribution });
          }
        });
      });
    });
    candidates.sort((a, b) => Math.abs(b.t || 0) - Math.abs(a.t || 0));
    if (candidates.length === 0) {
      list.innerHTML = `<div class="djg-conv-row"><span class="pair">—</span><span class="stats">No HAC t-stats above 3.0 in this scenario.</span><span></span></div>`;
      return;
    }
    candidates.slice(0, 8).forEach((c) => {
      const row = document.createElement("div");
      row.className = "djg-conv-row";
      row.dataset.direction = c.t > 0 ? "bull" : "bear";
      const factorLabel = FACTOR_LABELS[c.factor] || c.factor;
      const hitTxt = c.hit != null ? `· hit ${(c.hit * 100).toFixed(0)}%` : "";
      row.innerHTML = `
        <span class="pair">${c.asset.ticker || c.asset.key} × ${factorLabel}</span>
        <span class="stats">t=${signed(c.t, 2)} · n=${c.n} ${hitTxt} · E[contribution] ${signed(c.contribution, 1)}%</span>
        <span class="djg-conf-badge" data-tier="high">HIGH</span>`;
      row.style.cursor = "pointer";
      row.addEventListener("click", () => openDrawer(c.asset.key));
      list.appendChild(row);
    });
  }

  // ─── Drawer ──────────────────────────────────────────────────────────────
  function bindDrawerCloseHandlers() {
    $$("[data-drawer-close]").forEach((el) => el.addEventListener("click", closeDrawer));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  }

  function openDrawer(assetKey) {
    if (!state.scenario) return;
    const scenario = state.scenario;
    const all = [];
    Object.values(scenario.buckets || {}).forEach((b) => (b.assets || []).forEach((a) => all.push(a)));
    const asset = all.find((a) => a.key === assetKey);
    if (!asset) return;

    const drawer = $("[data-drawer]");
    drawer.hidden = false;
    document.body.style.overflow = "hidden";

    $("[data-drawer-bucket]").textContent = (asset.bucket || "").replace(/_/g, " ").toUpperCase();
    $("[data-drawer-title]").textContent = `${asset.label} · ${asset.ticker || asset.key}`;
    const benchmark = asset.benchmark ? ` · vs ${asset.benchmark}` : "";
    const lowHist = asset.low_history ? " · ⚠ low history" : "";
    $("[data-drawer-sub]").textContent = `${asset.basis || "absolute"}${benchmark}${lowHist}`;

    const stats = $("[data-drawer-stats]");
    const conf = asset.confidence || {};
    const weight = state.lastWeightsByKey[asset.key];
    stats.innerHTML = `
      <div class="djg-drawer-stat"><span>Composite</span><strong>${signed(asset.composite_score, 2)}</strong></div>
      <div class="djg-drawer-stat"><span>E[Return]</span><strong>${signed(asset.expected_return, 2)}%</strong></div>
      <div class="djg-drawer-stat"><span>Avg T (raw)</span><strong>${signed(asset.avg_t_stat, 2)}</strong></div>
      <div class="djg-drawer-stat"><span>Avg T (HAC)</span><strong>${formatT(asset.avg_t_stat_hac)}</strong></div>
      <div class="djg-drawer-stat"><span>Confidence</span><strong>${(conf.label || '—').toUpperCase()}</strong></div>
      <div class="djg-drawer-stat"><span>Weight</span><strong>${weight != null ? (weight * 100).toFixed(1) + '%' : '—'}</strong></div>
    `;

    const tbody = $("[data-drawer-cells] tbody");
    tbody.innerHTML = "";
    (asset.disclosures || []).forEach((d) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${FACTOR_LABELS[d.factor] || d.factor}</td>
        <td>${formatNumber(d.correlation, 2)}</td>
        <td>${formatT(d.t_stat_hac)}</td>
        <td>${formatNumber(d.factor_value > 0 ? d.expected_contribution / Math.max(d.factor_value, 0.01) : null, 1)}%</td>
        <td>${formatNumber(d.factor_value < 0 ? d.expected_contribution / Math.max(-d.factor_value, 0.01) : null, 1)}%</td>
        <td>${d.n ?? "—"}</td>`;
      tbody.appendChild(tr);
    });
  }

  function closeDrawer() {
    $("[data-drawer]").hidden = true;
    document.body.style.overflow = "";
  }

  // ─── Toast ───────────────────────────────────────────────────────────────
  function toast(text) {
    const el = $("[data-toast]");
    el.textContent = text;
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 2400);
  }

  // ─── Formatters ──────────────────────────────────────────────────────────
  function signed(value, digits = 2) {
    if (value == null || !Number.isFinite(value)) return "—";
    const sign = value > 0 ? "+" : "";
    return `${sign}${value.toFixed(digits)}`;
  }
  function formatNumber(value, digits = 2) {
    if (value == null || !Number.isFinite(value)) return "—";
    return value.toFixed(digits);
  }
  function formatT(value) {
    if (value == null || !Number.isFinite(value)) return "—";
    return signed(value, 2);
  }
  function formatDir(dir, z) {
    if (!dir) return z != null ? signed(z, 2) : "—";
    const arrow = dir === "up" ? "↑" : dir === "down" ? "↓" : "•";
    return z != null ? `${arrow} ${signed(z, 2)}` : arrow;
  }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }
})();
