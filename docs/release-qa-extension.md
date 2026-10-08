# Extension release QA checklist

Branch `agent/extension-release-qa`. Merged: backend `b83c3e7`, DNC `7bae67a`, extension `e8ba4ba` + `dc890d7`
(merge commits `2a78862`, `4625fb7`, `7dacc8c`). Baseline main `b9c77ac`.

**Verdict: NOT SHIPPABLE.** The extension client and the backend speak different contracts; real pairing fails (see F1).

## Results
| Check | Result | Evidence |
|---|---|---|
| Full Python suite incl. browser/load | PASS | 271 passed (before packaging test) |
| ruff check / format | PASS | clean, 78+ files |
| pip-audit (requirements.lock) | FAIL | oauthlib 3.3.1 PYSEC-2026-4114, fix 4.0.0 (transitive via google-auth-oauthlib/requests-oauthlib) |
| Secret scan | PARTIAL | gitleaks absent; regex grep of tracked files clean; no tracked `.env` |
| Extension unit + syntax check | PASS | `npm test` 14/14, `npm run check` ok |
| Manifest validation | PASS | MV3; no `<all_urls>`, no `host_permissions`, no Gmail host, no webRequest/debugger/cookies |
| Chromium `--load-extension` | PASS | service worker starts, options + side panel load, zero console/page errors |
| Demo-transport pair + states | PASS | status "Connected"; screenshots below |
| Narrow (360px) / desktop (1280px) | PASS | no horizontal scroll, 0 unnamed controls (scripted check only, not full axe audit) |
| DNC (scheduling, follow-ups, capture) | PASS | `tests/test_dnc_safety.py`, `tests/test_extension_api.py` in the 271 |
| Deterministic packaging | PASS | `scripts/package_extension.py`; two builds identical, sha256 `c4de7e6f9f773b43542ff9ccc34d518d733d06942d192cb24696034f07b4d1b3` for `hermes-extension-0.1.0.zip` (re-check after any extension change) |
| Pair against real backend | **FAIL** | F1 |
| Revoke / reconnect, preview/commit vs real backend | BLOCKED by F1 | |
| Service-worker restart, backend-offline recovery (real backend) | BLOCKED by F1 | demo mode only |

## Findings
**F1 (blocker): contract mismatch.** Extension `shared/api-client.js` calls `POST /api/extension/pair`, `GET /api/extension/summary`,
`/campaigns`, `/captures/preview`, `/captures`, `DELETE /api/extension/device` with camelCase bodies (`deviceToken`, `previewId`) and
`X-Hermes-API-Version` headers. Backend `app/extension_api.py` serves `/extension/v1/pair/exchange`, `/status`, `/campaigns`,
`/capture/preview`, `/capture/commit` with snake_case (`device_name`, `campaign_id`, `token`), no `/summary`, no previewId.
Live probe: pairing code created OK, then extension client `pair()` -> `unauthorized 401 "missing or bad app token"` (path sits under
the dashboard `/api/` guard). Owners must align one side; not fixed here (not my files, and it is a design call).
**F2:** manifest `optional_host_permissions` includes `https://*/*` (all HTTPS hosts, requestable at runtime). Not `<all_urls>`, but wider
than a pinned backend origin. `scripting` + `activeTab` are the only injection scopes; keep, but decide whether `https://*/*` is needed.
**F3:** demo transport (`mock/`) ships in the release zip.
**F4:** pairing placeholder `HERMES-AB12` (11 chars) vs backend minimum code length 8; confirm code format after F1.
**F5:** backend-native `/extension/v1/pair/exchange` returned 403 to a plain urllib call (origin allowlist/edge rules); not investigated.
**F6:** oauthlib advisory above.

## Screenshots (`docs/release-qa/`)
options-unpaired-desktop, options-paired-desktop, options-paired-narrow, sidepanel-overview-desktop, sidepanel-overview-narrow.
Capture-preview, DNC-blocked, revoked and offline states not captured against a real backend (blocked by F1); the demo-mode
equivalents are covered by the extension's own mock tests only.
