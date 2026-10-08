"""Regression coverage for queue, restart, reconciliation, freshness, and analytics reliability."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import analytics, demo, followups
from app.cache import Cache
from app.campaigns import CampaignService, parse_request
from app.config import Config
from app.gmail import GmailDrafts, GmailReconciliationIncomplete
from app.jobs import QueueSaturated
from app.storage import CampaignStore
from app.workspace import Workspace
from tests.support import BIO
from tests.test_delivery_integration import world


def service(tmp_path, *, gmail=None, freshness_hours=336, workspace=False):
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    cache = Cache(root / "cache.sqlite3")
    ws = Workspace(cache, root) if workspace else None
    svc = CampaignService(
        CampaignStore(root),
        cache,
        demo.DemoModel(cache),
        demo.demo_fetcher(cache),
        gmail,
        ws,
        send_every=None,
        config=Config(data_root=root, research_freshness_hours=freshness_hours),
    )
    return svc


def campaign_with_people(svc, count=2, *, identity_id=None):
    intake, _ = parse_request("Rutgers professors working on computational neurodevelopment")
    intake.update(sender_background=None if identity_id else BIO, sender_identity_id=identity_id)
    cid = svc.create(intake)
    ids = [f"c_{i}" for i in range(count)]

    def add(doc):
        for i, cand_id in enumerate(ids):
            doc["candidates"].append(
                {
                    "candidate_id": cand_id,
                    "name": f"Person {i}",
                    "organization": "Rutgers",
                    "role": "Professor",
                    "profile_url": f"https://example.edu/{i}",
                    "discovery_source_url": "https://example.edu/directory",
                    "status": "selected",
                }
            )

    svc.store.update_candidates(cid, add)
    return cid, ids


def profile(cand_id, researched_at=None):
    return {
        "candidate_id": cand_id,
        "name": cand_id,
        "organization": "Rutgers",
        "role": "Professor",
        "status": "researched",
        "contact_email": f"{cand_id}@example.edu",
        "email_verified_on_page": True,
        "fit_reason": "Relevant sourced work.",
        "evidence": [{"claim": "Relevant sourced work.", "source_url": "https://example.edu/source"}],
        "researched_at": researched_at or datetime.now(UTC).isoformat(),
    }


def test_partial_research_and_draft_admission_returns_candidate_results(tmp_path):
    svc = service(tmp_path)
    cid, ids = campaign_with_people(svc)
    calls = []

    def admit(_cid, _kind, candidate):
        calls.append(candidate)
        if len(calls) > 1:
            raise QueueSaturated("lane full")
        return "job_first"

    svc.jobs.submit = admit
    result = svc.research(cid, ids)
    assert result["accepted"] == [{"candidate_id": ids[0], "job_id": "job_first"}]
    assert result["rejected"] == [
        {"candidate_id": ids[1], "reason": "queue_saturated", "detail": "lane full"}
    ]

    svc.store.update_research(cid, lambda doc: doc["profiles"].update({i: profile(i) for i in ids}))
    calls.clear()
    result = svc.generate(cid, ids)
    assert result["accepted_ids"] == [ids[0]]
    assert result["rejected_ids"] == [ids[1]]
    assert result["rejected"][0]["reason"] == "queue_saturated"


def due_followup(svc, cid, cand):
    svc.cache.x(
        "INSERT INTO outreach_contacts (campaign_id,candidate_id,outcome,sequence,sequence_state,do_not_contact) "
        "VALUES (?,?, 'awaiting_reply','professor','active',0)",
        (cid, cand),
    )
    svc.cache.x(
        "INSERT INTO followups (campaign_id,candidate_id,step,due_at,status,attempts,updated_at) "
        "VALUES (?,?,0,0,'scheduled',0,0)",
        (cid, cand),
    )


def test_followup_admission_saturation_rolls_back_and_restart_recovers(tmp_path):
    svc = service(tmp_path)
    cid, (cand,) = campaign_with_people(svc, 1)
    due_followup(svc, cid, cand)
    svc.now = lambda: 1000
    svc.jobs.submit = lambda *_: (_ for _ in ()).throw(QueueSaturated("write lane full"))

    assert svc.outreach.tick() == 0
    row = svc.cache.q("SELECT status,attempts FROM followups")[0]
    assert row == {"status": "scheduled", "attempts": 0}

    svc.cache.x("UPDATE followups SET status='generating',attempts=1")
    svc.cache.close()
    restarted = service(tmp_path)
    restarted.outreach.reset_interrupted()
    assert restarted.cache.q("SELECT status,attempts FROM followups")[0] == {
        "status": "scheduled",
        "attempts": 1,
    }


def test_two_scheduler_ticks_claim_a_due_followup_once(tmp_path):
    svc = service(tmp_path)
    cid, (cand,) = campaign_with_people(svc, 1)
    due_followup(svc, cid, cand)
    svc.now = lambda: 1000
    admitted = []
    svc.jobs.submit = lambda *_args: admitted.append(_args[-1]) or "job"

    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = list(pool.map(lambda _: svc.outreach.tick(), range(2)))

    assert sum(queued) == 1
    assert admitted == [f"{cand}#0"]
    assert svc.cache.q("SELECT status,attempts FROM followups")[0] == {
        "status": "generating",
        "attempts": 1,
    }


def test_identity_fields_flow_through_automated_followup(tmp_path, monkeypatch):
    svc = service(tmp_path, workspace=True)
    identity = svc.workspace.create_identity(
        {
            "display_name": "Nitu",
            "biography": "Identity biography",
            "signature": "Identity Signature",
            "default_ask": "Identity default ask",
        }
    )
    cid, (cand,) = campaign_with_people(svc, 1, identity_id=str(identity["id"]))
    svc.store.update_research(cid, lambda doc: doc["profiles"].update({cand: profile(cand)}))
    svc.cache.x(
        "INSERT INTO outreach_contacts (campaign_id,candidate_id,subject,sent_at,outcome,sequence,sequence_state) "
        "VALUES (?,?,?,?,'awaiting_reply','professor','active')",
        (cid, cand, "Original subject", 100),
    )
    svc.cache.x(
        "INSERT INTO followups (campaign_id,candidate_id,step,due_at,status,attempts,updated_at) "
        "VALUES (?,?,0,0,'generating',1,0)",
        (cid, cand),
    )
    captured = {}

    async def write(_model, _cid, outline, _profile):
        captured.update(outline)
        return {"subject": "ignored", "body": "Follow-up body", "evidence_ids": ["e0"], "issues": []}

    monkeypatch.setattr(followups.writer, "write_draft", write)
    asyncio.run(svc.outreach.generate(cid, f"{cand}#0"))

    assert captured["sender_context"] == "Identity biography"
    assert captured["ask"] == "Identity default ask"
    row = svc.cache.q("SELECT body,status FROM followups")[0]
    assert row["body"].endswith("Identity Signature") and row["status"] == "needs_review"


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class PagedDrafts:
    def users(self):
        return self

    def drafts(self):
        return self

    def list(self, userId, maxResults, pageToken=None):
        assert userId == "me" and maxResults > 0
        if pageToken is None:
            return Request({"drafts": [{"id": "d1"}], "nextPageToken": "page-2"})
        return Request({"drafts": [{"id": "d2"}]})

    def get(self, userId, id, format):
        key = "wanted" if id == "d2" else "other"
        return Request(
            {
                "id": id,
                "message": {
                    "id": f"m-{id}",
                    "threadId": f"t-{id}",
                    "payload": {"headers": [{"name": "X-Outreach-Key", "value": key}]},
                },
            }
        )


def test_gmail_reconciliation_paginates_and_bounded_search_stays_uncertain(tmp_path):
    gmail = GmailDrafts(PagedDrafts())
    assert gmail.find_by_key("wanted")["draft_id"] == "d2"
    with pytest.raises(GmailReconciliationIncomplete):
        gmail.find_by_key("missing", scan=1)

    svc = service(tmp_path)
    created = []
    svc.gmail = SimpleNamespace(
        find_by_key=lambda _key: (_ for _ in ()).throw(GmailReconciliationIncomplete("more pages")),
        create=lambda *_args, **_kwargs: created.append(True),
    )
    result = asyncio.run(svc.gmail_create("key", True, lambda: created.append("attempt"), ("to", "s", "b")))
    assert result["result"].startswith("uncertain:")
    assert created == []


def test_sync_failure_backoff_is_fair_and_success_timestamp_is_truthful(tmp_path):
    class Mail:
        can_sync = True

        def thread(self, thread_id):
            if thread_id == "bad":
                raise RuntimeError("temporary")
            return []

    svc = service(tmp_path, gmail=Mail())
    svc.now = lambda: 1000
    svc.cache.x(
        "INSERT INTO outreach_contacts (campaign_id,candidate_id,gmail_thread_id,outcome,sequence_state) "
        "VALUES ('campaign','c1','bad','awaiting_reply','active')"
    )

    first = asyncio.run(svc.sync_now())
    assert first["errors"] == 1
    assert svc.last_gmail_sync is None
    failed = svc.cache.q("SELECT * FROM outreach_contacts WHERE candidate_id='c1'")[0]
    assert failed["last_synced_at"] is None and failed["sync_retry_at"] == 1060

    svc.cache.x(
        "INSERT INTO outreach_contacts (campaign_id,candidate_id,gmail_thread_id,outcome,sequence_state) "
        "VALUES ('campaign','c2','good','awaiting_reply','active')"
    )
    second = asyncio.run(svc.sync_now())
    assert second == {"checked": 1, "replies": 0, "bounces": 0, "errors": 0, "followups_queued": 0}
    assert svc.last_gmail_sync is not None
    succeeded = svc.cache.q("SELECT * FROM outreach_contacts WHERE candidate_id='c2'")[0]
    assert succeeded["last_synced_at"] == 1000 and succeeded["sync_failures"] == 0


def test_stale_profile_is_visible_and_requires_refresh(tmp_path):
    svc = service(tmp_path, freshness_hours=1)
    cid, (cand,) = campaign_with_people(svc, 1)
    stale_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    svc.store.update_research(cid, lambda doc: doc["profiles"].update({cand: profile(cand, stale_at)}))

    assert svc.get(cid)["candidates"][0]["research_freshness"]["state"] == "stale"
    assert svc.detail(cid, cand)["profile"]["freshness"]["is_stale"] is True
    assert svc.generate(cid, [cand])["rejected"] == [
        {"candidate_id": cand, "reason": "not_researched", "detail": "fresh research profile required"}
    ]
    svc.jobs.submit = lambda *_: "refresh-job"
    assert svc.research(cid, [cand])["accepted_ids"] == [cand]


def test_scheduled_send_records_analytics_milestone(tmp_path):
    svc, _provider, _clock, _workspace, _identity, _attachment, cid, cand = world(tmp_path)
    preview = svc.preview_send(cid, cand)
    svc.confirm_send(cid, cand, preview["approval_hash"])

    assert analytics.timeline(svc.cache, cid, cand) == [
        {"stage": "scheduled", "at": svc.outbox.row(svc.outbox.list(cid)[0]["send_id"])["approved_at"]}
    ]
    assert analytics.report(svc, cid)["counts"]["scheduled"] == 1
