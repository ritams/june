const dashboardSlug = document.body.dataset.dashboard;
const signalBadge = document.querySelector("[data-signal-badge]");
const signalHeading = document.querySelector("[data-signal-heading]");
const signalSummary = document.querySelector("[data-signal-summary]");
const updatedAt = document.querySelector("[data-updated-at]");
const panelTitle = document.querySelector("[data-panel-title]");
const panelStatus = document.querySelector("[data-panel-status]");
const metricsRoot = document.querySelector("[data-metrics]");
const integrationsRoot = document.querySelector("[data-integrations]");
const notice = document.querySelector("[data-notice]");

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

function setNotice(message) {
  if (!notice) return;
  notice.textContent = message;
}

async function loadDashboard(force = false) {
  const response = await fetch(`/api/dashboard/${dashboardSlug}?force=${force}`);
  if (!response.ok) {
    throw new Error(`Failed to load dashboard: ${response.status}`);
  }
  const payload = await response.json();
  const { signal, dashboard, generated_at, integrations } = payload;
  signalBadge.textContent = signal.label;
  signalBadge.className = `signal-badge ${signal.tone}`;
  signalHeading.textContent = dashboard.title;
  signalSummary.textContent = signal.summary;
  updatedAt.textContent = formatUpdatedAt(generated_at);
  panelTitle.textContent = dashboard.title;
  panelStatus.textContent = dashboard.status;
  panelStatus.className = `panel-status ${dashboard.tone}`;
  metricsRoot.innerHTML = dashboard.metrics.map(renderMetric).join("");
  integrationsRoot.innerHTML = [
    integrationLabel("Telegram", integrations.telegram),
    integrationLabel("Google Sheets", integrations.google_sheets),
    integrationLabel("Official ISM", integrations.ism_configured),
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
    await loadDashboard(true);
    setNotice("Dashboard refreshed.");
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("[data-send-card]")?.addEventListener("click", async () => {
  try {
    setNotice("Sending daily card...");
    const payload = await postAction("/api/actions/send-daily-card");
    await loadDashboard(true);
    setNotice(`Daily card sent. Telegram: ${payload.telegram_sent}. Sheets: ${payload.sheets_logged}.`);
  } catch (error) {
    setNotice(error.message);
  }
});

loadDashboard().catch((error) => {
  setNotice(error.message);
});

window.setInterval(() => {
  loadDashboard().catch((error) => {
    setNotice(error.message);
  });
}, 60000);
