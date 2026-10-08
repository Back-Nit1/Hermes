# Accuracy evals

Offline and deterministic: scripted model output, in-memory HTTP, temp SQLite. No credentials, no network, no email.

    python -m evals.run          # prints metrics, exits 1 on regression (also gated by tests/test_evals.py in CI)
    EVAL_LIVE=1 OPENAI_API_KEY=... python -m evals.live_smoke   # opt-in, <=3 API calls, never sends, never in CI

Fixtures live in `cases.json` (professor, startup, speaker, missing email, conflicting identity, stale source,
prompt injection, unsupported claim, citation-only, manual labels, plus a DNC probe set). Bounds are in `thresholds.json`.

| Metric | Definition |
| --- | --- |
| candidate_identity_precision | accepted discovered people who are the right person / accepted |
| evidence_support_precision | accepted evidence whose claim is gold-supported / accepted |
| verified_email_precision | `email_verified_on_page` addresses equal to gold / verified |
| unsupported_personalization_rate | summary, fit_reason, interests, evidence, outline connection that are unsupported / all emitted |
| draft_evidence_coverage | draft cases citing >=1 outline evidence id, all web-verified, with no `check_draft` issues |
| dnc_false_negative_rate | DNC contacts the workspace fails to block across probe variants / probes expected to block |

## Known gaps (thresholds are set at today's baseline, tighten when fixed)

- `draft_evidence_coverage` 0.857: `outlines.pick_evidence` accepts evidence with `web_verified=false`
  (citation-only sources). It should prefer `web_verified` or `manual_correction` evidence.
- `dnc_false_negative_rate` 0.5: `Ledger.match` returns `review` for same-URL/different-name and
  same-name/different-email, and `Workspace.contact_row` treats `review` as "not DNC". A review hit on a
  DNC contact should block.
