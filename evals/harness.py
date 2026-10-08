"""Offline deterministic accuracy harness. No network, no credentials, no email.

Drives the real discovery/research/outline/draft-check/DNC code against fixtures in cases.json
with scripted model output and an in-memory HTTP transport, then scores the results against gold labels.
Run: python -m evals.run
"""

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app import research
from app.cache import Cache
from app.contracts import profile_problems
from app.openai_client import Citations
from app.outlines import OutlineBlocked, build_outline
from app.workspace import Workspace
from app.writer import check_draft

HERE = Path(__file__).parent
NOW = datetime(2026, 6, 1, tzinfo=UTC)  # fixed clock: freshness results never depend on the day the eval runs
BIO = "I'm Nitu, a Rutgers undergraduate studying computer science."


class ScriptedModel:
    """Returns canned (data, Citations) per schema, like OpenAIModel.structured."""

    def __init__(self, out):
        self.out = out

    async def structured(self, *a, **k):
        return self.out


def _fetcher(cache, pages):
    def handler(request):
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="missing")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return research.Fetcher(cache, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ratio(num, den, empty=1.0):
    return num / den if den else empty


async def _run_case(case, cache):
    r = {"id": case["id"], "identity": [0, 0], "evidence": [0, 0], "email": [0, 0], "unsupported": [0, 0]}
    gold = case["gold"]
    if d := case.get("discovery"):
        out = await research.discover(
            ScriptedModel(({"people": d["people"]}, Citations(d["annotations"], d["inventory"]))), "eval", {}, 10
        )
        truth = {(p["name"], p["organization"]): p["correct"] for p in d["people"]}
        r["identity"] = [sum(truth[(p["name"], p["organization"])] for p in out), len(out)]
    profiles = list(case.get("stored_profiles") or [])
    if "model" in case:
        cited = Citations(case["annotations"], case.get("inventory", []))
        profile = await research.research_candidate(
            ScriptedModel((case["model"], cited)),
            _fetcher(cache, case["pages"]),
            "eval",
            {"subtype": case.get("subtype")},
            case["candidate"],
        )
        profiles.append(profile)
        supported, fabricated = set(gold["supported"]), set(gold.get("fabricated", []))
        ev = profile["evidence"]
        r["evidence"] = [sum(e["claim"] in supported for e in ev), len(ev)]
        if profile["email_verified_on_page"]:
            r["email"] = [int(profile["contact_email"] == gold["email"]), 1]
        items = [(profile["summary"], "prose"), (profile["fit_reason"], "prose")]
        items += [(e["claim"], "prose") for e in ev] + [(i, "interest") for i in profile["research_interests"]]
        outline = None
        try:
            outline = build_outline({"subtype": case.get("subtype")}, profile, BIO)
        except OutlineBlocked:
            pass
        r["draft_built"] = outline is not None
        if outline:
            items.append((outline["specific_connection"], "prose"))
        items = [(t, k) for t, k in items if t]
        r["unsupported"] = [sum(t in fabricated or (k == "prose" and t not in supported) for t, k in items), len(items)]
        r["draft_expected"] = bool(gold["draft"])
        r["unsupported_draft_built"] = bool(outline and not gold["draft"])
        if gold["draft"] and (draft := case.get("draft")) and outline:
            ids = [i for i in draft["ids"] if i in outline["evidence_ids"]]
            by_id = {e["id"]: e for e in ev}
            clean = not check_draft(draft["subject"], draft["body"], outline, profile, ids)
            r["draft_covered"] = bool(ids) and clean and all(by_id[i]["web_verified"] for i in ids)
        elif gold["draft"]:
            r["draft_covered"] = False
    profiles = [research.annotate_freshness(p, NOW) for p in profiles]
    r["label_violations"] = sum(len(profile_problems(p)) for p in profiles)
    r["stale"] = [sum(e.get("freshness") == "stale" for p in profiles for e in p["evidence"]), gold.get("stale", 0)]
    return r


def _dnc(spec, tmp):
    ws = Workspace(Cache(Path(tmp) / "dnc.db"), tmp)
    s = spec["seed"]
    cand = {"name": s["name"], "organization": s["organization"], "profile_url": s["profile_url"]}
    ws.set_do_not_contact("seed", "c_seed", cand, {"contact_email": s["email"]}, True, "eval")
    missed = blocked_expected = 0
    for n, p in enumerate(spec["probes"]):
        c = {"name": p["name"], "organization": p["organization"], "profile_url": p["profile_url"]}
        hit, _ = ws.is_do_not_contact("other", f"c_probe{n}", c, {"contact_email": p["email"]})
        if p["block"]:
            blocked_expected += 1
            missed += not hit
    return missed, blocked_expected


def run(cases_path=HERE / "cases.json"):
    spec = json.loads(Path(cases_path).read_text())
    with tempfile.TemporaryDirectory() as tmp:
        cache = Cache(Path(tmp) / "eval.db")
        cache.set_budget("eval", 999)
        rows = [asyncio.run(_run_case(c, cache)) for c in spec["cases"]]
        missed, expected = _dnc(spec["dnc"], tmp)
    tot = lambda key, i: sum(r[key][i] for r in rows)
    drafts = [r for r in rows if r.get("draft_expected")]
    metrics = {
        "candidate_identity_precision": _ratio(tot("identity", 0), tot("identity", 1)),
        "evidence_support_precision": _ratio(tot("evidence", 0), tot("evidence", 1)),
        "verified_email_precision": _ratio(tot("email", 0), tot("email", 1)),
        "unsupported_personalization_rate": _ratio(tot("unsupported", 0), tot("unsupported", 1), 0.0),
        "draft_evidence_coverage": _ratio(sum(r["draft_covered"] for r in drafts), len(drafts)),
        "dnc_false_negative_rate": _ratio(missed, expected, 0.0),
        "label_violations": sum(r["label_violations"] for r in rows),
        "unsupported_drafts_built": sum(r.get("unsupported_draft_built", False) for r in rows),
        "stale_labelled": _ratio(sum(r["stale"][0] == r["stale"][1] for r in rows), len(rows)),
    }
    return metrics, rows


def check(metrics, thresholds=None):
    """-> list of regression messages (empty = pass)."""
    thresholds = thresholds or json.loads((HERE / "thresholds.json").read_text())
    bad = []
    for name, bound in thresholds.items():
        v = metrics[name]
        if "min" in bound and v < bound["min"]:
            bad.append(f"{name}={v:.3f} below minimum {bound['min']}")
        if "max" in bound and v > bound["max"]:
            bad.append(f"{name}={v:.3f} above maximum {bound['max']}")
    return bad
