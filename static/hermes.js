/* Hermes CIO View + What-If Outcome — small standalone script.
   Independent of djg.js so the existing dashboard can't regress from this. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const fmtPct = (v, digits = 1) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${(v * 100).toFixed(digits)}%`;
  };
  const fmtGBP = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 0 }).format(v);
  };

  document.addEventListener("DOMContentLoaded", () => {
    bootHermes().catch((err) => console.error("Hermes boot failed", err));
    bootWhatIf().catch((err) => console.error("What-If boot failed", err));
  });

  async function bootHermes() {
    try {
      const res = await fetch("/api/hermes/state");
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      renderHermes(data);
    } catch (err) {
      console.error("Hermes state fetch failed", err);
      $("[data-hermes-summary]").textContent = "Hermes state unavailable — check backend.";
    }
  }

  function renderHermes(state) {
    $("[data-hermes-stance]").textContent = state.stance || "—";
    $("[data-hermes-score]").textContent = state.risk_budget;
    $("[data-hermes-deploy]").textContent = `${state.deploy_pct}%`;
    $("[data-hermes-cash]").textContent = `${state.cash_pct}%`;
    $("[data-hermes-season]").textContent = state.macro_season || "—";
    $("[data-hermes-liquidity]").textContent = state.liquidity_state || "—";
    $("[data-hermes-cycle]").textContent = state.cycle_state || "—";
    $("[data-hermes-confidence]").textContent = state.confidence || "—";
    $("[data-hermes-updated]").textContent = state.last_updated
      ? `Updated ${state.last_updated.replace("T", " ")}`
      : "—";
    $("[data-hermes-summary]").textContent = state.summary || "";
    $("[data-hermes-mit]").textContent = state.mit_overlay || "";
    $("[data-hermes-slr]").textContent = state.slr_note || "";

    const fill = $("[data-hermes-score-fill]");
    if (fill) {
      fill.style.width = `${Math.max(0, Math.min(100, state.risk_budget))}%`;
      fill.dataset.band = state.stance.replace(/\s+/g, "-").toLowerCase();
    }
  }

  // ─── What-If panel ─────────────────────────────────────────────────────────

  async function bootWhatIf() {
    const form = $("[data-whatif-form]");
    if (!form) return;
    const select = $("[data-whatif-target]");
    const endInput = $("[data-whatif-end]");
    // Default end = today
    if (endInput && !endInput.value) endInput.value = new Date().toISOString().slice(0, 10);

    try {
      const res = await fetch("/api/hermes/whatif/options");
      const opts = await res.json();
      // Build a flat options list. Single assets first, then baskets, then Framework Portfolio.
      const groups = [
        { label: "Single Asset", items: opts.assets, prefix: "asset:" },
        { label: "Basket", items: opts.baskets, prefix: "basket:" },
      ];
      for (const group of groups) {
        const og = document.createElement("optgroup");
        og.label = group.label;
        for (const item of group.items) {
          const opt = document.createElement("option");
          opt.value = `${group.prefix}${item.key}`;
          opt.textContent = item.label;
          og.appendChild(opt);
        }
        select.appendChild(og);
      }
      const fp = document.createElement("option");
      fp.value = `basket:${opts.framework_portfolio_key}`;
      fp.textContent = "Framework Portfolio (this dashboard's allocation)";
      select.appendChild(fp);
      // Default to SPY for a fast first run
      select.value = "asset:SPY";
    } catch (err) {
      console.error("What-If options fetch failed", err);
    }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const targetValue = select.value;
      const [kind, key] = targetValue.split(":");
      const amount = parseFloat($("[data-whatif-amount]").value || "100000");
      const start = $("[data-whatif-start]").value;
      const end = endInput.value;
      const params = new URLSearchParams({ amount, start_date: start, end_date: end });
      if (kind === "asset") params.set("asset_key", key);
      else params.set("basket_key", key);

      const submitBtn = form.querySelector("button[type=submit]");
      submitBtn.disabled = true;
      submitBtn.textContent = "Running…";
      try {
        const res = await fetch(`/api/hermes/whatif?${params}`, { method: "POST" });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(detail.detail || `status ${res.status}`);
        }
        const data = await res.json();
        renderWhatIf(data);
      } catch (err) {
        console.error("What-If run failed", err);
        const resultEl = $("[data-whatif-result]");
        resultEl.hidden = false;
        $("[data-whatif-warnings]").innerHTML = `<div class="hermes-warning">${err.message}</div>`;
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Run";
      }
    });
  }

  function renderWhatIf(data) {
    $("[data-whatif-result]").hidden = false;
    // Single-asset path vs Framework Portfolio path share most fields.
    $("[data-whatif-ending]").textContent = fmtGBP(data.ending_value);
    $("[data-whatif-total]").textContent = fmtPct(data.total_return);
    $("[data-whatif-cagr]").textContent = fmtPct(data.annualised_return);
    $("[data-whatif-mdd]").textContent = fmtPct(data.max_drawdown);
    $("[data-whatif-best]").textContent = data.best_month != null ? fmtPct(data.best_month) : (data.best_12m != null ? fmtPct(data.best_12m) + " (12m)" : "—");
    $("[data-whatif-worst]").textContent = data.worst_month != null ? fmtPct(data.worst_month) : (data.worst_12m != null ? fmtPct(data.worst_12m) + " (12m)" : "—");

    const warnings = data.warnings || [];
    const warnEl = $("[data-whatif-warnings]");
    warnEl.innerHTML = warnings.map((w) => `<div class="hermes-warning">${w}</div>`).join("");

    drawCurve(data.equity_curve || []);
  }

  function drawCurve(curve) {
    const svg = $("[data-whatif-chart]");
    if (!svg || curve.length < 2) {
      if (svg) svg.innerHTML = "";
      return;
    }
    const values = curve.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const W = 400, H = 100;
    const xScale = (i) => (i / (curve.length - 1)) * W;
    const yScale = (v) => H - ((v - min) / Math.max(max - min, 1e-9)) * H;
    let path = `M ${xScale(0).toFixed(2)} ${yScale(values[0]).toFixed(2)}`;
    for (let i = 1; i < curve.length; i++) {
      path += ` L ${xScale(i).toFixed(2)} ${yScale(values[i]).toFixed(2)}`;
    }
    svg.innerHTML = `<path d="${path}" fill="none" stroke="currentColor" stroke-width="1.5" />`;
  }
})();
