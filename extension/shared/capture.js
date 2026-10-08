import { MAX_DESCRIPTION_CHARS, MAX_SELECTION_CHARS } from "./contracts.js";

export function cleanText(value, maxLength) {
  return String(value || "").replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, " ").replace(/\s+/g, " ").trim().slice(0, maxLength);
}

export function isCapturableUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function normalizeCapture(input) {
  if (!isCapturableUrl(input.url)) throw new Error("This browser page cannot be captured.");
  const selection = cleanText(input.selection, MAX_SELECTION_CHARS);
  return {
    kind: input.kind === "selection" && selection ? "selection" : "page",
    url: new URL(input.url).href,
    title: cleanText(input.title, 300) || new URL(input.url).hostname,
    ...(selection ? { selection } : {}),
    ...(input.description ? { description: cleanText(input.description, MAX_DESCRIPTION_CHARS) } : {}),
    capturedAt: input.capturedAt || new Date().toISOString()
  };
}

// Serialized by chrome.scripting.executeScript. Keep it dependency-free and
// intentionally omit body text, DOM HTML, forms, and cookies.
export function readPageMetadata() {
  const selected = globalThis.getSelection?.()?.toString() || "";
  const description = document.querySelector('meta[name="description"]')?.getAttribute("content") || "";
  return { url: location.href, title: document.title, selection: selected, description };
}
