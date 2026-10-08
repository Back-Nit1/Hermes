import test from "node:test";
import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";

const manifest = JSON.parse(await readFile(new URL("../manifest.json", import.meta.url), "utf8"));

test("manifest is MV3 and requests only the approved extension permissions", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(new Set(manifest.permissions), new Set(["storage", "activeTab", "scripting", "contextMenus", "sidePanel", "notifications", "alarms"]));
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.permissions.includes("clipboardRead"), false);
  assert.equal(manifest.permissions.includes("clipboardWrite"), false);
});

test("manifest bundles all referenced executable and icon assets", async () => {
  const paths = [manifest.background.service_worker, manifest.side_panel.default_path, manifest.options_page, ...Object.values(manifest.icons)];
  for (const path of paths) assert.ok((await stat(new URL(`../${path}`, import.meta.url))).size > 0, path);
});

test("all host access is optional and no all-urls grant exists", () => {
  assert.ok(manifest.optional_host_permissions.length > 0);
  assert.equal(manifest.optional_host_permissions.includes("<all_urls>"), false);
});
