"""Strict request models for /api and /auth write routes.

Every model forbids unknown fields and refuses string "true"/"false" for
booleans (StrictBool). Bodies are read manually (not via FastAPI's Body(...))
so we can enforce a route-appropriate byte limit before JSON parsing, and so
every rejection - bad JSON, oversized body, schema mismatch - comes back as
the same {"detail": ...} shape the rest of the API already uses.
"""

import json
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

DEFAULT_LIMIT = 32 * 1024       # typical control-plane JSON body
BULK_LIMIT = 8 * 1024 * 1024    # CSV import/export bodies


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _detail(exc: ValidationError) -> str:
    errs = exc.errors()
    first = errs[0]
    loc = ".".join(str(p) for p in first["loc"]) or "body"
    return f"{loc}: {first['msg']}"


def body_model(model_cls: type[StrictModel], max_bytes: int = DEFAULT_LIMIT):
    """FastAPI dependency: read the raw body, enforce a size cap, parse strictly."""

    async def dep(request: Request):
        raw = await request.body()
        if len(raw) > max_bytes:
            raise HTTPException(413, f"request body exceeds {max_bytes} bytes")
        if not raw:
            raw = b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a JSON object")
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            raise HTTPException(400, _detail(e)) from None

    return dep


# ------------------------------------------------------------ auth
class LoginRequest(StrictModel):
    username: str = ""
    password: str = ""


class OpenAIKeyRequest(StrictModel):
    api_key: str = ""


# ------------------------------------------------------------ campaigns
class CampaignCreateRequest(StrictModel):
    intake: dict = {}
    max_candidates: int = 20
    budget: int = 60
    name: str | None = None


class CampaignPatchRequest(StrictModel):
    name: str | None = None
    archived: StrictBool | None = None


class DuplicateRequest(StrictModel):
    name: str | None = None


class DeleteConfirmRequest(StrictModel):
    confirm: str | None = None


class SelectRequest(StrictModel):
    candidate_ids: list[str] = []
    action: str = "select"


class RankingRequest(StrictModel):
    weights: dict = {}


class CandidateRankingRequest(StrictModel):
    pinned: StrictBool | None = None
    manual_score_adjustment: float | None = None


class CorrectionRequest(StrictModel):
    field: str = ""
    value: Any = None


class CompareRequest(StrictModel):
    candidate_ids: list[str] = []


class ResearchRequest(StrictModel):
    candidate_ids: list[str] | None = None
    refresh: StrictBool = False


class GenerateRequest(StrictModel):
    candidate_ids: list[str] = []
    followup: StrictBool = False
    template_id: str | None = None
    template_version: int | None = None


class AssetsRequest(StrictModel):
    attachment_ids: list = []
    content_ids: list = []


class RunRuleRequest(StrictModel):
    dry_run: StrictBool = True


class DoNotContactRequest(StrictModel):
    do_not_contact: StrictBool = True
    reason: str | None = None


class EditDraftRequest(StrictModel):
    subject: str = ""
    body: str = ""


class GmailDraftsRequest(StrictModel):
    candidate_ids: list[str] = []


class OutcomeRequest(StrictModel):
    outcome: Any = None
    note: str = ""


class SequenceRequest(StrictModel):
    action: Any = None


class RecordOutcomeRequest(StrictModel):
    outcome: Any = None
    at: Any = None


class InteractionRequest(StrictModel):
    kind: Any = None
    detail: str = ""
    at: Any = None


class ReviewResolveRequest(StrictModel):
    contact_id: Any = None


class ImportRequest(StrictModel):
    csv: str = ""
    commit: StrictBool = False


# ------------------------------------------------------------ sending
class CampaignSendingRequest(StrictModel):
    enabled: StrictBool = False


class SendPreviewRequest(StrictModel):
    candidate_id: str = ""
    scheduled_at: Any = None


class SendConfirmRequest(StrictModel):
    candidate_id: str = ""
    approval_hash: str = ""
    scheduled_at: Any = None


class SendOutcomeRequest(StrictModel):
    outcome: Any = None


class SuppressRequest(StrictModel):
    email: Any = None
    reason: str = "do_not_contact"
