import { HermesClient, connectionStateFor } from "./shared/api-client.js";
import { MESSAGE, STORAGE_KEYS } from "./shared/contracts.js";
import { normalizeCapture, readPageMetadata } from "./shared/capture.js";
import { MockTransport } from "./mock/mock-transport.js";

const DEFAULT_SETTINGS = { baseUrl: "http://127.0.0.1:8000", deviceToken: null, mockMode: false };
const BADGE_ALARM = "hermes-summary";

async function settings() {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.settings);
  return { ...DEFAULT_SETTINGS, ...(stored[STORAGE_KEYS.settings] || {}) };
}

async function saveSettings(next) {
  await chrome.storage.local.set({ [STORAGE_KEYS.settings]: { ...DEFAULT_SETTINGS, ...next } });
}

async function transport() {
  const value = await settings();
  return value.mockMode ? new MockTransport(chrome.storage.local) : new HermesClient(value);
}

async function callTransport(method, args = []) {
  const client = await transport();
  if (typeof client[method] !== "function") throw new Error("Unsupported HERMES action.");
  return client[method](...args);
}

function safeError(error) {
  return { ok: false, error: { state: connectionStateFor(error), message: error?.message || "HERMES request failed." } };
}

async function storeCapture(capture) {
  const normalized = normalizeCapture(capture);
  await chrome.storage.local.set({ [STORAGE_KEYS.pendingCapture]: normalized });
  await chrome.storage.local.remove("captureError");
  return normalized;
}

async function captureTab(tab, kind = "page", selection = "") {
  if (!tab?.id || !tab.url) throw Object.assign(new Error("No active browser tab is available."), { state: "permission_denied" });
  if (kind === "selection" && selection) return storeCapture({ kind, url: tab.url, title: tab.title, selection });
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readPageMetadata });
    return storeCapture({ ...result, kind: result.selection ? "selection" : "page" });
  } catch {
    throw Object.assign(new Error("Chrome does not allow capture on this page. Open a regular website and try again."), { state: "permission_denied" });
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

async function openPanel(tab) {
  if (tab?.windowId) await chrome.sidePanel.open({ windowId: tab.windowId });
}

async function updateBadge({ notify = false } = {}) {
  try {
    const cfg = await settings();
    if (!cfg.mockMode && !cfg.deviceToken) {
      await chrome.action.setBadgeText({ text: "" });
      return;
    }
    const summary = await callTransport("summary");
    const count = Number(summary.reviewQueue || 0) + Number(summary.followupsDue || 0);
    await chrome.action.setBadgeBackgroundColor({ color: count ? "#D45B32" : "#315F4A" });
    await chrome.action.setBadgeText({ text: count ? String(Math.min(count, 99)) : "" });
    const prior = (await chrome.storage.session.get("badgeCount")).badgeCount || 0;
    await chrome.storage.session.set({ badgeCount: count });
    if (notify && count > prior) {
      await chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title: "HERMES needs your attention", message: `${count} review or follow-up item${count === 1 ? "" : "s"} waiting.` });
    }
  } catch {
    await chrome.action.setBadgeBackgroundColor({ color: "#826F62" });
    await chrome.action.setBadgeText({ text: "!" });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "capture-selection", title: "Capture selection in HERMES", contexts: ["selection"] });
    chrome.contextMenus.create({ id: "capture-page", title: "Capture current page", contexts: ["page"] });
  });
  await chrome.alarms.create(BADGE_ALARM, { periodInMinutes: 5 });
  await updateBadge();
});

chrome.runtime.onStartup.addListener(() => updateBadge());
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === BADGE_ALARM) updateBadge({ notify: true }); });

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    await captureTab(tab, info.menuItemId === "capture-selection" ? "selection" : "page", info.selectionText || "");
    await openPanel(tab);
  } catch (error) {
    await chrome.storage.local.set({ captureError: safeError(error).error });
    await openPanel(tab);
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "capture-current-page") return;
  const tab = await activeTab();
  try { await captureTab(tab); } catch (error) { await chrome.storage.local.set({ captureError: safeError(error).error }); }
  await openPanel(tab);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === MESSAGE.getState) {
      const value = await settings();
      const stored = await chrome.storage.local.get([STORAGE_KEYS.pendingCapture, "captureError"]);
      return { ok: true, data: { settings: value, pendingCapture: stored[STORAGE_KEYS.pendingCapture] || null, captureError: stored.captureError || null } };
    }
    if (message.type === MESSAGE.pair) {
      const current = await settings();
      const client = current.mockMode ? new MockTransport(chrome.storage.local) : new HermesClient({ baseUrl: message.baseUrl });
      const result = await client.pair(message.code, message.deviceName || "Chrome");
      await saveSettings({ ...current, baseUrl: message.baseUrl, deviceToken: result.deviceToken, backendVersion: result.backendVersion, apiVersion: result.apiVersion });
      await updateBadge();
      return { ok: true, data: result };
    }
    if (message.type === MESSAGE.revoke) {
      const current = await settings();
      if (current.deviceToken) await callTransport("revoke");
      await saveSettings({ ...current, deviceToken: null, backendVersion: undefined, apiVersion: undefined });
      await updateBadge();
      return { ok: true, data: { revoked: true } };
    }
    if (message.type === MESSAGE.api) return { ok: true, data: await callTransport(message.method, message.args || []) };
    if (message.type === MESSAGE.capture) {
      const tab = await activeTab();
      const capture = await captureTab(tab, message.kind || "page");
      return { ok: true, data: capture };
    }
    if (message.type === MESSAGE.openOptions) { await chrome.runtime.openOptionsPage(); return { ok: true }; }
    if (message.type === MESSAGE.refreshBadge) { await updateBadge(); return { ok: true }; }
    throw new Error("Unknown extension request.");
  })().then(sendResponse).catch((error) => sendResponse(safeError(error)));
  return true;
});
