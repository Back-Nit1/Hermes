import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from app.contracts import profile_problems
from app.openai_client import Citations, _cited_urls
from app.research import annotate_freshness, discover, finalize_profile, source_kinds

URL = "https://o.edu/a"
CAND = {"candidate_id": "c_1", "name": "Ada Lovelace", "organization": "Example University", "role": "Prof"}
PAGE = "Ada Lovelace, Example University. Ada studies analytical engines. Email: ada@o.edu"
FE = {"summary": [0], "research_interests": [0], "fit_reason": [0], "affiliation": [0]}


def data(**kw):
    base = {
        "contact_email": None,
        "contact_source_url": None,
        "summary": "Ada studies engines.",
        "research_interests": ["engines"],
        "fit_reason": "Matches focus.",
        "evidence": [{"claim": "Ada studies analytical engines", "source_url": URL, "source_locator": None}],
        "field_evidence": FE,
    }
    return base | kw


class Model:
    def __init__(self, people, cited):
        self.out = ({"people": people}, cited)

    async def structured(self, *a, **k):
        return self.out


def person(src, name="Ada Lovelace"):
    return {
        "name": name,
        "organization": "O",
        "role": "R",
        "profile_url": None,
        "discovery_source_url": src,
        "fit_hint": "",
    }


def test_cited_urls_split_annotations_from_search_inventory():
    resp = NS(
        output=[
            NS(content=[NS(annotations=[NS(url="https://a.org/x")])], action=None),
            NS(content=None, action=NS(sources=[NS(url="https://b.org/y")])),
        ]
    )
    c = _cited_urls(resp)
    assert c.annotations == {"https://a.org/x"} and c.inventory == {"https://b.org/y"}
    assert c == {"https://a.org/x", "https://b.org/y"}  # still a plain set for old callers


def test_discovery_rejects_person_whose_source_was_never_returned_or_fetched():
    cited = Citations({"https://o.edu/ann"}, {"https://o.edu/inv"})
    people = [
        person("https://o.edu/ann", "A One"),
        person("https://o.edu/inv", "B Two"),
        person("https://x.io/z", "C Three"),
    ]
    out = asyncio.run(discover(Model(people, cited), "c", {}, 5))
    assert [(p["name"], p["discovery_source_kind"]) for p in out] == [
        ("A One", "claim_annotation"),
        ("B Two", "search_result"),
    ]
    out = asyncio.run(discover(Model([person("https://x.io/z")], set()), "c", {}, 5, fetched_urls={"https://x.io/z"}))
    assert out[0]["discovery_source_kind"] == "fetched"


def test_source_kinds_prefers_fetched_over_citation_over_inventory():
    k = source_kinds(
        Citations({"https://a/1"}, {"https://a/1", "https://a/2"}), {"https://a/3": "t", "https://a/1": "t"}
    )
    assert k == {"https://a/1": "fetched", "https://a/2": "search_result", "https://a/3": "fetched"}


def test_unfetched_citation_is_never_web_verified():
    kinds = source_kinds(Citations({URL}, set()), {})
    p = finalize_profile(CAND, data(), {URL}, {}, kinds=kinds)
    (e,) = p["evidence"]
    assert e["web_verified"] is False and e["source_kind"] == "claim_annotation" and e["id"] == "e0"
    p = finalize_profile(CAND, data(), {URL}, {URL: PAGE}, strict_grounding=True)
    assert p["evidence"][0]["web_verified"] is True and p["evidence"][0]["source_kind"] == "fetched"


def test_inventory_only_url_is_labelled_search_result():
    kinds = source_kinds(Citations(set(), {URL}), {})
    assert finalize_profile(CAND, data(), set(kinds), {}, kinds=kinds)["evidence"][0]["source_kind"] == "search_result"


def test_prose_without_field_evidence_is_parked_not_kept():
    fe = {"summary": [], "research_interests": [], "fit_reason": [5], "affiliation": []}
    p = finalize_profile(CAND, data(field_evidence=fe), {URL}, {URL: PAGE})
    assert p["summary"] == "" and p["fit_reason"] == "" and p["research_interests"] == []
    assert p["notes"]["unsupported_fields"] == ["fit_reason", "research_interests", "summary"]
    assert p["notes"]["unverified_model_note"]["fit_reason"] == "Matches focus."
    assert p["notes"]["affiliation_supported"] is False
    assert p["evidence"]  # the sourced fact itself survives


def test_field_evidence_ids_follow_surviving_evidence():
    ev = [
        {"claim": "Ada won a Nobel prize", "source_url": URL, "source_locator": None},  # dropped: not on page
        {"claim": "Ada studies analytical engines", "source_url": URL, "source_locator": None},
    ]
    fe = {"summary": [0, 1], "research_interests": [1], "fit_reason": [0], "affiliation": [1]}
    p = finalize_profile(CAND, data(evidence=ev, field_evidence=fe), {URL}, {URL: PAGE}, strict_grounding=True)
    assert p["field_evidence"] == {
        "summary": ["e0"],
        "research_interests": ["e0"],
        "fit_reason": [],
        "affiliation": ["e0"],
    }
    assert p["fit_reason"] == "" and p["summary"]


def test_email_verification_needs_a_whole_address():
    d = data(contact_email="da@o.edu", contact_source_url=URL)
    assert finalize_profile(CAND, d, {URL}, {URL: "reach ada@o.edu"})["email_verified_on_page"] is False
    d = data(contact_email="ada@o.edu", contact_source_url=URL)
    assert finalize_profile(CAND, d, {URL}, {URL: PAGE})["email_verified_on_page"] is True


def test_page_about_someone_else_cannot_support_claims():
    other = "Grace Hopper, Other College. Grace studies analytical engines."
    p = finalize_profile(CAND, data(), {URL}, {URL: other}, strict_grounding=True)
    assert p["evidence"] == [] and p["status"] == "research_failed"
    same_name_elsewhere = "Ada Lovelace, Faraway Institute of Cooking. Ada studies analytical engines."
    p = finalize_profile(
        CAND, data(), {"https://cook.io/a"}, {"https://cook.io/a": same_name_elsewhere}, strict_grounding=True
    )
    assert p["evidence"] == []


def test_stale_sources_are_labelled_without_refetching():
    old = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prof = {"evidence": [{"claim": "x", "retrieved_at": old}, {"claim": "y", "retrieved_at": "2020"}]}
    annotate_freshness(prof)
    assert [e["freshness"] for e in prof["evidence"]] == ["stale", "unknown"]
    assert prof["evidence"][0]["source_age_days"] >= 200 and prof["notes"]["stale_sources"] == 1


def test_manual_correction_must_stay_labelled_unverified():
    ev = {"claim": "c", "source_url": URL, "retrieved_at": "t", "provenance": "manual_correction"}
    assert profile_problems({"candidate_id": "c", "evidence": [{**ev, "web_verified": False}]}) == []
    assert profile_problems({"candidate_id": "c", "evidence": [{**ev, "web_verified": True}]}) == [
        "manual evidence mislabeled as web verified"
    ]


def test_email_on_a_page_about_someone_else_is_not_verified():
    page = "Grace Hopper, Other College. Contact grace@other.edu and ada@o.edu"
    d = data(contact_email="ada@o.edu", contact_source_url=URL)
    assert finalize_profile(CAND, d, {URL}, {URL: page}, strict_grounding=True)["email_verified_on_page"] is False
