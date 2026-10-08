"""Chrome-extension API: CORS/preflight, pairing, token hashing, expiry, revocation, scopes, isolation, caps, DNC."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import extension_api
from app.api import create_app
from tests.conftest import make_service
from tests.support import make_campaign
from tests.test_remote import PW, As, remote  # noqa: F401 - remote is a fixture

ORIGIN = "chrome-extension://" + "a" * 32
OTHER = "chrome-extension://" + "b" * 32
DASH = {"x-app-token": "t"}
NO_ROOT = {"x-app-token": ""}  # the TestClient default header is overridden: extension calls carry no root token
EXT = "/extension/v1"
BIO_REQ = "Rutgers/Princeton professors working on computational neurodevelopment"


@pytest.fixture(autouse=True)
def _origins(monkeypatch):  # autouse so it runs before fixtures that build the app (e.g. remote)
    monkeypatch.setenv("HERMES_EXTENSION_ORIGINS", ORIGIN)


@pytest.fixture
def ext(tmp_path):
    svc = make_service(tmp_path / "data")
    with TestClient(create_app(service=svc, token="t"), headers=DASH) as c:
        c.svc, c.root = svc, tmp_path / "data"
        yield c


def call(c, method, path, token=None, origin=ORIGIN, **kw):
    h = {**NO_ROOT, **kw.pop("headers", {})}
    if origin:
        h["Origin"] = origin
    if token:
        h["Authorization"] = f"Bearer {token}"
    return c.request(method, EXT + path, headers=h, **kw)


def pair(c, scopes=None, name="laptop"):
    r = c.post("/api/extension/pairing-codes", json={"scopes": scopes} if scopes else {})
    assert r.status_code == 200, r.text
    r = call(c, "POST", "/pair/exchange", json={"code": r.json()["code"], "device_name": name})
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["device"]


def db(c):
    d = sqlite3.connect(c.root / "extension.sqlite3")
    d.row_factory = sqlite3.Row
    return d


def cap(cid, **kw):
    return {
        "campaign_id": cid,
        "url": "https://www.example.edu/people/ada",
        "title": "Ada Lovelace",
        "selection": "Works on analytical engines.",
        "metadata": {"organization": "Example University", "role": "Professor"},
        **kw,
    }


# ------------------------------------------------------------------ CORS / preflight
def test_preflight_is_tokenless_exact_origin_and_scoped_to_prefix(ext):
    r = call(
        ext,
        "OPTIONS",
        "/capture/commit",
        headers={
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["vary"] == "Origin"
    assert "*" not in "".join(r.headers.values())
    bad = [
        dict(origin=OTHER),
        dict(origin="*"),
        dict(origin=None),
        dict(origin="https://evil.example"),
        dict(origin=ORIGIN + ".evil.com"),
    ]
    for kw in bad:
        r = call(ext, "OPTIONS", "/status", headers={"Access-Control-Request-Method": "GET"}, **kw)
        assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    pre = {"Access-Control-Request-Method": "PUT"}
    assert call(ext, "OPTIONS", "/status", headers=pre).status_code == 403
    assert call(ext, "OPTIONS", "/device", headers={"Access-Control-Request-Method": "POST"}).status_code == 403
    assert call(ext, "OPTIONS", "/nope", headers={"Access-Control-Request-Method": "GET"}).status_code == 403
    hdr = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-app-token"}
    assert call(ext, "OPTIONS", "/status", headers=hdr).status_code == 403
    # outside the prefix nothing is tokenless
    r = ext.request("OPTIONS", "/api/campaigns", headers={**NO_ROOT, "Origin": ORIGIN})
    assert r.status_code == 401


def test_every_request_needs_an_allowlisted_origin_and_responses_echo_only_it(ext):
    tok, _ = pair(ext)
    assert call(ext, "GET", "/status", tok, origin=None).status_code == 403
    assert call(ext, "GET", "/status", tok, origin=OTHER).status_code == 403
    r = call(ext, "GET", "/status", tok)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["cache-control"] == "no-store"
    assert call(ext, "GET", "/status", origin=ORIGIN).headers["access-control-allow-origin"] == ORIGIN  # 401 too


def test_empty_allowlist_refuses_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_EXTENSION_ORIGINS", raising=False)
    with TestClient(create_app(service=make_service(tmp_path / "d"), token="t"), headers=DASH) as c:
        assert call(c, "POST", "/pair/exchange", json={"code": "ABCDEFGHJK", "device_name": "x"}).status_code == 403


def test_origin_config_only_accepts_exact_extension_ids():
    env = {"HERMES_EXTENSION_ORIGINS": f"{ORIGIN}, *, https://x.com, chrome-extension://short, {OTHER}/"}
    assert extension_api.configured_origins(env) == {ORIGIN}


# ------------------------------------------------------------------ pairing, hashing, expiry, revocation
def test_pairing_code_is_one_time_and_tokens_are_stored_hashed(ext):
    code = ext.post("/api/extension/pairing-codes", json={}).json()["code"]
    body = {"code": code, "device_name": "mbp"}
    r = call(ext, "POST", "/pair/exchange", json=body)
    token = r.json()["token"]
    assert token.startswith("hxe_") and len(token) > 40
    assert call(ext, "POST", "/pair/exchange", json=body).status_code == 401  # replay
    assert call(ext, "POST", "/pair/exchange", json={**body, "code": "WRONGCODE123"}).status_code == 401
    raw = b"".join(p.read_bytes() for p in ext.root.glob("extension.sqlite3*"))
    assert token.encode() not in raw and code.encode() not in raw
    row = db(ext).execute("SELECT * FROM devices").fetchone()
    assert row["token_hash"] != token and len(row["token_hash"]) == 64
    assert row["name"] == "mbp" and row["user_id"] == 0 and row["created_at"] and row["expires_at"] > row["created_at"]
    assert (ext.root / "extension.sqlite3").stat().st_mode & 0o777 == 0o600
    assert call(ext, "GET", "/status", token).json()["device"]["name"] == "mbp"
    assert db(ext).execute("SELECT last_used_at FROM devices").fetchone()[0] is not None


def test_pairing_code_expires(ext):
    code = ext.post("/api/extension/pairing-codes", json={}).json()["code"]
    d = db(ext)
    d.execute("UPDATE pairing_codes SET expires_at=1")
    d.commit()
    assert call(ext, "POST", "/pair/exchange", json={"code": code, "device_name": "x"}).status_code == 401


def test_pairing_codes_need_dashboard_auth(ext):
    assert ext.post("/api/extension/pairing-codes", json={}, headers=NO_ROOT).status_code == 401
    tok, _ = pair(ext)
    r = ext.post("/api/extension/pairing-codes", json={}, headers={**NO_ROOT, "Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    assert ext.post("/api/extension/pairing-codes", json={"scopes": ["root"]}).status_code == 400
    assert ext.post("/api/extension/pairing-codes", json={"admin": 1}).status_code == 400


def test_device_token_expiry(ext):
    tok, _ = pair(ext)
    d = db(ext)
    d.execute("UPDATE devices SET expires_at=1")
    d.commit()
    assert call(ext, "GET", "/status", tok).status_code == 401


def test_device_self_revocation_and_dashboard_revocation(ext):
    a, _ = pair(ext, name="a")
    b, dev_b = pair(ext, name="b")
    assert call(ext, "DELETE", "/device", a).json() == {"revoked": True}
    assert call(ext, "GET", "/status", a).status_code == 401
    listed = ext.get("/api/extension/devices").json()
    assert {d["name"]: d["revoked_at"] is not None for d in listed} == {"a": True, "b": False}
    assert "token_hash" not in listed[0]
    assert ext.delete(f"/api/extension/devices/{dev_b['id']}").status_code == 200
    assert call(ext, "GET", "/status", b).status_code == 401
    assert ext.delete(f"/api/extension/devices/{dev_b['id']}").status_code == 404  # already revoked


def test_bad_tokens(ext):
    assert call(ext, "GET", "/status").status_code == 401
    for t in ("hxe_nope", "x" * 500, "Bearer"):
        assert call(ext, "GET", "/status", t).status_code == 401


# ------------------------------------------------------------------ scopes
def test_scope_denial(ext):
    tok, dev = pair(ext, scopes=["status", "capture:preview"])
    assert dev["scopes"] == ["capture:preview", "status"]
    cid = make_campaign(ext, BIO_REQ)
    assert call(ext, "GET", "/status", tok).status_code == 200
    assert call(ext, "GET", "/campaigns", tok).status_code == 403
    assert call(ext, "GET", "/notifications", tok).status_code == 403
    assert call(ext, "GET", "/review-queue", tok).status_code == 403
    assert call(ext, "POST", "/capture/preview", tok, json=cap(cid)).status_code == 200
    assert call(ext, "POST", "/capture/commit", tok, json=cap(cid)).status_code == 403
    assert ext.svc.store.candidates(cid)["candidates"] == []
    assert call(ext, "DELETE", "/device", tok).status_code == 200  # self-revoke needs no scope


# ------------------------------------------------------------------ capture
def test_capture_commit_adds_one_candidate(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    pre = call(ext, "POST", "/capture/preview", tok, json=cap(cid)).json()
    assert pre["decision"] == "ok" and pre["can_commit"] and pre["existing_contact"] is None
    assert ext.svc.store.candidates(cid)["candidates"] == []  # preview writes nothing
    r = call(ext, "POST", "/capture/commit", tok, json=cap(cid))
    assert r.status_code == 200, r.text
    (c,) = ext.svc.store.candidates(cid)["candidates"]
    assert c["name"] == "Ada Lovelace" and c["status"] == "discovered"
    assert c["profile_url"] == "https://example.edu/people/ada" and c["fit_hint"] == "Works on analytical engines."
    assert ext.svc.ledger.linked(cid, c["candidate_id"])
    again = call(ext, "POST", "/capture/commit", tok, json=cap(cid))
    assert again.status_code == 409 and again.json()["decision"] == "duplicate"
    assert len(ext.svc.store.candidates(cid)["candidates"]) == 1
    assert [x["campaign_id"] for x in call(ext, "GET", "/campaigns", tok).json()] == [cid]


def test_capture_reports_existing_contact(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    ext.svc.ledger.upsert_person(
        {"name": "Ada Lovelace", "organization": "Example University", "email": "ada@example.edu"}, "manual"
    )
    pre = call(ext, "POST", "/capture/preview", tok, json=cap(cid)).json()
    assert pre["decision"] == "ok" and pre["existing_contact"]["name"] == "Ada Lovelace"
    assert "email" not in pre["existing_contact"]
    assert call(ext, "POST", "/capture/commit", tok, json=cap(cid)).status_code == 200


def test_capture_blocks_do_not_contact_and_suppressed(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    ext.svc.ledger.upsert_person(
        {"name": "Ada Lovelace", "organization": "Example University", "do_not_contact": True, "dnc_reason": "asked"},
        "manual",
    )
    pre = call(ext, "POST", "/capture/preview", tok, json=cap(cid)).json()
    assert pre["decision"] == "blocked_dnc" and pre["do_not_contact"] and not pre["can_commit"]
    r = call(ext, "POST", "/capture/commit", tok, json=cap(cid))
    assert r.status_code == 409 and r.json()["decision"] == "blocked_dnc"
    assert ext.svc.store.candidates(cid)["candidates"] == []
    ext.svc.outbox.suppress("grace@example.edu", "do_not_contact")
    g = cap(cid, title="Grace Hopper", metadata={"email": "grace@example.edu", "organization": "Navy"})
    assert call(ext, "POST", "/capture/commit", tok, json=g).status_code == 409


def test_capture_ambiguous_match_is_refused_even_if_one_is_dnc(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    led = ext.svc.ledger
    for email in ("a1@example.edu", "a2@example.edu"):
        led.create_contact(
            {"name": "Ada Lovelace", "organization": "Example University", "email": email}, source="manual"
        )
    pre = call(ext, "POST", "/capture/preview", tok, json=cap(cid)).json()
    assert pre["decision"] == "ambiguous" and len(pre["ambiguous_matches"]["contacts"]) == 2
    assert call(ext, "POST", "/capture/commit", tok, json=cap(cid)).status_code == 409
    led.update_contact(2, {"do_not_contact": 1})
    assert call(ext, "POST", "/capture/preview", tok, json=cap(cid)).json()["decision"] == "blocked_dnc"
    assert ext.svc.store.candidates(cid)["candidates"] == []


def test_capture_input_validation(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    p = lambda body: call(ext, "POST", "/capture/preview", tok, json=body).status_code  # noqa: E731
    assert p(cap(cid)) == 200
    assert p(cap(cid, selection="x" * 2001)) == 422
    assert p(cap(cid, title="x" * 301)) == 422
    assert p(cap(cid, url="https://e.com/" + "a" * 2048)) == 422
    assert p(cap(cid, extra="field")) == 422
    assert p(cap(cid, metadata={"organization": "x", "evil": "y"})) == 422
    assert p(cap(cid, metadata={"name": "x" * 121})) == 422
    assert p(cap(cid, campaign_id="../../etc")) == 422
    assert p(cap(cid, url="javascript:alert(1)")) == 400
    assert p(cap(cid, url="file:///etc/passwd")) == 400
    assert p(cap(cid, title="")) == 400  # no name anywhere
    assert p(cap(cid, metadata={"name": "A B", "email": "not-an-email"})) == 400
    assert p(cap("cmp_20200101_deadbeef")) == 404
    assert p(cap(cid, selection=123)) == 422  # strict types


def test_body_size_content_type_and_method_limits(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    big = cap(cid, selection="x" * 40000)
    assert call(ext, "POST", "/capture/preview", tok, json=big).status_code == 413
    assert (
        call(ext, "POST", "/capture/preview", tok, content=b"{}", headers={"content-type": "text/plain"}).status_code
        == 415
    )
    chunked = call(
        ext, "POST", "/capture/preview", tok, content=iter([b"{}"]), headers={"content-type": "application/json"}
    )
    assert chunked.status_code == 411
    assert (
        call(ext, "POST", "/pair/exchange", content=b"{}", headers={"content-type": "application/json"}).status_code
        == 422
    )
    assert call(ext, "GET", "/status", tok, content=b"x" * 10).status_code == 413
    assert call(ext, "POST", "/status", tok, json={}).status_code == 405


# ------------------------------------------------------------------ queue / notifications
def test_review_queue_and_notifications_are_redacted_reads(ext):
    tok, _ = pair(ext)
    ext.svc.ledger.upsert_person(
        {"name": "Ada L", "organization": "X", "email": "a@x.org", "profile_url": "https://x.org/a"}, "manual"
    )
    ext.svc.ledger.upsert_person(
        {"name": "Ada L", "organization": "X", "email": "b@x.org", "profile_url": "https://x.org/b"}, "manual"
    )
    ext.svc.ledger.upsert_person(
        {"name": "Ada L", "organization": "X", "email": "c@x.org"}, "manual"
    )  # ambiguous -> review
    analytics_notify(ext.svc)
    q = call(ext, "GET", "/review-queue", tok).json()
    assert q["drafts_needing_review"] == [] and len(q["contact_reviews"]) == 2
    assert "@x.org" not in str(q)
    n = call(ext, "GET", "/notifications", tok).json()
    assert n["unread"] == 1 and n["items"][0]["message"] == "hello"


def analytics_notify(svc):
    from app import analytics

    analytics.notify(svc.cache, "k", "info", "hello")


# ------------------------------------------------------------------ rate limits
def test_pairing_attempts_are_rate_limited(ext):
    codes = [
        call(ext, "POST", "/pair/exchange", json={"code": "WRONGCODE123", "device_name": "x"}).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_failed_token_attempts_are_rate_limited(ext):
    codes = [call(ext, "GET", "/status", "hxe_bad").status_code for _ in range(31)]
    assert codes[29] == 401 and codes[30] == 429


def test_per_device_rate_limit(ext, monkeypatch):
    monkeypatch.setattr(extension_api, "DEVICE_RATE", (3, 60))
    tok, _ = pair(ext)
    assert [call(ext, "GET", "/status", tok).status_code for _ in range(4)] == [200, 200, 200, 429]


# ------------------------------------------------------------------ root API stays out of reach
def test_device_token_cannot_reach_root_routes_or_unexposed_actions(ext):
    tok, _ = pair(ext)
    cid = make_campaign(ext, BIO_REQ)
    h = {**NO_ROOT, "Authorization": f"Bearer {tok}", "Origin": ORIGIN}
    for method, path in [
        ("GET", "/api/campaigns"),
        ("GET", "/api/status"),
        ("PATCH", "/api/sending/settings"),
        ("POST", "/api/sending/emergency-stop"),
        ("DELETE", f"/api/campaigns/{cid}"),
        ("GET", "/api/extension/devices"),
    ]:
        assert ext.request(method, path, headers=h, json={} if method != "GET" else None).status_code == 401, path
    assert ext.get("/api/campaigns").status_code == 200  # dashboard auth is unaffected
    for method, path in [
        ("POST", f"/campaigns/{cid}/drafts/c_001/approve"),
        ("POST", f"/campaigns/{cid}/gmail-drafts"),
        ("POST", "/sends"),
        ("PATCH", "/sending/settings"),
        ("POST", "/sending/emergency-stop"),
        ("DELETE", f"/campaigns/{cid}"),
        ("GET", "/api/campaigns"),
        ("GET", "/../api/campaigns"),
    ]:
        r = call(ext, method, path, tok, json={} if method != "GET" else None)
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert ext.get("/api/campaigns").json()[0]["campaign_id"] == cid


# ------------------------------------------------------------------ remote mode: per-user isolation
def test_remote_devices_are_bound_to_their_user(remote):  # noqa: F811
    app, cfg, anon, _ = remote
    alice, bob = As(anon, "alice"), As(anon, "bob")
    a_cid = make_campaign_remote(alice)
    b_cid = make_campaign_remote(bob)

    def pair_for(user):
        code = user.post("/api/extension/pairing-codes", json={}).json()["code"]
        r = anon.post(EXT + "/pair/exchange", json={"code": code, "device_name": "d"}, headers={"Origin": ORIGIN})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def get(path, tok, **kw):
        return anon.get(EXT + path, headers={"Origin": ORIGIN, "Authorization": f"Bearer {tok}"}, **kw)

    ta, tb = pair_for(alice), pair_for(bob)
    assert [c["campaign_id"] for c in get("/campaigns", ta).json()] == [a_cid]
    assert [c["campaign_id"] for c in get("/campaigns", tb).json()] == [b_cid]
    body = cap(b_cid)
    r = anon.post(EXT + "/capture/preview", json=body, headers={"Origin": ORIGIN, "Authorization": f"Bearer {ta}"})
    assert r.status_code == 404  # alice's service has no such campaign
    # dashboard: alice cannot list or revoke bob's device
    assert len(alice.get("/api/extension/devices").json()) == 1
    bob_dev = bob.get("/api/extension/devices").json()[0]["id"]
    assert alice.delete(f"/api/extension/devices/{bob_dev}").status_code == 404
    assert get("/status", tb).status_code == 200
    # cookies never authenticate the extension API, and device tokens never authenticate /api
    assert anon.get(EXT + "/status", headers={"Origin": ORIGIN, **alice.headers}).status_code == 401
    assert anon.get("/api/campaigns", headers={"Authorization": f"Bearer {ta}"}).status_code == 401
    # a disabled account loses its devices immediately
    from app.auth import Accounts

    app.state.runtime["accounts"].set_disabled("alice")
    assert get("/status", ta).status_code == 401
    assert get("/status", tb).status_code == 200
    # extension POSTs with a chrome-extension Origin are not blocked by the dashboard's cross-origin guard
    assert (
        anon.post(
            EXT + "/capture/preview", json=cap(b_cid), headers={"Origin": ORIGIN, "Authorization": f"Bearer {tb}"}
        ).status_code
        == 200
    )
    assert Accounts  # imported for clarity of what set_disabled belongs to


def make_campaign_remote(user):
    parsed = user.post("/api/parse", json={"text": BIO_REQ}).json()
    body = {"intake": {**parsed["intake"], "sender_background": "I'm a student."}, "max_candidates": 20, "budget": 40}
    return user.post("/api/campaigns", json=body).json()["campaign_id"]
