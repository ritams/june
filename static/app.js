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

function renderMetric(metric) {
  return `
    <article class="metric-card ${metric.status}">
      <div class="metric-head">
        <div class="metric-label">${metric.label}</div>
        <div class="metric-state">${metricStateLabel(metric.status)}</div>
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
