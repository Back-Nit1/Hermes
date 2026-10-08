"""Offline accuracy regression gate (runs in default CI, no credentials, no network)."""

from app import research
from evals.harness import check, run


def test_metrics_meet_regression_thresholds():
    metrics, _ = run()
    assert check(metrics) == []


def test_metrics_detect_a_broken_pipeline(monkeypatch):
    monkeypatch.setattr(research, "claim_supported", lambda *a: True)
    monkeypatch.setattr(research, "identity_ok", lambda *a: True)
    monkeypatch.setattr(research, "_has_email", lambda email, page: True)
    metrics, _ = run()
    bad = check(metrics)
    assert any(b.startswith("evidence_support_precision") for b in bad)
    assert any(b.startswith("verified_email_precision") for b in bad)


def test_harness_is_deterministic():
    assert run()[0] == run()[0]
