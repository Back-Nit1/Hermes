import { API_VERSION, EXTENSION_VERSION } from "./contracts.js";

export class HermesError extends Error {
  constructor(state, message, status = 0) {
    super(message);
    this.name = "HermesError";
    this.state = state;
    this.status = status;
  }
}

export function normalizeBaseUrl(value) {
  const url = new URL(String(value || "").trim());
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error("Use an HTTP or HTTPS HERMES URL.");
  url.hash = "";
  url.search = "";
  url.pathname = url.pathname.replace(/\/+$/, "");
  return url.href.replace(/\/$/, "");
}

export function connectionStateFor(error) {
  if (error instanceof HermesError) return error.state;
  if (["offline", "revoked", "unauthorized", "permission_denied", "version_mismatch", "unpaired"].includes(error?.state)) return error.state;
  return "offline";
}

function parseVersion(value) {
  return String(value || "").match(/^\d{4}-\d{2}-\d{2}/)?.[0] || String(value || "");
}

export function assertApiVersion(received) {
  if (parseVersion(received) !== API_VERSION) {
    throw new HermesError("version_mismatch", `HERMES API ${received || "unknown"} is incompatible with ${API_VERSION}.`, 409);
  }
}

export class HermesClient {
  /** @param {{baseUrl:string, deviceToken?:string|null, fetchImpl?:typeof fetch}} options */
  constructor({ baseUrl, deviceToken, fetchImpl = globalThis.fetch }) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.deviceToken = deviceToken || null;
    this.fetchImpl = fetchImpl;
  }

  /**
   * @template T
   * @param {string} path
   * @param {{method?:string, body?:unknown, authenticated?:boolean}} [options]
   * @returns {Promise<T>}
   */
  async request(path, { method = "GET", body, authenticated = true } = {}) {
    const headers = { Accept: "application/json", "X-Hermes-API-Version": API_VERSION, "X-Hermes-Extension-Version": EXTENSION_VERSION };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (authenticated && this.deviceToken) headers.Authorization = `Bearer ${this.deviceToken}`;
    let response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    } catch {
      throw new HermesError("offline", "HERMES is unreachable. Check the URL and network connection.");
    }
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw new HermesError(payload.code === "device_revoked" ? "revoked" : "unauthorized", payload.detail || "This device is not authorized.", 401);
    if (response.status === 403) throw new HermesError("permission_denied", payload.detail || "HERMES denied this action.", 403);
    if (response.status === 409 && payload.code === "api_version_mismatch") throw new HermesError("version_mismatch", payload.detail || "The extension and backend versions do not match.", 409);
    if (!response.ok) throw new HermesError("offline", payload.detail || `HERMES request failed (${response.status}).`, response.status);
    if (payload.apiVersion) assertApiVersion(payload.apiVersion);
    return /** @type {T} */ (payload);
  }

  /** @returns {Promise<{deviceToken:string,backendVersion:string,apiVersion:string}>} */
  pair(code, deviceName) {
    return this.request("/api/extension/pair", { method: "POST", authenticated: false, body: { code, deviceName, apiVersion: API_VERSION, extensionVersion: EXTENSION_VERSION } });
  }

  /** @returns {Promise<{revoked:boolean}>} */
  revoke() { return this.request("/api/extension/device", { method: "DELETE" }); }
  /** @returns {Promise<import("./contracts.js").HermesSummary & {apiVersion:string, notificationItems?:unknown[], reviewItems?:unknown[], followupItems?:unknown[]}>} */
  summary() { return this.request("/api/extension/summary"); }
  /** @returns {Promise<{apiVersion:string,campaigns:import("./contracts.js").Campaign[]}>} */
  campaigns() { return this.request("/api/extension/campaigns"); }
  /** @param {import("./contracts.js").CapturePayload} capture @returns {Promise<import("./contracts.js").CapturePreview & {apiVersion:string}>} */
  preview(capture) { return this.request("/api/extension/captures/preview", { method: "POST", body: { capture } }); }
  /** @returns {Promise<{apiVersion:string,committed:boolean,campaignId:string,captureId:string}>} */
  commit(previewId, campaignId) { return this.request("/api/extension/captures", { method: "POST", body: { previewId, campaignId } }); }
}

export function dashboardLink(baseUrl, action, params = {}) {
  const url = new URL(normalizeBaseUrl(baseUrl));
  url.hash = new URLSearchParams({ view: action, ...params }).toString();
  return url.href;
}
