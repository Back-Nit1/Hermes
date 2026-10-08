import test from "node:test";
import assert from "node:assert/strict";
import { cleanText, isCapturableUrl, normalizeCapture } from "../shared/capture.js";

test("capture normalizes whitespace and preserves only bounded user selection", () => {
  const capture = normalizeCapture({
    kind: "selection",
    url: "https://example.edu/lab#people",
    title: "  Example\nLab  ",
    selection: ` Maya\tChen ${"x".repeat(9_000)}`,
    description: "A\u0000 lab"
  });
  assert.equal(capture.kind, "selection");
  assert.equal(capture.title, "Example Lab");
  assert.equal(capture.selection.length, 8_000);
  assert.equal(capture.description, "A lab");
  assert.equal(Object.hasOwn(capture, "html"), false);
  assert.equal(Object.hasOwn(capture, "body"), false);
});

test("capture rejects privileged and malformed URLs", () => {
  for (const url of ["chrome://settings", "file:///etc/passwd", "javascript:alert(1)", "not a url"]) {
    assert.equal(isCapturableUrl(url), false);
    assert.throws(() => normalizeCapture({ url, title: "No" }), /cannot be captured/);
  }
});

test("blank selection becomes metadata-only page capture", () => {
  const capture = normalizeCapture({ kind: "selection", url: "https://example.com", title: "Example", selection: "  " });
  assert.equal(capture.kind, "page");
  assert.equal(capture.selection, undefined);
  assert.equal(cleanText(" a\n b ", 20), "a b");
});
