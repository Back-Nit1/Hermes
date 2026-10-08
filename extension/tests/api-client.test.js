import test from "node:test";
import assert from "node:assert/strict";
import { API_VERSION } from "../shared/contracts.js";
import { HermesClient, HermesError, assertApiVersion, connectionStateFor, dashboardLink, normalizeBaseUrl } from "../shared/api-client.js";

const response = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test("client sends version and device token without leaking token into URL", async () => {
  let request;
  const client = new HermesClient({ baseUrl: "https://hermes.example/", deviceToken: "secret-device-token", fetchImpl: async (url, init) => { request = { url, init }; return response(200, { apiVersion: API_VERSION, notifications: 0 }); } });
  await client.summary();
  assert.equal(request.url, "https://hermes.example/api/extension/summary");
  assert.equal(request.init.headers.Authorization, "Bearer secret-device-token");
  assert.equal(request.init.headers["X-Hermes-API-Version"], API_VERSION);
  assert.equal(request.url.includes("secret-device-token"), false);
});

test("client maps backend and transport errors to explicit connection states", async () => {
  const cases = [
    [401, { code: "device_revoked" }, "revoked"],
    [401, {}, "unauthorized"],
    [403, {}, "permission_denied"],
    [409, { code: "api_version_mismatch" }, "version_mismatch"]
  ];
  for (const [status, body, state] of cases) {
    const client = new HermesClient({ baseUrl: "https://hermes.example", deviceToken: "x", fetchImpl: async () => response(status, body) });
    await assert.rejects(client.summary(), (error) => error instanceof HermesError && error.state === state);
  }
  const offline = new HermesClient({ baseUrl: "https://hermes.example", fetchImpl: async () => { throw new TypeError("fetch failed"); } });
  await assert.rejects(offline.summary(), (error) => error.state === "offline");
});

test("version checks, URL normalization, and dashboard links are deterministic", () => {
  assert.doesNotThrow(() => assertApiVersion(API_VERSION));
  assert.throws(() => assertApiVersion("1999-01-01"), (error) => error.state === "version_mismatch");
  assert.equal(normalizeBaseUrl("https://hermes.example/app/"), "https://hermes.example/app");
  assert.throws(() => normalizeBaseUrl("file:///tmp/hermes"), /HTTP or HTTPS/);
  assert.equal(dashboardLink("https://hermes.example", "approval", { campaign: "cmp_1" }), "https://hermes.example/#view=approval&campaign=cmp_1");
  assert.equal(connectionStateFor(Object.assign(new Error(), { state: "unauthorized" })), "unauthorized");
});
