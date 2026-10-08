import { dashboardLink } from "./shared/api-client.js";
import { MESSAGE, STORAGE_KEYS } from "./shared/contracts.js";
import { $, $$, announce, clear, element, message } from "./shared/ui.js";

let appState = { settings: null, summary: null, preview: null, connection: "unpaired" };

const connectionCopy = {
  online: ["Connected", "Capture is ready. Nothing is uploaded until you confirm."],
  unpaired: ["Pair this browser", "Open Settings to enter a one-time pairing code."],
  offline: ["HERMES is offline", "Your pending capture stays on this device. Try again when the backend is reachable."],
  revoked: ["Device access revoked", "Pair again with a new one-time code."],
  unauthorized: ["Authorization required", "The token was rejected. Check this device in HERMES settings."],
  permission_denied: ["Permission denied", "Chrome or HERMES blocked this action. Review connection permissions."],
  version_mismatch: ["Update required", "This extension and the HERMES backend use different API versions."]
};

function setConnection(state, detail) {
  appState.connection = state;
  const copy = connectionCopy[state] || connectionCopy.offline;
  $("#connection").dataset.state = state;
  $("#connection-title").textContent = copy[0];
  $("#connection-detail").textContent = detail || copy[1];
}

function setView(name) {
  $$(".view").forEach((node) => { node.hidden = node.id !== `${name}-view`; });
  $$("[data-view]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.view === name)));
}

function queueItem(item, action) {
  const main = element("div", { className: "list-main" }, [element("strong", { text: item.name || item.title }), element("span", { text: item.status || item.detail || "" })]);
  const badge = element("span", { className: "pill", text: item.due || action });
  const row = element("button", { className: "list-item", attrs: { type: "button", "data-campaign": item.campaignId || "", "data-action": action } }, [main, badge]);
  return row;
}

function renderSummary(summary) {
  $("#notification-count").textContent = String(summary.notifications ?? 0);
  $("#review-count").textContent = String(summary.reviewQueue ?? 0);
  $("#followup-count").textContent = String(summary.followupsDue ?? 0);
  const attention = $("#attention-list");
  clear(attention);
  const items = [...(summary.reviewItems || []).slice(0, 2), ...(summary.followupItems || []).slice(0, 1)];
  if (!items.length) attention.append(element("div", { className: "empty", text: "Nothing needs attention." }));
  items.forEach((item) => attention.append(queueItem(item, item.due ? "scheduling" : "approval")));
  const reviews = $("#review-list");
  const followups = $("#followup-list");
  clear(reviews); clear(followups);
  (summary.reviewItems || []).forEach((item) => reviews.append(queueItem(item, "approval")));
  (summary.followupItems || []).forEach((item) => followups.append(queueItem(item, "scheduling")));
  if (!reviews.children.length) reviews.append(element("div", { className: "empty", text: "No drafts are waiting." }));
  if (!followups.children.length) followups.append(element("div", { className: "empty", text: "No follow-ups are due." }));
}

async function loadSummary() {
  if (!appState.settings.mockMode && !appState.settings.deviceToken) { setConnection("unpaired"); renderSummary({}); return; }
  try {
    const summary = await message(MESSAGE.api, { method: "summary" });
    appState.summary = summary;
    setConnection("online", appState.settings.mockMode ? "Demo transport · no backend calls" : undefined);
    renderSummary(summary);
  } catch (error) {
    setConnection(error.state || "offline", error.message);
    renderSummary({});
  }
}

async function renderCapture(rawCapture) {
  if (!rawCapture) { $("#capture-empty").hidden = false; $("#capture-preview").hidden = true; return; }
  setView("capture");
  $("#capture-empty").hidden = true;
  $("#capture-preview").hidden = false;
  $("#capture-title").textContent = rawCapture.title;
  $("#capture-url").textContent = rawCapture.url;
  $("#capture-kind").textContent = rawCapture.selection ? "Selected text and page metadata" : "Page metadata only — no page body";
  const excerpt = $("#capture-excerpt");
  excerpt.textContent = rawCapture.selection || rawCapture.description || "No description was available.";
  excerpt.hidden = !(rawCapture.selection || rawCapture.description);
  try {
    appState.preview = await message(MESSAGE.api, { method: "preview", args: [rawCapture] });
    setConnection("online", appState.settings.mockMode ? "Demo transport · no backend calls" : undefined);
    const warnings = $("#capture-warnings"); clear(warnings);
    for (const warning of appState.preview.warnings || []) warnings.append(element("div", { className: `warning${warning.blocking ? " blocking" : ""}` }, [element("p", { text: warning.message })]));
    const campaigns = appState.summary?.campaigns || (await message(MESSAGE.api, { method: "campaigns" })).campaigns || [];
    const select = $("#campaign-select"); clear(select);
    for (const campaign of campaigns) select.append(element("option", { text: campaign.name, attrs: { value: campaign.id } }));
    select.value = appState.preview.suggestedCampaignId || campaigns[0]?.id || "";
    $("#commit-capture").disabled = !campaigns.length || appState.preview.warnings?.some((warning) => warning.blocking);
  } catch (error) {
    setConnection(error.state || "offline", error.message);
    $("#commit-capture").disabled = true;
  }
}

async function takeCapture(kind) {
  try {
    const capture = await message(MESSAGE.capture, { kind });
    await renderCapture(capture);
    announce("Capture preview ready.");
  } catch (error) {
    setConnection(error.state || "permission_denied", error.message);
    announce(error.message);
  }
}

function openDashboard(action, campaignId = "") {
  if (!appState.settings?.baseUrl) return;
  chrome.tabs.create({ url: dashboardLink(appState.settings.baseUrl, action, campaignId ? { campaign: campaignId } : {}) });
}

$$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("#open-settings").addEventListener("click", () => message(MESSAGE.openOptions));
$("#capture-selection").addEventListener("click", () => takeCapture("selection"));
$("#capture-page").addEventListener("click", () => takeCapture("page"));
$("#discard-capture").addEventListener("click", async () => {
  await chrome.storage.local.remove(STORAGE_KEYS.pendingCapture);
  appState.preview = null;
  renderCapture(null);
  announce("Capture discarded.");
});
$("#commit-capture").addEventListener("click", async () => {
  const button = $("#commit-capture"); button.disabled = true;
  try {
    const result = await message(MESSAGE.api, { method: "commit", args: [appState.preview.previewId, $("#campaign-select").value] });
    await chrome.storage.local.remove(STORAGE_KEYS.pendingCapture);
    appState.preview = null;
    renderCapture(null);
    setView("overview");
    announce("Capture added to campaign.");
    openDashboard("campaign", result.campaignId);
  } catch (error) {
    setConnection(error.state || "offline", error.message);
    announce(error.message);
    button.disabled = false;
  }
});
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-deep-link], [data-action]");
  if (target) openDashboard(target.dataset.deepLink || target.dataset.action, target.dataset.campaign || "");
});

(async () => {
  const state = await message(MESSAGE.getState);
  appState.settings = state.settings;
  if (state.captureError) setConnection(state.captureError.state, state.captureError.message);
  await loadSummary();
  if (state.pendingCapture) await renderCapture(state.pendingCapture);
})();
