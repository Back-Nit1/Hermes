# Load-unpacked Playwright checklist

Use a headed Chromium persistent context. Extensions do not load in Playwright's default incognito context. The checklist is intentionally runner-neutral because this repository does not carry a Node dependency tree or lockfile.

## Launch

```js
import { chromium } from "playwright";
import path from "node:path";

const extensionPath = path.resolve("extension");
const context = await chromium.launchPersistentContext("/tmp/hermes-extension-profile", {
  headless: false,
  args: [
    `--disable-extensions-except=${extensionPath}`,
    `--load-extension=${extensionPath}`
  ]
});
let [worker] = context.serviceWorkers();
if (!worker) worker = await context.waitForEvent("serviceworker");
const extensionId = new URL(worker.url()).host;
```

Keep a normal HTTP test page open (not `chrome://`, the Chrome Web Store, or a PDF) for active-tab checks.

## Automated assertions

- [ ] Open `chrome-extension://${extensionId}/options.html`; browser console and service-worker console have no errors.
- [ ] Enable demo transport. Confirm the status says demo transport is enabled and no host permission prompt appears.
- [ ] Enter `HERMES-DEMO`, pair, reload options, and confirm the paired state persists without displaying the token.
- [ ] Open `chrome-extension://${extensionId}/sidepanel.html`; confirm all three summary counts and fixture queue rows render.
- [ ] Switch Overview → Capture → Queue by keyboard; focus is visible and selected tab state is announced.
- [ ] Trigger **Capture page details** on a normal test page. Confirm the preview explicitly says metadata only and contains no page body text.
- [ ] Select visible text on the test page, trigger **Capture selection**, and confirm only that selection appears in preview.
- [ ] Preview title containing `Maya`; confirm the existing-contact warning is non-blocking.
- [ ] Preview title or selection containing `DNC`; confirm the do-not-contact warning appears and commit is disabled.
- [ ] Choose a campaign and commit a non-DNC preview. Confirm the mock state's capture count increases only after the click.
- [ ] Open each dashboard action: approval, Gmail creation, scheduling, sending, settings, deletion. Confirm the new tab hash contains the requested `view` and campaign where relevant.
- [ ] Revoke the mock device and confirm the revoked state is clear; enable demo mode alone and confirm screens still work.
- [ ] Set a mismatched API version in a route fixture and verify the version-mismatch message. Repeat fixtures for offline, revoked, unauthorized, and HTTP 403 permission-denied responses.
- [ ] Inspect `chrome://extensions`: no manifest errors, uncaught service-worker errors, or missing icon errors.

## Permission and privacy assertions

- [ ] `chrome.management.get(extensionId)` shows only storage, activeTab, scripting, contextMenus, sidePanel, notifications, and alarms permissions.
- [ ] No persistent host permission exists after a clean install. Pairing a real backend requests only its configured origin.
- [ ] No content script appears until a user invokes capture; the one-shot script returns only URL, title, selection, and meta description.
- [ ] Gmail pages are not inspected, clipboard APIs are not called, and no request body contains full document HTML or `document.body` text.
- [ ] Disable the backend/network after creating a preview; confirm the pending preview remains local and the UI shows Offline rather than discarding it.

## Viewport and accessibility pass

- [ ] Exercise the panel at 320, 400, and 600 CSS pixels wide; no horizontal scroll or clipped action labels.
- [ ] Text remains readable at 200% zoom and with forced colors.
- [ ] Run Axe (if available): no critical/serious findings.
- [ ] Verify all interactive controls are keyboard reachable, labels resolve, warnings are text (not color-only), and live announcements cover success/failure.
