"""Operational metrics are fed by the real provider / SQLite / queue / backup paths, and the response is redacted."""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import gmail
from app.api import create_app
from app.cache import Cache
from app.config import Config
from app.jobs import Coordinator
from app.openai_client import ModelError, OpenAIModel
from app.operational_metrics import metrics
from tests.conftest import make_service
from tests.support import BIO, make_campaign, run_research_flow


class Boom(Exception):
    status_code = 503


class FlakyClient:
    def __init__(self, failures):
        self.failures, self.calls = failures, 0
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **_):
        self.calls += 1
        if self.calls <= self.failures:
            raise Boom("provider down")
        return SimpleNamespace(output_text='{"ok": true}', usage=None, output=[])


def model(tmp_path, failures):
    m = OpenAIModel(Cache(tmp_path / "c.sqlite3"), api_key="sk-test-0123456789abcdef")
    m.client = FlakyClient(failures)
    m.cache.set_budget("c1", 10)
    return m


@pytest.fixture
def no_backoff(monkeypatch):
    async def instant(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", instant)


def test_provider_attempts_and_retries_are_recorded(tmp_path, no_backoff):
    m = model(tmp_path, failures=2)
    calls, retries = metrics.provider.count, metrics.provider_retries
    assert asyncio.run(m.structured("c1", "i", "u", "s", {}))[0] == {"ok": True}
    assert metrics.provider.count == calls + 3  # two failures and the success each hit the provider
    assert metrics.provider_retries == retries + 2


def test_provider_failure_is_recorded_and_budget_refusal_is_not(tmp_path, no_backoff):
    m = model(tmp_path, failures=9)
    calls = metrics.provider.count
    with pytest.raises(ModelError):
        asyncio.run(m.structured("c1", "i", "u", "s", {}))
    assert metrics.provider.count == calls + 3
    m.cache.set_budget("c1", 1)  # the 3 attempts above used it up
    calls = metrics.provider.count
    with pytest.raises(Exception, match="budget"):
        asyncio.run(m.structured("c1", "i", "u", "s", {}))
    assert metrics.provider.count == calls  # refused locally, nothing went to the provider


def test_gmail_calls_are_timed():
    calls = metrics.provider.count
    assert gmail._run(SimpleNamespace(execute=lambda: {"id": "x"})) == {"id": "x"}
    with pytest.raises(RuntimeError):
        gmail._run(SimpleNamespace(execute=lambda: (_ for _ in ()).throw(RuntimeError("x"))))
    assert metrics.provider.count == calls + 2


def test_sqlite_calls_are_timed(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3")
    before = metrics.sqlite.count
    cache.q("SELECT 1")
    cache.x("SELECT 1")
    with cache.tx():
        pass
    assert metrics.sqlite.count >= before + 3


def test_queue_age_tracks_waiting_jobs_only(tmp_path):
    async def go():
        c = Coordinator(Cache(tmp_path / "c.sqlite3"), {"research": _noop}, research_workers=0, queue_size=4)
        c.loop = asyncio.get_running_loop()
        c.submit("cid", "research")
        assert [len(v) for v in c.queue_times().values()] == [1, 0]  # research, write
        snap = metrics.snapshot(queues=c.queue_times())
        assert snap["queues"]["research"]["depth"] == 1 and snap["queues"]["research"]["oldest_age_s"] >= 0
        w = Coordinator(Cache(tmp_path / "d.sqlite3"), {"research": _noop}, research_workers=1)
        w.start()
        w.submit("cid", "research")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not w.queue_times()["research"]:
                break
        assert w.queue_times() == {"research": [], "write": []}
        await w.close(0)

    asyncio.run(go())


async def _noop(*_):
    pass


def test_backup_age_comes_from_newest_completed_backup(tmp_path):
    svc = make_service(tmp_path / "data")
    assert svc.metrics_snapshot()["backup_age_s"] is None  # unknown unless HERMES_BACKUP_DIR is set
    svc.backup_dir = tmp_path / "backups"
    svc.backup_dir.mkdir()
    old, new = svc.backup_dir / "a.hbk", svc.backup_dir / "b.hbk"
    for p, age in ((old, 7200), (new, 600)):
        p.write_bytes(b"x")
        os.utime(p, (time.time() - age, time.time() - age))
    (svc.backup_dir / "c.hbk.part").write_bytes(b"x")  # an unfinished backup must not count
    assert 590 <= svc.metrics_snapshot()["backup_age_s"] <= 700
    assert svc.metrics_snapshot()["backup_verified_age_s"] is None  # nothing verifies restores yet


def test_metrics_endpoint_is_authenticated_and_contains_no_personal_data(tmp_path):
    svc = make_service(tmp_path / "data")
    secret_email = "someone.private@leaky.example"
    with TestClient(
        create_app(service=svc, token="t", config=Config(data_root=tmp_path)), headers={"x-app-token": "t"}
    ) as c:
        assert TestClient(c.app).get("/api/metrics").status_code == 401
        cid = make_campaign(c, "Rutgers professors working on computational neurodevelopment")
        c.put(f"/api/campaigns/{cid}/intake", json={"sender_background": BIO, "notes": secret_email})
        c.post(f"/api/campaigns/{cid}/rename", json={"name": "Dr Zed Quux https://leaky.example/x"})
        ids = run_research_flow(c, cid)
        names = [p["name"] for p in c.get(f"/api/campaigns/{cid}").json()["candidates"]]
        r = c.get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body)
    for needle in [secret_email, "leaky", "http", "@", "Quux", "sk-", "token", *names, *ids, cid]:
        assert needle not in text, needle
    assert set(body["queues"]) == {"research", "write"}
    assert body["sqlite"]["count"] > 0 and body["disk"]["free_bytes"] > 0 and body["workers_alive"] is True
