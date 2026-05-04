const signalBadge = document.querySelector("[data-signal-badge]");
const signalHeading = document.querySelector("[data-signal-heading]");
const signalSummary = document.querySelector("[data-signal-summary]");
const signalAction = document.querySelector("[data-signal-action]");
const updatedAt = document.querySelector("[data-updated-at]");
const notice = document.querySelector("[data-notice]");
const integrationsRoot = document.querySelector("[data-integrations]");
const pageDashboard = document.body.dataset.dashboard || null;

const sectionNodes = {
  liquidity: {
    status: document.querySelector("[data-liquidity-status]"),
    summary: document.querySelector("[data-liquidity-summary]"),
    metrics: document.querySelector("[data-liquidity-metrics]"),
  },
  "business-cycle": {
    status: document.querySelector("[data-cycle-status]"),
    summary: document.querySelector("[data-cycle-summary]"),
    metrics: document.querySelector("[data-cycle-metrics]"),
  },
};

const singlePageNodes = {
  title: document.querySelector("[data-panel-title]"),
  status: document.querySelector("[data-panel-status]"),
  summary: document.querySelector("[data-panel-summary]"),
  metrics: document.querySelector("[data-metrics]"),
};

const playbookNodes = {
  updated: document.querySelector("[data-playbook-updated]"),
  allocationHeadline: document.querySelector("[data-allocation-headline]"),
  allocationAction: document.querySelector("[data-allocation-action]"),
  allocationDeploy: document.querySelector("[data-allocation-deploy]"),
  allocationCash: document.querySelector("[data-allocation-cash]"),
  allocationFavor: document.querySelector("[data-allocation-favor]"),
  allocationTrim: document.querySelector("[data-allocation-trim]"),
  conviction: document.querySelector("[data-conviction-callout]"),
  cards: document.querySelector("[data-playbook-cards]"),
};

function metricStateLabel(status) {
  if (status === "positive") return "supportive";
  if (status === "negative") return "stress";
  return "watch";
}

function formatUpdatedAt(value) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function integrationLabel(name, enabled) {
  return `
    <div class="pill ${enabled ? "pill-enabled" : "pill-disabled"}">
      <span>${name}</span>
      <strong>${enabled ? "ready" : "off"}</strong>
    </div>
  `;
}

function formatZscore(z) {
  if (z === null || z === undefined || Number.isNaN(z)) return "";
  const sign = z > 0 ? "+" : "";
  const tone = Math.abs(z) >= 1 ? "z-strong" : "z-mild";
  return `<span class="z-pill ${tone}" title="5-yr rolling z-score">z ${sign}${z.toFixed(2)}</span>`;
}

function renderMetric(metric) {
  const zscore = metric.details && typeof metric.details.zscore === "number" ? metric.details.zscore : null;
  return `
    <article class="metric-card ${metric.status}">
      <div class="metric-head">
        <div class="metric-label">${metric.label}</div>
        <div class="metric-head-right">
          ${formatZscore(zscore)}
          <div class="metric-state">${metricStateLabel(metric.status)}</div>
        </div>
      </div>
      <div class="metric-copy">
        <h4 class="metric-value">${metric.display_value}</h4>
        <p class="metric-summary">${metric.summary}</p>
        ${metric.secondary ? `<p class="metric-secondary">${metric.secondary}</p>` : ""}
      </div>
      <div class="metric-updated">${metric.updated_at ? `Updated ${formatUpdatedAt(metric.updated_at)}` : "Live source pending"}</div>
    </article>
  `;
}

function renderConfidenceBadge(confidence) {
  if (!confidence) return "";
  return `<span class="confidence-badge ${confidence.tone}">${confidence.label}</span>`;
}

function renderPlaybookTable(card) {
  const horizonKeys = [];
  card.assets.forEach((asset) => {
    Object.keys(asset.horizons).forEach((key) => {
      if (!horizonKeys.includes(key)) horizonKeys.push(key);
    });
  });
  const orderedHorizons = horizonKeys.sort((left, right) => Number(left) - Number(right));
  const headers = orderedHorizons.map((key) => `<th>${card.assets[0]?.horizons[key]?.label || key}</th>`).join("");
  const rows = card.assets
    .map((asset) => {
      const cells = orderedHorizons
        .map((key) => {
          const entry = asset.horizons[key];
          if (!entry) return "<td>n/a</td>";
          const valueClass = entry.missing ? "insufficient" : entry.limited ? "limited-sample" : "";
          const sampleTitle = entry.n ? ` title="${entry.n} complete ${entry.n === 1 ? "case" : "cases"}"` : "";
          return `<td class="${valueClass}"${sampleTitle}>${entry.display_avg}</td>`;
        })
        .join("");
      const winRateKey = asset.horizons["365"] ? "365" : orderedHorizons[0];
      const winRateEntry = asset.horizons[winRateKey];
      const winRate = winRateEntry?.display_win_rate || "n/a";
      const winRateClass = winRateEntry?.missing ? "insufficient" : winRateEntry?.limited ? "limited-sample" : "";
      const winRateTitle = winRateEntry?.n
        ? ` title="${winRateEntry.n} complete ${winRateEntry.n === 1 ? "case" : "cases"}"`
        : "";
      const tStatEntry = asset.horizons[winRateKey];
      const tStatDisplay = tStatEntry?.display_t_stat || "n/a";
      const tStatClass = tStatEntry?.missing ? "insufficient" : tStatEntry?.t_stat && Math.abs(tStatEntry.t_stat) >= 2 ? "t-strong" : "";
      return `
        <tr>
          <td class="asset-cell">
            <div class="asset-line">
              <span class="asset-name">${asset.label}</span>
              ${renderConfidenceBadge(asset.confidence)}
            </div>
            ${asset.warning ? `<p class="asset-warning">${asset.warning}</p>` : ""}
          </td>
          ${cells}
          <td class="${winRateClass}"${winRateTitle}>${winRate}</td>
          <td class="${tStatClass}" title="t-statistic on the 1yr forward returns">${tStatDisplay}</td>
        </tr>
      `;
    })
    .join("");

  return `
    <table class="playbook-table">
      <thead>
        <tr>
          <th>Asset</th>
          ${headers}
          <th>Win%(1yr)</th>
          <th>T-stat(1yr)</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderPlaybookCard(card) {
  if (!card.available) {
    return `
      <article class="playbook-card">
        <div class="playbook-card-header">
          <div>
            <div class="eyebrow">Signal</div>
            <h4>${card.label}</h4>
            <p>${card.subtitle}</p>
          </div>
        </div>
        <p class="playbook-empty">${card.warning}</p>
      </article>
    `;
  }
  return `
    <article class="playbook-card">
      <div class="playbook-card-header">
        <div>
          <div class="eyebrow">Signal</div>
          <h4>${card.label}</h4>
          <p>${card.subtitle}</p>
        </div>
        <div class="playbook-count">${card.event_count} events</div>
      </div>
      ${card.callout ? `<p class="playbook-card-callout">${card.callout}</p>` : ""}
      ${renderPlaybookTable(card)}
      ${card.warning ? `<p class="playbook-warning">${card.warning}</p>` : ""}
    </article>
  `;
}

function renderPlaybook(playbook) {
  if (!playbookNodes.cards || !playbook) return;
  playbookNodes.updated.textContent = playbook.last_calculated
    ? `Cache ${playbook.stale ? "stale" : "ready"} · ${playbook.last_calculated}`
    : "Cache pending";
  playbookNodes.allocationHeadline.textContent = `${playbook.allocation.key.replace("_", " ")} allocation`;
  playbookNodes.allocationAction.textContent = playbook.allocation.action;
  playbookNodes.allocationDeploy.textContent = `${playbook.allocation.deploy_pct}%`;
  playbookNodes.allocationCash.textContent = `${playbook.allocation.cash_pct}%`;
  playbookNodes.allocationFavor.textContent = `Favor: ${playbook.allocation.favor}`;
  playbookNodes.allocationTrim.textContent = `Trim: ${playbook.allocation.trim}`;
  playbookNodes.conviction.textContent =
    playbook.conviction || "Conviction callout will appear once enough historical windows have elapsed.";
  playbookNodes.cards.innerHTML = playbook.cards.map(renderPlaybookCard).join("");
}

function setNotice(message) {
  if (!notice) return;
  notice.textContent = message;
}

function renderSection(section) {
  const nodes = sectionNodes[section.slug];
  if (!nodes) return;
  nodes.status.textContent = section.status;
  nodes.status.className = `panel-status ${section.tone}`;
  nodes.summary.textContent = section.summary;
  nodes.metrics.innerHTML = section.metrics.map(renderMetric).join("");
}

async function loadCombinedDashboard(force = false) {
  const response = await fetch(`/api/snapshot?force=${force}`);
  if (!response.ok) {
    throw new Error(`Failed to load dashboard: ${response.status}`);
  }
  const payload = await response.json();
  const { signal, dashboards, generated_at, integrations } = payload;

  signalBadge.textContent = signal.label;
  signalBadge.className = `signal-badge ${signal.tone}`;
  signalHeading.textContent = signal.label;
  signalSummary.textContent = signal.summary;
  signalAction.textContent = signal.action;
  updatedAt.textContent = formatUpdatedAt(generated_at);

  Object.values(dashboards).forEach(renderSection);

  integrationsRoot.innerHTML = [
    integrationLabel("Telegram", integrations.telegram),
    integrationLabel("Google Sheets", integrations.google_sheets),
    integrationLabel("Perplexity", integrations.perplexity),
    integrationLabel("Global M2 Proxy", integrations.global_m2_proxy),
  ].join("");
}

async function loadSectionDashboard(force = false) {
  const response = await fetch(`/api/dashboard/${pageDashboard}?force=${force}`);
  if (!response.ok) {
    throw new Error(`Failed to load dashboard: ${response.status}`);
  }
  const payload = await response.json();
  const { signal, dashboard, generated_at, integrations, playbook } = payload;

  signalBadge.textContent = signal.label;
  signalBadge.className = `signal-badge ${signal.tone}`;
  signalHeading.textContent = dashboard.title;
  signalSummary.textContent = signal.summary;
  if (signalAction) signalAction.textContent = signal.action;
  updatedAt.textContent = formatUpdatedAt(generated_at);

  if (singlePageNodes.title) singlePageNodes.title.textContent = dashboard.title;
  if (singlePageNodes.status) {
    singlePageNodes.status.textContent = dashboard.status;
    singlePageNodes.status.className = `panel-status ${dashboard.tone}`;
  }
  if (singlePageNodes.summary) singlePageNodes.summary.textContent = dashboard.summary;
  if (singlePageNodes.metrics) singlePageNodes.metrics.innerHTML = dashboard.metrics.map(renderMetric).join("");
  renderPlaybook(playbook);

  integrationsRoot.innerHTML = [
    integrationLabel("Telegram", integrations.telegram),
    integrationLabel("Google Sheets", integrations.google_sheets),
    integrationLabel("Perplexity", integrations.perplexity),
    integrationLabel("Global M2 Proxy", integrations.global_m2_proxy),
  ].join("");
}

async function postAction(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) {
    throw new Error(`Action failed: ${response.status}`);
  }
  return response.json();
}

document.querySelector("[data-refresh]")?.addEventListener("click", async () => {
  try {
    setNotice("Refreshing live data...");
    await postAction("/api/actions/refresh");
    if (pageDashboard) {
      await loadSectionDashboard(true);
    } else {
      await loadCombinedDashboard(true);
    }
    setNotice("Dashboard refreshed.");
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("[data-send-card]")?.addEventListener("click", async () => {
  try {
    setNotice("Sending daily card...");
    const payload = await postAction("/api/actions/send-daily-card");
    if (pageDashboard) {
      await loadSectionDashboard(true);
    } else {
      await loadCombinedDashboard(true);
    }
    setNotice(`Daily card sent. Telegram: ${payload.telegram_sent}. Sheets: ${payload.sheets_logged}.`);
  } catch (error) {
    setNotice(error.message);
  }
});

const loader = pageDashboard ? loadSectionDashboard : loadCombinedDashboard;

loader().catch((error) => {
  setNotice(error.message);
});

window.setInterval(() => {
  loader().catch((error) => {
    setNotice(error.message);
  });
}, 60000);

/* ============================================================
   Scenario panel — Bittel MIT engine
   ============================================================ */

const scenarioPanel = document.querySelector("[data-scenario-panel]");
const scenarioState = {
  factorKeys: ["risk_on_off", "growth", "inflation", "short_rates", "liquidity", "dollar", "oil"],
  factorLabels: {
    risk_on_off: "Risk On/Off",
    growth: "Growth",
    inflation: "Inflation",
    short_rates: "Short Rates",
    liquidity: "Liquidity",
    dollar: "Dollar",
    oil: "Oil",
  },
  presets: null,
  values: {},
  activePreset: null,
};

const bucketLabels = {
  asset_class: "Asset classes",
  equity_region: "Equity regions",
  equity_sector: "Equity sectors",
  fixed_income: "Fixed income",
  currency: "Currencies",
  commodity: "Commodities",
  style: "Style factors",
  crypto: "Crypto",
};

const scenarioNodes = scenarioPanel
  ? {
      panel: scenarioPanel,
      updated: scenarioPanel.querySelector("[data-scenario-updated]"),
      presets: scenarioPanel.querySelector("[data-scenario-presets]"),
      sliders: scenarioPanel.querySelector("[data-scenario-sliders]"),
      buckets: scenarioPanel.querySelector("[data-scenario-buckets]"),
      heatmap: scenarioPanel.querySelector("[data-scenario-heatmap]"),
      autoBtn: scenarioPanel.querySelector("[data-scenario-auto]"),
      resetBtn: scenarioPanel.querySelector("[data-scenario-reset]"),
      recalcBtn: scenarioPanel.querySelector("[data-scenario-recalc]"),
    }
  : null;

function fmtSliderValue(v) {
  const sign = v > 0 ? "+" : "";
  return `${sign}${Number(v).toFixed(2)}`;
}

function renderScenarioSliders() {
  if (!scenarioNodes) return;
  scenarioNodes.sliders.innerHTML = scenarioState.factorKeys
    .map((key) => {
      const value = scenarioState.values[key] ?? 0;
      return `
        <label class="slider-row" data-factor="${key}">
          <div class="slider-label">
            <span>${scenarioState.factorLabels[key]}</span>
            <span class="slider-value" data-slider-value="${key}">${fmtSliderValue(value)}</span>
          </div>
          <input type="range" min="-1" max="1" step="0.05" value="${value}" data-slider="${key}" />
          <div class="slider-scale"><span>−1</span><span>0</span><span>+1</span></div>
        </label>
      `;
    })
    .join("");
  scenarioNodes.sliders.querySelectorAll("input[type=range]").forEach((input) => {
    input.addEventListener("input", (event) => {
      const key = event.target.dataset.slider;
      const value = parseFloat(event.target.value);
      scenarioState.values[key] = value;
      const display = scenarioNodes.sliders.querySelector(`[data-slider-value="${key}"]`);
      if (display) display.textContent = fmtSliderValue(value);
    });
    input.addEventListener("change", () => {
      scenarioState.activePreset = null;
      highlightPreset(null);
      runScenario();
    });
  });
}

function renderScenarioPresets() {
  if (!scenarioNodes || !scenarioState.presets) return;
  const entries = Object.entries(scenarioState.presets);
  scenarioNodes.presets.innerHTML = entries
    .map(([key, preset]) => `<button type="button" class="preset-chip" data-preset="${key}">${preset.label}</button>`)
    .join("");
  scenarioNodes.presets.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.preset;
      const preset = scenarioState.presets[key];
      scenarioState.factorKeys.forEach((factorKey) => {
        scenarioState.values[factorKey] = preset[factorKey] ?? 0;
      });
      scenarioState.activePreset = key;
      highlightPreset(key);
      renderScenarioSliders();
      runScenario();
    });
  });
}

function highlightPreset(key) {
  if (!scenarioNodes) return;
  scenarioNodes.presets.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.preset === key);
  });
}

function buildScenarioQuery() {
  return scenarioState.factorKeys.map((key) => `${key}=${scenarioState.values[key] ?? 0}`).join("&");
}

function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function fmtT(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  const sign = v > 0 ? "+" : "";
  return `t ${sign}${v.toFixed(2)}`;
}

function tToneClass(v) {
  if (v === null || v === undefined) return "";
  const a = Math.abs(v);
  if (a >= 3) return "t-strong";
  if (a >= 2) return "t-medium";
  if (a >= 1) return "t-mild";
  return "t-weak";
}

function confidenceDots(t) {
  if (t === null || t === undefined || Number.isNaN(t)) return "";
  const a = Math.abs(t);
  const filled = a >= 3 ? 3 : a >= 2 ? 2 : a >= 1 ? 1 : 0;
  const dots = Array.from({ length: 3 }, (_, i) =>
    `<span class="dot ${i < filled ? "dot-on" : "dot-off"}"></span>`
  ).join("");
  return `<span class="confidence-dots" title="t-stat ${t >= 0 ? "+" : ""}${t.toFixed(2)} — ${filled === 3 ? "strong" : filled === 2 ? "decent" : filled === 1 ? "weak" : "noise"}">${dots}</span>`;
}

function renderRanking(title, list, polarity, kind) {
  const rows = list
    .map((item, idx) => `
      <li class="rank-row">
        <span class="rank-num">${idx + 1}</span>
        <span class="rank-asset" title="${item.label}">${item.label}</span>
        <span class="rank-ret ${polarity > 0 ? "ret-pos" : "ret-neg"}">${fmtPct(item.expected_return)}</span>
        ${confidenceDots(item.avg_t_stat)}
      </li>
    `)
    .join("");
  return `
    <div class="rank-block ${kind}">
      <div class="rank-title">
        <span class="rank-arrow">${polarity > 0 ? "↑" : "↓"}</span>
        ${title}
      </div>
      <ol class="rank-list">${rows}</ol>
    </div>
  `;
}

function renderBuckets(buckets) {
  const order = ["asset_class", "equity_region", "equity_sector", "fixed_income", "currency", "commodity", "style", "crypto"];
  return order
    .filter((bucketKey) => buckets[bucketKey])
    .map((bucketKey) => {
      const bucket = buckets[bucketKey];
      const top = bucket.top_3 || [];
      const bottom = bucket.bottom_3 || [];
      const tiny = bucket.assets.length <= 3;
      const n = bucket.assets.length;
      return `
        <article class="bucket-card${tiny ? " bucket-card-tiny" : ""}">
          <header class="bucket-head">
            <div class="bucket-name">${bucketLabels[bucketKey] || bucketKey}</div>
            <div class="bucket-meta">${n} ${n === 1 ? "asset" : "assets"}</div>
          </header>
          ${renderRanking("Own these", top, +1, "rank-own")}
          ${tiny ? "" : renderRanking("Avoid these", bottom, -1, "rank-avoid")}
        </article>
      `;
    })
    .join("");
}

function renderHeatmap(heatMap) {
  if (!scenarioNodes || !heatMap) return;
  const factors = heatMap.factors || [];
  const rows = heatMap.rows || [];
  const headers = factors.map((f) => `<th>${f.label}</th>`).join("");
  const body = rows
    .map((row) => {
      const cells = factors
        .map((f) => {
          const cell = row[f.key];
          if (!cell || cell.correlation === null) return `<td class="hm-empty">·</td>`;
          const corr = cell.correlation;
          const t = cell.t_stat;
          const intensity = Math.min(1, Math.abs(corr));
          const tone = corr >= 0 ? "hm-pos" : "hm-neg";
          return `<td class="${tone}" style="--alpha:${intensity.toFixed(2)}" title="corr ${corr.toFixed(2)} | t ${t?.toFixed?.(2) ?? "n/a"}">${corr.toFixed(2)}</td>`;
        })
        .join("");
      return `<tr><td class="hm-asset">${row.label}<span class="hm-bucket">${bucketLabels[row.bucket] || row.bucket}</span></td>${cells}</tr>`;
    })
    .join("");
  scenarioNodes.heatmap.innerHTML = `
    <table class="heatmap-table">
      <thead><tr><th>Asset</th>${headers}</tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

async function runScenario() {
  if (!scenarioNodes) return;
  try {
    const response = await fetch(`/api/scenario?${buildScenarioQuery()}`);
    if (!response.ok) throw new Error(`Scenario request failed: ${response.status}`);
    const payload = await response.json();
    if (!payload.available) {
      scenarioNodes.buckets.innerHTML = `<div class="empty-state">Correlation matrix not yet built. Click <strong>Recalculate matrix</strong> to build it (1–2 minutes).</div>`;
      scenarioNodes.updated.textContent = "Cache pending";
      return;
    }
    scenarioNodes.updated.textContent = `Matrix ${payload.last_calculated}`;
    scenarioNodes.buckets.innerHTML = renderBuckets(payload.buckets);
    renderHeatmap(payload.heat_map);
  } catch (err) {
    setNotice(err.message);
  }
}

async function loadScenarioPresets() {
  const response = await fetch("/api/scenario/presets");
  if (!response.ok) throw new Error("Failed to load presets");
  const payload = await response.json();
  scenarioState.presets = payload.presets;
  payload.factor_keys.forEach((k) => {
    scenarioState.values[k] = scenarioState.values[k] ?? 0;
  });
  renderScenarioPresets();
  renderScenarioSliders();
}

async function autoFillScenario() {
  const response = await fetch("/api/scenario?auto=true");
  if (!response.ok) throw new Error("Auto-fill failed");
  const payload = await response.json();
  if (payload.scenario) {
    Object.entries(payload.scenario).forEach(([k, v]) => {
      scenarioState.values[k] = v;
    });
    scenarioState.activePreset = null;
    highlightPreset(null);
    renderScenarioSliders();
  }
  if (payload.available) {
    scenarioNodes.updated.textContent = `Matrix ${payload.last_calculated}`;
    scenarioNodes.buckets.innerHTML = renderBuckets(payload.buckets);
    renderHeatmap(payload.heat_map);
  } else {
    scenarioNodes.updated.textContent = "Cache pending";
    scenarioNodes.buckets.innerHTML = `<div class="empty-state">Correlation matrix not yet built. Click <strong>Recalculate matrix</strong> to build it (~1 minute).</div>`;
  }
}

if (scenarioNodes) {
  scenarioNodes.autoBtn?.addEventListener("click", () => {
    autoFillScenario().catch((e) => setNotice(e.message));
  });
  scenarioNodes.resetBtn?.addEventListener("click", () => {
    scenarioState.factorKeys.forEach((k) => (scenarioState.values[k] = 0));
    scenarioState.activePreset = null;
    highlightPreset(null);
    renderScenarioSliders();
    runScenario();
  });
  scenarioNodes.recalcBtn?.addEventListener("click", async () => {
    try {
      setNotice("Building correlation matrix — this takes 1–2 minutes…");
      await postAction("/api/actions/recalculate-correlations");
      setNotice("Correlation matrix rebuilt.");
      await runScenario();
    } catch (e) {
      setNotice(e.message);
    }
  });

  loadScenarioPresets()
    .then(() => autoFillScenario())
    .catch((e) => setNotice(e.message));
}

/* ============================================================
   Cycle quadrant + sparklines
   ============================================================ */

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function seasonName(growthZ, inflationZ) {
  if (growthZ >= 0 && inflationZ < 0) return "Spring";
  if (growthZ >= 0 && inflationZ >= 0) return "Summer";
  if (growthZ < 0 && inflationZ >= 0) return "Fall";
  return "Winter";
}

function renderQuadrant(growthZ, inflationZ) {
  const node = document.querySelector("[data-cycle-quadrant]");
  if (!node) return;
  const labelNode = document.querySelector("[data-quadrant-label]");
  const gx = clamp(inflationZ ?? 0, -1, 1);
  const gy = clamp(growthZ ?? 0, -1, 1);
  const cx = 50 + gx * 40;
  const cy = 50 - gy * 40;
  const season = seasonName(growthZ ?? 0, inflationZ ?? 0);
  if (labelNode) labelNode.textContent = season;
  node.innerHTML = `
    <svg viewBox="0 0 100 100" class="quadrant-svg">
      <rect x="0" y="0" width="50" height="50" class="q-spring"/>
      <rect x="50" y="0" width="50" height="50" class="q-summer"/>
      <rect x="50" y="50" width="50" height="50" class="q-fall"/>
      <rect x="0" y="50" width="50" height="50" class="q-winter"/>
      <line x1="50" y1="0" x2="50" y2="100" class="q-axis"/>
      <line x1="0" y1="50" x2="100" y2="50" class="q-axis"/>
      <text x="25" y="14" class="q-label">SPRING</text>
      <text x="75" y="14" class="q-label">SUMMER</text>
      <text x="75" y="92" class="q-label">FALL</text>
      <text x="25" y="92" class="q-label">WINTER</text>
      <text x="50" y="99" class="q-axis-label" text-anchor="middle">inflation →</text>
      <text x="2"  y="50" class="q-axis-label" transform="rotate(-90 2 50)">growth →</text>
      <circle cx="${cx.toFixed(2)}" cy="${cy.toFixed(2)}" r="3.4" class="q-dot"/>
    </svg>
  `;
}

function buildSparkPath(values, width, height, padding) {
  if (!values.length) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const w = width - padding * 2;
  const h = height - padding * 2;
  return values
    .map((v, i) => {
      const x = padding + (i / (values.length - 1)) * w;
      const y = padding + h - ((v - min) / range) * h;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

async function renderSparkline(seriesKey, ref50 = null) {
  const node = document.querySelector("[data-cycle-spark]");
  if (!node) return;
  try {
    const res = await fetch(`/api/history/${seriesKey}?months=60`);
    if (!res.ok) return;
    const data = await res.json();
    const points = data.points || [];
    if (points.length < 2) return;
    const values = points.map((p) => p.value);
    const path = buildSparkPath(values, 600, 120, 8);
    const min = Math.min(...values).toFixed(1);
    const max = Math.max(...values).toFixed(1);
    const last = values[values.length - 1].toFixed(1);
    let refLine = "";
    if (ref50 !== null) {
      const yMin = Math.min(...values, ref50);
      const yMax = Math.max(...values, ref50);
      const range = yMax - yMin || 1;
      const y = 8 + (120 - 16) - ((ref50 - yMin) / range) * (120 - 16);
      refLine = `<line x1="8" y1="${y.toFixed(2)}" x2="592" y2="${y.toFixed(2)}" class="spark-ref"/>`;
    }
    node.innerHTML = `
      <svg viewBox="0 0 600 120" class="spark-svg">
        ${refLine}
        <path d="${path}" class="spark-path"/>
      </svg>
      <div class="spark-meta">
        <span>Range ${min} → ${max}</span>
        <span>Latest <strong>${last}</strong></span>
        <span>${points[0].date} → ${points[points.length - 1].date}</span>
      </div>
    `;
  } catch (e) {
    /* silent */
  }
}

/* Refresh quadrant whenever scenario state changes */
const _quadrantObserver = setInterval(() => {
  if (scenarioState.values.growth !== undefined) {
    renderQuadrant(scenarioState.values.growth, scenarioState.values.inflation);
  }
}, 1000);

if (pageDashboard === "business-cycle") {
  renderSparkline("ism");
}
if (pageDashboard === "liquidity") {
  renderSparkline("m2");
}
