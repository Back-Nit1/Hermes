import { normalizeBaseUrl } from "./shared/api-client.js";
import { MESSAGE, STORAGE_KEYS } from "./shared/contracts.js";
import { $, announce, message } from "./shared/ui.js";

let current = null;

function originPattern(value) {
  const url = new URL(normalizeBaseUrl(value));
  return `${url.protocol}//${url.host}/*`;
}

function state(name, detail) {
  const copy = {
    online: ["Connected", "This device is ready to capture."], unpaired: ["Not paired", "Enter a one-time code from HERMES Settings."],
    offline: ["HERMES is offline", "Check the address and make sure HERMES is running."], revoked: ["Device revoked", "Generate a new code in HERMES to pair again."],
    unauthorized: ["Code or token rejected", "Generate a new one-time code and try again."], permission_denied: ["Permission denied", "Allow access to this HERMES address when Chrome asks."],
    version_mismatch: ["Update required", "Update HERMES or this extension so their API versions match."]
  };
  $("#connection").dataset.state = name;
  $("#connection-title").textContent = copy[name]?.[0] || copy.offline[0];
  $("#connection-detail").textContent = detail || copy[name]?.[1] || copy.offline[1];
}

async function saveMode() {
  current = { ...current, mockMode: $("#mock-mode").checked, baseUrl: normalizeBaseUrl($("#base-url").value) };
  await chrome.storage.local.set({ [STORAGE_KEYS.settings]: current });
}

async function ensurePermission(baseUrl) {
  if ($("#mock-mode").checked) return true;
  const origin = originPattern(baseUrl);
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (granted) $("#permission-origin").textContent = origin;
  return granted;
}

$("#mock-mode").addEventListener("change", async () => {
  if (current.deviceToken && current.mockMode !== $("#mock-mode").checked) {
    $("#mock-mode").checked = current.mockMode;
    state("permission_denied", "Revoke the current device token before changing transports.");
    return;
  }
  await saveMode();
  $("#pairing-code").placeholder = $("#mock-mode").checked ? "HERMES-DEMO" : "HERMES-AB12";
  state(current.deviceToken ? "online" : "unpaired", $("#mock-mode").checked ? "Demo transport enabled. No backend calls will be made." : undefined);
});

$("#connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const baseUrl = normalizeBaseUrl($("#base-url").value);
  if (!(await ensurePermission(baseUrl))) { state("permission_denied"); return; }
  await saveMode();
  try {
    const result = await message(MESSAGE.pair, { baseUrl, code: $("#pairing-code").value, deviceName: $("#device-name").value });
    current = { ...current, baseUrl, deviceToken: result.deviceToken, backendVersion: result.backendVersion, apiVersion: result.apiVersion };
    $("#pairing-code").value = "";
    state("online", current.mockMode ? "Demo transport enabled. No backend calls will be made." : undefined);
    announce("Device paired successfully.");
  } catch (error) { state(error.state || "offline", error.message); announce(error.message); }
});

$("#test-button").addEventListener("click", async () => {
  try { await saveMode(); await message(MESSAGE.api, { method: "summary" }); state("online", current.mockMode ? "Demo transport enabled. No backend calls will be made." : undefined); announce("Connection test passed."); }
  catch (error) { state(error.state || "offline", error.message); announce(error.message); }
});

$("#revoke-button").addEventListener("click", async () => {
  try { await message(MESSAGE.revoke); current.deviceToken = null; state("revoked"); announce("Device access revoked."); }
  catch (error) { state(error.state || "offline", error.message); announce(error.message); }
});

(async () => {
  const data = await message(MESSAGE.getState);
  current = data.settings;
  $("#base-url").value = current.baseUrl;
  $("#mock-mode").checked = current.mockMode;
  $("#pairing-code").placeholder = current.mockMode ? "HERMES-DEMO" : "HERMES-AB12";
  if (current.deviceToken) state("online", current.mockMode ? "Demo transport enabled. No backend calls will be made." : undefined);
  const origin = originPattern(current.baseUrl);
  $("#permission-origin").textContent = await chrome.permissions.contains({ origins: [origin] }) ? origin : "Not granted";
})();
