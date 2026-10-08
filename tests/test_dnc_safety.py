"""Do-not-contact is one service-level operation: once any path completes, no Gmail draft or send can follow."""

import threading
import time

import pytest

from tests.test_sending import (
    Clock,
    FakeGmail,
    add_person,
    at,
    enable,
    make_campaign,
    make_service,
    schedule,
)

EMAIL = "avery.lin@demo.example.edu"


@pytest.fixture
def world(tmp_path):
    fake, clock = FakeGmail(), Clock(at(2026, 9, 28, 14, 0))
    svc = make_service(tmp_path, fake, clock)
    svc.outbox.recover()
    cid = make_campaign(svc)
    return svc, fake, clock, cid, tmp_path


def link(svc, cid, cand):
    c = next(c for c in svc.store.candidates(cid)["candidates"] if c["candidate_id"] == cand)
    p = svc.store.research(cid)["profiles"][cand]
    return svc.ledger.link_candidate(cid, c, p["contact_email"], source="test")


def add_followups(svc, cid, cand):
    svc.outreach._set(cid, cand, sequence="professor", sequence_state="active", outcome="awaiting_reply", sent_at=1.0)
    for step, status in enumerate(("scheduled", "needs_review", "awaiting_delivery")):
        svc.cache.x(
            "INSERT INTO followups (campaign_id,candidate_id,step,due_at,status,updated_at) VALUES (?,?,?,?,?,?)",
            (cid, cand, step, 1.0, status, 1.0),
        )


def statuses(svc, cid, cand):
    return {
        r["step"]: r["status"]
        for r in svc.cache.q("SELECT step,status FROM followups WHERE campaign_id=? AND candidate_id=?", (cid, cand))
    }


def draft_status(svc, cid, cand):
    return svc.cache.get_draft(cid, cand)["status"]


def audit_events(svc):
    return [a["event"] for a in svc.outbox.global_audit()]


def assert_nothing_goes_out(svc, fake, clock):
    for _ in range(4):
        clock.t += 3600
        assert svc.outbox.tick() is None
    assert fake.sent == [] and fake.n == 0


def scheduled_world(world, n=1):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, n)
    enable(svc, cid)
    contact_id = link(svc, cid, cand)
    row = schedule(svc, cid, cand, "2026-09-28T15:00")
    return cand, contact_id, row


def test_contact_patch_enforces_everywhere(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    add_followups(svc, cid, cand)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True, "dnc_reason": "asked to stop"})
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"
    assert svc.cache.q("SELECT reason FROM suppressions") == [{"reason": "do_not_contact"}]
    assert svc.cache.q("SELECT email FROM suppressions") == [{"email": EMAIL}]
    assert draft_status(svc, cid, cand) == "needs_review"
    assert statuses(svc, cid, cand) == {0: "cancelled", 1: "cancelled", 2: "cancelled"}
    oc = svc.outreach.contact(cid, cand)
    assert oc["do_not_contact"] == 1 and oc["sequence_state"] == "stopped"
    assert "dnc_enforced" in audit_events(svc)
    clock.t = at(2026, 9, 28, 15, 5)
    assert_nothing_goes_out(svc, fake, clock)


def test_api_patch_goes_through_the_same_operation(world):
    from fastapi.testclient import TestClient

    from app.api import create_app

    svc, *_ = world
    cand, contact_id, row = scheduled_world(world)
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        r = client.patch(f"/api/contacts/{contact_id}", json={"do_not_contact": True})
        assert r.status_code == 200
        assert svc.outbox.row(row["send_id"])["status"] == "cancelled"


def test_campaign_dnc_path_and_manual_sequence_action(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    svc.sequence(cid, cand, "do_not_contact")
    assert svc.ledger.contact(contact_id)["do_not_contact"] is True
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"
    assert svc.cache.q("SELECT email FROM suppressions") == [{"email": EMAIL}]
    assert draft_status(svc, cid, cand) == "needs_review"


def test_cross_campaign_links_and_second_address(world):
    svc, fake, clock, cid, _ = world
    cid2 = make_campaign(svc)
    a = add_person(svc, cid, 1)
    b = add_person(svc, cid2, 1)  # same person and address in another campaign
    enable(svc, cid)
    svc.outbox.set_campaign_enabled(cid2, True)
    contact_id = link(svc, cid, a)
    assert link(svc, cid2, b) == contact_id
    # a profile address the contact record itself does not carry
    svc.store.update_research(cid2, lambda d: d["profiles"][b].update(contact_email="alt@demo.example.edu"))
    add_followups(svc, cid2, b)
    rows = [schedule(svc, cid, a, "2026-09-28T15:00"), schedule(svc, cid2, b, "2026-09-28T16:00")]
    svc.cache.x("UPDATE drafts SET status='gmail_draft_created' WHERE campaign_id=?", (cid2,))
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    assert [svc.outbox.row(r["send_id"])["status"] for r in rows] == ["cancelled", "cancelled"]
    assert {r["email"] for r in svc.cache.q("SELECT email FROM suppressions")} == {EMAIL, "alt@demo.example.edu"}
    assert draft_status(svc, cid2, b) == "needs_review"  # gmail_draft_created is invalidated too
    assert set(statuses(svc, cid2, b).values()) == {"cancelled"}
    clock.t = at(2026, 9, 28, 17, 0)
    assert_nothing_goes_out(svc, fake, clock)


def test_enforcement_is_idempotent(world):
    svc, *_ = world
    cand, contact_id, row = scheduled_world(world)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    before = audit_events(svc)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True, "notes": "again"})
    svc.enforce_dnc(contact_id)
    assert audit_events(svc) == before
    assert len(svc.cache.q("SELECT * FROM suppressions")) == 1


def test_csv_import_and_new_dnc_contact(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    csv = f"name,organization,email,do_not_contact\nAvery Lin1,Rutgers,{EMAIL},yes\nNew Person,Elsewhere,new@x.example.edu,yes\n"
    svc.import_csv("contacts", csv, commit=True)
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"
    assert {r["email"] for r in svc.cache.q("SELECT email FROM suppressions")} == {EMAIL, "new@x.example.edu"}


def test_survives_restart(world):
    svc, fake, clock, cid, tmp = world
    cand, contact_id, row = scheduled_world(world)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    svc2 = make_service(tmp, fake, clock)
    svc2.outbox.recover()
    clock.t = at(2026, 9, 28, 15, 5)
    assert_nothing_goes_out(svc2, fake, clock)
    assert svc2.outbox.row(row["send_id"])["status"] == "cancelled"


def test_dnc_set_behind_the_scheduler_is_caught_before_gmail_draft(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    real = svc.outbox.revalidate

    def flip_after_validation(r):
        out = real(r)
        svc.cache.x("UPDATE people SET do_not_contact=1")  # lands after tick's check, before the claim
        return out

    svc.outbox.revalidate = flip_after_validation
    clock.t = at(2026, 9, 28, 15, 5)
    svc.outbox.tick()
    assert fake.n == 0 and fake.sent == []
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"


def test_dnc_during_draft_creation_blocks_drafts_send(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    done = threading.Event()
    seen = {}

    def dnc_arrives_mid_create():
        t = threading.Thread(
            target=lambda: (svc.ledger.update_contact(contact_id, {"do_not_contact": True}), done.set())
        )
        t.start()
        time.sleep(0.2)
        seen["blocked"] = not done.is_set()  # the DNC write waits for the in-flight Gmail call

    fake.on_create = dnc_arrives_mid_create
    clock.t = at(2026, 9, 28, 15, 5)
    svc.outbox.tick()
    assert done.wait(5) and seen["blocked"]
    assert fake.sent == [] and fake.n == 1  # the existing Gmail draft is kept, never sent
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"


def test_gmail_draft_creation_rechecks_dnc(world):
    import asyncio

    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    contact_id = link(svc, cid, cand)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    svc.cache.upsert_draft(cid, cand, svc.cache.get_draft(cid, cand)["template_version"], status="approved")
    out = asyncio.run(svc.gmail_create("k", False, lambda: None, (EMAIL, "s", "b"), who=(cid, cand)))
    assert out["result"].startswith("skipped") and fake.n == 0
    assert asyncio.run(svc.create_gmail_drafts(cid, [cand]))[0]["result"].startswith("skipped")
    assert fake.n == 0


def test_removing_dnc_does_not_resume_cancelled_work(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, row = scheduled_world(world)
    add_followups(svc, cid, cand)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    svc.ledger.update_contact(contact_id, {"do_not_contact": False})
    assert svc.outreach.contact(cid, cand)["do_not_contact"] == 0
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"
    assert set(statuses(svc, cid, cand).values()) == {"cancelled"}
    assert svc.outreach.contact(cid, cand)["sequence_state"] == "stopped"
    assert draft_status(svc, cid, cand) == "needs_review"
    assert "dnc_removed" in audit_events(svc)
    assert svc.cache.q("SELECT email FROM suppressions") == [{"email": EMAIL}]  # lifted only by an explicit human step
    clock.t = at(2026, 9, 28, 15, 5)
    assert_nothing_goes_out(svc, fake, clock)


def test_removed_dnc_resume_is_still_refused(world):
    svc, fake, clock, cid, _ = world
    cand, contact_id, _ = scheduled_world(world)
    add_followups(svc, cid, cand)
    svc.ledger.update_contact(contact_id, {"do_not_contact": True})
    svc.ledger.update_contact(contact_id, {"do_not_contact": False})
    with pytest.raises(ValueError):
        svc.outreach.sequence_action(cid, cand, "resume")


def test_correction_matching_a_dnc_contact_is_enforced(tmp_path):
    from tests.test_delivery_integration import world as dworld

    svc, provider, clock, workspace, identity, attachment, cid, cand = dworld(tmp_path)
    contact_id = workspace.set_do_not_contact(cid, cand, None, None, True, "stop")["id"]
    other = "c_other"
    svc.store.update_candidates(
        cid,
        lambda d: d["candidates"].append(
            {"candidate_id": other, "name": "Someone Else", "organization": "Elsewhere", "status": "researched"}
        ),
    )
    svc.store.update_research(
        cid,
        lambda d: d["profiles"].__setitem__(
            other, {"candidate_id": other, "name": "Someone Else", "status": "researched"}
        ),
    )
    svc.cache.upsert_draft(cid, other, "tv", status="approved", subject="s", body="b", issues=[])
    svc.correct(cid, other, "contact_email", "avery@example.edu")
    assert svc.cache.get_draft(cid, other)["status"] == "needs_review"
    assert svc.cache.q("SELECT 1 FROM suppressions WHERE email='avery@example.edu'")
    assert contact_id
