import asyncio

from app import demo
from app.cache import Cache
from app.campaigns import CampaignService
from app.config import Config
from app.storage import CampaignStore


def make(tmp_path, **cfg):
    cache = Cache(tmp_path / "cache.sqlite3")
    return CampaignService(
        CampaignStore(tmp_path),
        cache,
        demo.DemoModel(cache),
        demo.demo_fetcher(cache),
        send_every=None,
        config=Config(data_root=tmp_path, **cfg),
    )


def test_service_uses_config_for_capacity(tmp_path):
    svc = make(tmp_path, research_workers=3, job_queue_size=7)
    assert svc.jobs.workers == {"research": 3, "write": 1}
    assert svc.jobs.queues["research"].maxsize == svc.jobs.queues["write"].maxsize == 7


def test_gmail_sync_interval_comes_from_config_not_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_SYNC_MINUTES", "1")  # legacy env var must be ignored
    seen = []

    async def go():
        svc = make(tmp_path, gmail_sync_interval=123.0)

        async def fake_loop(seconds):
            seen.append(seconds)

        svc._sync_loop = fake_loop
        svc.startup()
        await asyncio.sleep(0)
        await svc.shutdown(0)

    asyncio.run(go())
    assert seen == [123.0]
