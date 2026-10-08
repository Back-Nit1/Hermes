"""Chrome-extension API, mounted at /extension/v1 (device bearer tokens, never the root app token).

The extension can only: read status/campaigns/review queue/notifications, preview and commit a
page capture (add one candidate to a campaign, after existing-contact / ambiguous-match / DNC
checks), pair, and revoke its own token. Approval, Gmail drafts, scheduling, sending, settings,
credentials, emergency controls and deletion are not routed here.

Edge rules (middleware, so they also cover errors and unknown paths): the Origin header must be
exactly one of HERMES_EXTENSION_ORIGINS (comma-separated chrome-extension://<32 a-p chars>); an empty
list refuses everything. CORS headers echo that one origin, never "*". OPTIONS preflight is answered
tokenless but only under this prefix and only for known paths. Bodies need a Content-Length of at
most MAX_BODY and JSON content type. Dashboard-side pairing lives under /api/extension/* so the
normal session/CSRF (remote) or app-token (local) guard protects it.
"""

import logging
import os
import re
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import analytics
from .extension_auth import SCOPES, ExtensionAuth, RateLimiter
from .ledger import canonical_url, norm_email
from .storage import ID_RE

log = logging.getLogger("extension")

PREFIX = "/extension/v1"
ORIGIN_RE = re.compile(r"chrome-extension://[a-p]{32}")
MAX_BODY = 32 * 1024
MAX_CANDIDATES = 500
ALLOWED_HEADERS = {"authorization", "content-type"}
ROUTES = {
    "/status": {"GET"},
    "/campaigns": {"GET"},
    "/capture/preview": {"POST"},
    "/capture/commit": {"POST"},
    "/review-queue": {"GET"},
    "/notifications": {"GET"},
    "/pair/exchange": {"POST"},
    "/device": {"DELETE"},
}
DEVICE_RATE = (120, 60)  # requests per device per minute
PAIR_RATE = (10, 300)  # exchange attempts per source per 5 minutes
FAIL_RATE = (30, 300)  # bad-token attempts per source per 5 minutes


def configured_origins(env=None):
    raw = (os.environ if env is None else env).get("HERMES_EXTENSION_ORIGINS", "")
    items = [o.strip() for o in raw.split(",") if o.strip()]
    for o in items:
        if not ORIGIN_RE.fullmatch(o):
            log.warning("ignoring HERMES_EXTENSION_ORIGINS entry that is not chrome-extension://<id>")
    return frozenset(o for o in items if ORIGIN_RE.fullmatch(o))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Exchange(Strict):
    code: str = Field(min_length=8, max_length=32)
    device_name: str = Field(min_length=1, max_length=64)


class Metadata(Strict):
    name: str = Field(default="", max_length=120)
    organization: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254)
    profile_url: str = Field(default="", max_length=2048)


class Capture(Strict):
    campaign_id: str = Field(pattern=ID_RE.pattern, max_length=40)
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=300)
    selection: str = Field(default="", max_length=2000)
    metadata: Metadata = Metadata()


def register(app, cfg, state, service_for, origins=None):
    """Mount the extension API and the dashboard pairing routes on `app`.

    service_for(user_id) -> awaitable CampaignService for that user (user 0 = local mode).
    """
    allowed = frozenset(origins) if origins is not None else configured_origins()
    limiter = RateLimiter()

    def auth():
        if "extension_auth" not in state:
            root = cfg.data_root if cfg.remote else state["svc"].store.root
            state["extension_auth"] = ExtensionAuth(root / "extension.sqlite3")
        return state["extension_auth"]

    def user_active(uid):
        if not cfg.remote:
            return uid == 0
        return bool(state["accounts"].q("SELECT 1 AS ok FROM users WHERE id=? AND disabled=0", (uid,)))

    def source(request):
        return request.client.host if request.client else "?"

    # ------------------------------------------------------------ dashboard side (root guard applies: /api/*)
    def dash_user(request):
        return request.state.user["user_id"] if cfg.remote else 0

    @app.post("/api/extension/pairing-codes")
    def create_pairing_code(request: Request, body: dict = Body(default={})):
        if set(body) - {"scopes"}:
            raise ValueError("only 'scopes' is accepted")
        scopes = body.get("scopes")
        if scopes is not None and not (isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)):
            raise ValueError("scopes must be a list of strings")
        code, expires = auth().create_code(dash_user(request), scopes)
        return {"code": code, "expires_at": expires, "scopes": sorted(scopes or SCOPES)}

    @app.get("/api/extension/devices")
    def list_devices(request: Request):
        return auth().list_devices(dash_user(request))

    @app.delete("/api/extension/devices/{device_id}")
    def revoke_device(device_id: int, request: Request):
        if not auth().revoke(device_id, dash_user(request)):
            raise KeyError("no such active device")
        return {"revoked": True}

    # ------------------------------------------------------------ extension side
    ext = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def reply(status, detail, **headers):
        return JSONResponse({"detail": detail}, status, headers=headers or None)

    @ext.middleware("http")
    async def edge(request: Request, call_next):
        origin = request.headers.get("origin", "")
        if origin not in allowed:
            return reply(403, "origin not allowed")
        route = request.url.path.removeprefix(PREFIX)
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if request.method == "OPTIONS":
            want = request.headers.get("access-control-request-method", "")
            asked = {h.strip().lower() for h in request.headers.get("access-control-request-headers", "").split(",")}
            if want not in ROUTES.get(route, ()) or not asked - {""} <= ALLOWED_HEADERS:
                return reply(403, "preflight refused")
            return Response(
                status_code=204,
                headers={
                    **cors,
                    "Access-Control-Allow-Methods": want,
                    "Access-Control-Allow-Headers": "authorization, content-type",
                    "Access-Control-Max-Age": "600",
                },
            )
        if request.method not in ROUTES.get(route, ()):
            return reply(404 if route not in ROUTES else 405, "not found", **cors)
        if request.method == "POST":
            if "application/json" not in request.headers.get("content-type", ""):
                return reply(415, "JSON body required", **cors)
            length = request.headers.get("content-length", "")
            if not length.isdigit():
                return reply(411, "Content-Length required", **cors)
            if int(length) > MAX_BODY:
                return reply(413, f"body over {MAX_BODY} bytes", **cors)
        elif int(request.headers.get("content-length") or 0) > 0:
            return reply(413, "this endpoint takes no body", **cors)
        response = await call_next(request)
        response.headers.update(cors)
        response.headers["Cache-Control"] = "no-store"
        return response

    for exc, code in ((KeyError, 404), (ValueError, 400)):
        ext.add_exception_handler(exc, lambda r, e, code=code: JSONResponse({"detail": str(e).strip("'")}, code))

    def device(scope=None):
        def dep(request: Request):
            src = source(request)
            head = request.headers.get("authorization", "")
            found = auth().authenticate(head[7:].strip()) if head[:7].lower() == "bearer " else None
            if not found or not user_active(found["user_id"]):
                if not limiter.allow(f"fail:{src}", *FAIL_RATE):
                    raise HTTPException(429, "too many failed attempts", {"Retry-After": str(FAIL_RATE[1])})
                raise HTTPException(401, "missing, expired, or revoked device token", {"WWW-Authenticate": "Bearer"})
            if scope and scope not in found["scopes"]:
                raise HTTPException(403, f"device lacks the '{scope}' scope")
            if not limiter.allow(f"dev:{found['id']}", *DEVICE_RATE):
                raise HTTPException(429, "rate limit exceeded", {"Retry-After": str(DEVICE_RATE[1])})
            request.state.device = found
            return found

        return dep

    @ext.post("/pair/exchange")
    def pair_exchange(request: Request, body: Exchange):
        if not limiter.allow(f"pair:{source(request)}", *PAIR_RATE):
            raise HTTPException(429, "too many pairing attempts", {"Retry-After": str(PAIR_RATE[1])})
        res = auth().exchange(body.code.upper(), body.device_name)
        if not res:
            raise HTTPException(401, "pairing code is invalid, expired, or already used")
        token, dev = res
        return {"token": token, "device": dev}

    @ext.delete("/device")
    def unpair(dev: dict = Depends(device())):
        auth().revoke(dev["id"], dev["user_id"])
        return {"revoked": True}

    @ext.get("/status")
    def status(dev: dict = Depends(device("status"))):
        keep = ("id", "name", "scopes", "expires_at")
        return {"ok": True, "api": "v1", "device": {k: dev[k] for k in keep}}

    @ext.get("/campaigns")
    async def campaigns(dev: dict = Depends(device("campaigns:read"))):
        rows = (await service_for(dev["user_id"])).list(limit=100)
        keep = ("campaign_id", "name", "subtype", "candidates")
        return [{k: r.get(k) for k in keep} for r in rows if not r.get("error")]

    @ext.get("/notifications")
    async def notifications(dev: dict = Depends(device("notifications:read"))):
        out = analytics.notifications((await service_for(dev["user_id"])).cache)
        keep = ("id", "kind", "message", "campaign_id", "candidate_id", "created_at", "read_at")
        return {"unread": out["unread"], "items": [{k: i.get(k) for k in keep} for i in out["items"][:50]]}

    @ext.get("/review-queue")
    async def review_queue(dev: dict = Depends(device("queue:read"))):
        svc = await service_for(dev["user_id"])
        contact_reviews = [
            {
                "id": r["id"],
                "reason": r["reason"],
                "campaign_id": r["campaign_id"],
                "candidate_id": r["candidate_id"],
                "person": {k: r["person"].get(k) for k in ("name", "organization", "role")},
                "possible_matches": [{k: c[k] for k in ("id", "name", "organization")} for c in r["option_contacts"]],
            }
            for r in svc.ledger.reviews()[:100]
        ]
        drafts = []
        for cid in svc.store.list_ids()[:50]:
            drafts += [
                {"campaign_id": cid, "candidate_id": d["candidate_id"]}
                for d in svc.cache.list_drafts(cid)
                if d["status"] == "needs_review"
            ]
        return {"contact_reviews": contact_reviews, "drafts_needing_review": drafts[:100]}

    # ------------------------------------------------------------ capture
    def assess(svc, body):
        """Read-only checks. Returns (decision, details, candidate_fields). Decision is one of
        ok | blocked_dnc | ambiguous | duplicate."""
        svc._require(body.campaign_id)
        if svc.ledger.campaign_meta(body.campaign_id)["archived"]:
            raise ValueError("campaign is archived")
        md = body.metadata
        parts = urlsplit(body.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must be http(s)")
        page = canonical_url(body.url)
        profile = canonical_url(md.profile_url) if md.profile_url else page
        if not profile:
            raise ValueError("profile_url must be http(s)")
        name = md.name or body.title
        if not name:
            raise ValueError("a name is required (metadata.name or title)")
        email = norm_email(md.email) if md.email else None
        if md.email and not email:
            raise ValueError("malformed email")
        fields = {
            "name": name[:120],
            "organization": md.organization,
            "role": md.role,
            "profile_url": profile,
            "discovery_source_url": page,
            "fit_hint": body.selection[:200],
        }
        led = svc.ledger
        m = led.match(name, md.organization, email, profile)
        ids = [m[1]] if m[0] == "match" else list(m[1]) if m[0] == "review" else []
        contacts = [led.contact(i) for i in ids]
        suppressed = bool(email and svc.cache.q("SELECT 1 FROM suppressions WHERE email=?", (email,)))
        dnc = suppressed or any(c["do_not_contact"] for c in contacts)
        details = {
            "existing_contact": (
                {k: contacts[0][k] for k in ("id", "name", "organization", "relationship")} if m[0] == "match" else None
            ),
            "ambiguous_matches": (
                {
                    "reason": m[2],
                    "contacts": [{k: c[k] for k in ("id", "name", "organization")} for c in contacts],
                }
                if m[0] == "review"
                else None
            ),
            "do_not_contact": dnc,
            "candidate": {k: fields[k] for k in ("name", "organization", "role", "profile_url")},
        }
        have = {
            c.get("profile_url") and canonical_url(c["profile_url"])
            for c in svc.store.candidates(body.campaign_id)["candidates"]
        }
        if dnc:
            return "blocked_dnc", details, fields
        if m[0] == "review":
            return "ambiguous", details, fields
        if profile in have:
            return "duplicate", details, fields
        return "ok", details, fields

    @ext.post("/capture/preview")
    async def preview(body: Capture, dev: dict = Depends(device("capture:preview"))):
        decision, details, _ = assess(await service_for(dev["user_id"]), body)
        return {"decision": decision, "can_commit": decision == "ok", **details}

    @ext.post("/capture/commit")
    async def commit(body: Capture, dev: dict = Depends(device("capture:commit"))):
        svc = await service_for(dev["user_id"])
        decision, details, fields = assess(svc, body)  # never trust an earlier preview
        if decision != "ok":
            return JSONResponse({"decision": decision, "can_commit": False, **details}, 409)
        if len(svc.store.candidates(body.campaign_id)["candidates"]) >= MAX_CANDIDATES:
            raise ValueError(f"campaign already has {MAX_CANDIDATES} candidates")
        cand = svc._add_candidates(body.campaign_id, [fields])[0]
        email = norm_email(body.metadata.email) if body.metadata.email else None
        svc.ledger.link_candidate(body.campaign_id, cand, email, source="extension")
        log.info("capture committed", extra={"user_id": dev["user_id"], "status": "ok"})
        return {"decision": "ok", "campaign_id": body.campaign_id, "candidate_id": cand["candidate_id"]}

    app.mount(PREFIX, ext)
    return ext
