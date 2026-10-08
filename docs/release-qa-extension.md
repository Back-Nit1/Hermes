# Extension release QA checklist

Branch `agent/extension-release-qa`, base `b9c77ac` (main). Status: **BLOCKED** — extension, extension backend and DNC work are not committed by their owners yet.

## Upstream state at run time
| Branch | Commits ahead of main | Working tree |
|---|---|---|
| agent/chrome-extension | 0 | untracked `extension/` only |
| agent/extension-backend | 0 | uncommitted `app/extension_api.py`, `app/extension_auth.py`, `app/api.py` edits |
| agent/dnc-safety | 0 | uncommitted edits to campaigns/followups/sending/workspace |

## Results on main (b9c77ac)
| Check | Result | Evidence |
|---|---|---|
| Full Python suite (non-browser) | PASS | 227 passed, 8 deselected, 64s |
| Browser + load tests | PASS | 8 passed, 28s |
| ruff check | PASS | All checks passed |
| ruff format --check | PASS | 70 files already formatted |
| pip-audit (requirements.lock) | FAIL | oauthlib 3.3.1 PYSEC-2026-4114, fix 4.0.0; transitive via google-auth-oauthlib 1.4.1 / requests-oauthlib 2.0.0 |
| Secret scan | PARTIAL | gitleaks not installed; regex grep of tracked files found nothing; no tracked `.env` |
| Schema checks | NOT RUN | no separate schema command found in repo |

## Not runnable until upstream commits land
Extension unit/manifest validation; Chromium `--load-extension`; pair/revoke/reconnect; capture preview/commit against the local backend; DNC during scheduling and follow-up generation; permission regression (no `<all_urls>`, no Gmail DOM, no privileged APIs); narrow/desktop accessibility; service-worker restart and backend-offline recovery; deterministic packaging with version and checksum; core-state screenshots.

## Unresolved risks
1. oauthlib vulnerability: needs upstream-compatible bump decision (4.0.0 may break requests-oauthlib pin).
2. No gitleaks binary; secret scan is regex-only.
3. All extension items above unverified.
