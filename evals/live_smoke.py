"""Opt-in live smoke eval. Never sends email; never runs in CI or pytest.

    EVAL_LIVE=1 OPENAI_API_KEY=... python -m evals.live_smoke

Spends at most CALL_BUDGET OpenAI calls and fetches a handful of public pages. It imports no Gmail,
sending, or auth code, and checks the same provenance invariants the offline harness enforces.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

CALL_BUDGET = 3
BRIEF = {
    "mode": "research",
    "subtype": "research_professor",
    "organizations": ["Rutgers University"],
    "research_areas": ["machine learning"],
}


async def main():
    from app import research
    from app.cache import Cache
    from app.openai_client import OpenAIModel

    with tempfile.TemporaryDirectory() as tmp:
        cache = Cache(Path(tmp) / "live.db")
        cache.set_budget("live", CALL_BUDGET)
        model, fetcher = OpenAIModel(cache), research.Fetcher(cache)
        people = await research.discover(model, "live", BRIEF, 1)
        assert people, "discovery returned no person with a returned/fetched source"
        assert all(p["discovery_source_kind"] in ("claim_annotation", "search_result", "fetched") for p in people)
        cand = {
            "candidate_id": "c_live",
            **{k: people[0].get(k) for k in ("name", "organization", "role", "profile_url", "discovery_source_url")},
        }
        profile = await research.research_candidate(model, fetcher, "live", BRIEF, cand)
        for e in profile["evidence"]:
            assert e["web_verified"] == (e["source_kind"] == "fetched"), e
        assert not profile["fit_reason"] or profile["field_evidence"]["fit_reason"]
        print(
            {
                "candidate": cand["name"],
                "status": profile["status"],
                "evidence": len(profile["evidence"]),
                "web_verified": sum(e["web_verified"] for e in profile["evidence"]),
                "api_calls": cache.usage("live")["api_calls"],
            }
        )


if os.environ.get("EVAL_LIVE") != "1" or not os.environ.get("OPENAI_API_KEY"):
    sys.exit("live smoke is opt-in: set EVAL_LIVE=1 and OPENAI_API_KEY")
asyncio.run(main())
