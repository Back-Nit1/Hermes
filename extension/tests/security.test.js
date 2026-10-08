import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const productionScripts = ["background.js", "sidepanel.js", "options.js", "shared/api-client.js", "shared/capture.js", "shared/ui.js", "mock/mock-transport.js"];

test("production scripts avoid HTML injection, whole-page reads, clipboard access, and dynamic code", async () => {
  for (const path of productionScripts) {
    const source = await readFile(new URL(`../${path}`, import.meta.url), "utf8");
    for (const forbidden of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.body", "navigator.clipboard", "eval(", "new Function("]) {
      assert.equal(source.includes(forbidden), false, `${path} contains ${forbidden}`);
    }
  }
});

test("extension pages load only bundled scripts and styles", async () => {
  for (const page of ["sidepanel.html", "options.html"]) {
    const html = await readFile(new URL(`../${page}`, import.meta.url), "utf8");
    assert.equal(/<(?:script|link)[^>]+(?:src|href)=["']https?:/i.test(html), false, page);
    assert.equal(/on(?:click|load|error)\s*=/i.test(html), false, `${page} has inline event code`);
  }
});
