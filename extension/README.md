# HERMES Capture extension

This directory is a dependency-light Chrome Manifest V3 extension. Chrome runs the checked-in JavaScript directly; there is no build step, remote script, remote font, or persistent content script.

## Try it without a backend

1. Open `chrome://extensions`, enable Developer mode, and choose **Load unpacked**.
2. Select this `extension/` directory.
3. Open the extension's **Details → Extension options**.
4. Enable **Use demo transport**. The side panel now shows fixture campaigns, notifications, review items, and follow-ups.
5. To exercise pairing too, enter `HERMES-DEMO` as the one-time code.

Run the local checks with:

```sh
cd extension
npm test
npm run check
```

No install command is necessary, and this package intentionally has no lockfile.

## Privacy model

- `activeTab` access begins only after a context-menu, toolbar, side-panel, or keyboard action.
- Selection capture includes at most 8,000 normalized characters.
- Page capture includes URL, title, meta description (at most 500 characters), and an active selection. It never reads or uploads the page body, page HTML, form values, cookies, or Gmail DOM.
- Every capture is previewed by the backend or mock transport and explicitly committed to a selected campaign.
- Backend access is an optional host permission requested for the exact configured origin. `<all_urls>` is never requested.
- Pairing codes are sent once and never persisted. Device tokens are kept in `chrome.storage.local`, never sync storage, and can be revoked from the options page.

The backend API and deep-link contract is documented in `../docs/extension-contract.md`.

## Files

- `background.js`: service worker, on-demand capture, context menus, alarm badge, notifications, connection broker.
- `sidepanel.*`: summary, capture preview, warnings, campaigns, review queue, follow-ups, and dashboard links.
- `options.*`: one-time pairing, optional origin permission, mock mode, connection testing, and revocation.
- `shared/`: JSDoc-typed API client, capture boundary, and safe DOM helpers.
- `mock/`: persistent backend-independent transport fixtures.
- `tests/`: Node unit and manifest tests.
- `PLAYWRIGHT_CHECKLIST.md`: load-unpacked browser acceptance procedure.
