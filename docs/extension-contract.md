# HERMES Chrome extension contract

Contract version: `2026-10-01`

This document is the boundary between the Chrome extension and the HERMES backend. The extension is implemented under `extension/`; backend implementation is owned separately. JSON property names are camelCase on this boundary even if backend domain objects use snake_case.

## Trust and transport

- Production uses HTTPS. Loopback development may use `http://127.0.0.1` or `http://localhost`.
- The options page requests Chrome's optional host permission for exactly the configured origin. The extension does not hold a persistent `<all_urls>` grant.
- All responses include `apiVersion: "2026-10-01"`. Requests include `X-Hermes-API-Version` and `X-Hermes-Extension-Version`.
- Authenticated requests use `Authorization: Bearer <device-token>`. Tokens must be scoped to one device, stored hashed by the backend, revocable, and excluded from URLs and logs.
- The backend must allow the installed `chrome-extension://<id>` origin. It must not combine wildcard CORS origins with credentials.
- Responses use `Cache-Control: no-store`. Neither pairing codes nor device tokens may appear in response error detail, telemetry, or dashboard deep links.
- Mutations use the authenticated HERMES user resolved from the device token. A request may not select a user or data root.

## Pairing and device lifecycle

`POST /api/extension/pair` is unauthenticated and consumes a short-lived, single-use code.

```json
{
  "code": "HERMES-AB12",
  "deviceName": "Chrome",
  "apiVersion": "2026-10-01",
  "extensionVersion": "0.1.0"
}
```

Success returns `200`:

```json
{
  "apiVersion": "2026-10-01",
  "backendVersion": "0.3.0",
  "deviceToken": "opaque-high-entropy-token"
}
```

Codes expire after ten minutes, are invalid after the first attempt that succeeds, and are rate-limited by account and source. Bad or expired codes return `401` with `{ "code": "pairing_code_invalid", "detail": "Pairing code is invalid or expired." }`.

`DELETE /api/extension/device` revokes the calling token and returns `{ "apiVersion": "2026-10-01", "revoked": true }`. Subsequent use returns `401` with `code: "device_revoked"`. The dashboard must also expose device listing and revocation.

## Summary and campaigns

`GET /api/extension/summary` returns a bounded summary:

```json
{
  "apiVersion": "2026-10-01",
  "notifications": 2,
  "reviewQueue": 2,
  "followupsDue": 1,
  "campaigns": [{ "id": "cmp_123", "name": "Neurodevelopment", "status": "active" }],
  "notificationItems": [{ "id": 4, "title": "Draft ready", "detail": "Dr. Chen", "kind": "review" }],
  "reviewItems": [{ "id": "c_1", "campaignId": "cmp_123", "name": "Dr. Chen", "status": "Needs approval" }],
  "followupItems": [{ "id": "f_1", "campaignId": "cmp_123", "name": "Avery Singh", "due": "Today", "status": "Needs review" }]
}
```

Item arrays are capped at 20 and contain no draft bodies. `GET /api/extension/campaigns` returns `{ apiVersion, campaigns }`, active campaigns first, capped at 100.

## Capture preview and commit

The extension sends one of these bounded payloads to `POST /api/extension/captures/preview`:

```json
{
  "capture": {
    "kind": "selection",
    "url": "https://example.edu/lab",
    "title": "Example Lab",
    "selection": "User-selected text, at most 8,000 characters",
    "description": "Meta description, at most 500 characters",
    "capturedAt": "2026-10-07T18:00:00.000Z"
  }
}
```

`kind: "page"` omits `selection`. The backend rejects other keys that could carry HTML, cookies, headers, screenshots, page body text, or form data. It must treat all captured strings as untrusted data, never as instructions or markup. Preview response:

```json
{
  "apiVersion": "2026-10-01",
  "previewId": "preview_opaque",
  "capture": { "kind": "page", "url": "https://example.edu/lab", "title": "Example Lab", "capturedAt": "2026-10-07T18:00:00.000Z" },
  "warnings": [{ "kind": "existing_contact", "message": "This person is already in a campaign.", "blocking": false }],
  "suggestedCampaignId": "cmp_123"
}
```

The preview is server-side, token-bound, immutable, single-use, and expires after 30 minutes. It checks normalized profile URL/contact identity against existing contacts and global DNC/suppressions. DNC produces `kind: "do_not_contact"` with `blocking: true`.

`POST /api/extension/captures` commits only a prior preview:

```json
{ "previewId": "preview_opaque", "campaignId": "cmp_123" }
```

Success returns `{ apiVersion, committed: true, campaignId, captureId }`. The backend re-checks campaign access and DNC atomically; a blocked commit returns `403`. Replaying a committed/expired preview returns `409`. Captures enter the campaign as reviewable source material and never trigger research, drafting, Gmail creation, scheduling, or sending automatically.

## Error states

| HTTP/result | Code | Extension state |
| --- | --- | --- |
| Network failure/timeout | n/a | `offline` |
| 401 | `device_revoked` | `revoked` |
| 401 | other | `unauthorized` |
| 403 | any | `permission_denied` |
| 409 | `api_version_mismatch` | `version_mismatch` |

Version mismatch detail should state the minimum compatible extension/backend version without exposing server internals. Other 4xx/5xx errors include a safe human-readable `detail`; raw exception text is never returned.

## Dashboard deep links

The extension opens the configured HERMES base URL with a hash query. The dashboard owns routing and authorization:

| Intent | Hash |
| --- | --- |
| Campaign/source detail | `#view=campaign&campaign=<id>` |
| Approval/review queue | `#view=approval&campaign=<optional-id>` |
| Create Gmail draft | `#view=gmail&campaign=<optional-id>` |
| Schedule | `#view=scheduling&campaign=<optional-id>` |
| Send controls | `#view=sending&campaign=<optional-id>` |
| Settings/devices | `#view=settings` |
| Deletion/privacy | `#view=deletion&campaign=<optional-id>` |

Deep links never approve, create, schedule, send, or delete by themselves. They only navigate to the corresponding dashboard confirmation screen.
