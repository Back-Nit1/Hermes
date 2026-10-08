import test from "node:test";
import assert from "node:assert/strict";
import { MockTransport } from "../mock/mock-transport.js";

class MemoryStorage {
  constructor() { this.data = {}; }
  async get(key) { return { [key]: this.data[key] }; }
  async set(value) { Object.assign(this.data, value); }
}

test("mock pairing supports backend-independent development", async () => {
  const mock = new MockTransport(new MemoryStorage());
  await assert.rejects(mock.pair("bad"), (error) => error.state === "unauthorized");
  const paired = await mock.pair("HERMES-DEMO");
  assert.equal(paired.deviceToken, "mock-device-token");
  assert.equal((await mock.summary()).campaigns.length, 3);
});

test("mock capture requires preview and explicit campaign commit", async () => {
  const mock = new MockTransport(new MemoryStorage());
  const preview = await mock.preview({ kind: "selection", url: "https://example.edu", title: "Lab", selection: "Promising research" });
  assert.match(preview.previewId, /^preview_/);
  assert.equal((await mock.state()).captures.length, 0);
  const result = await mock.commit(preview.previewId, "cmp_neuro");
  assert.equal(result.committed, true);
  assert.equal((await mock.state()).captures.length, 1);
  await assert.rejects(mock.commit(preview.previewId, "cmp_neuro"), /expired/);
});

test("mock warns for existing contacts and blocks DNC commits", async () => {
  const mock = new MockTransport(new MemoryStorage());
  const preview = await mock.preview({ kind: "selection", url: "https://example.edu", title: "Maya — DNC", selection: "Do not contact" });
  assert.deepEqual(preview.warnings.map((x) => x.kind), ["existing_contact", "do_not_contact"]);
  assert.equal(preview.warnings[1].blocking, true);
  await assert.rejects(mock.commit(preview.previewId, "cmp_neuro"), (error) => error.state === "permission_denied");
});
