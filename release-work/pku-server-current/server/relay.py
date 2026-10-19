"""The M2 cloud wrapper service: /v1/transcribe + /v1/quota + /v1/redeem.

Deployment: server-a/b/c behind an aeoluswu.info subdomain
(docs/SERVICE_PLAN.md §1.7). Classmates hold no keys -- the app
authenticates with the platform account session written at activation.
The upstream ASR vendor is a server-side internal decision injected as
``asr_fn``; until one is configured the endpoint refuses loudly (503),
never half-working. Audio is spooled and hashed once; an exact-fingerprint
hit reuses the stored transcript with no upstream call and no charge,
and a miss is transcribed upstream, metered, stored for exact-fingerprint
reuse, and returned. Concurrent identical misses are serialized by the
per-fingerprint in-flight lock (:mod:`server.in_flight`): one upstream
call and one charge per fingerprint, with waiters falling through to
their own attempt if the first one fails. Only transcript results
persist (the approved reuse store) -- never raw audio, account
identity, or any credential.

Recharging: /v1/redeem atomically turns a single-use code into its
configured transcription-second and optional LLM-millipoint grants
(manual collection flow, SERVICE_PLAN §1.3). /v1/recharge is
the reserved payment interface -- 501 until a provider is integrated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO

from fastapi import Body, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import State

from . import (
    account_profiles,
    accounts,
    admin,
    auth_pages,
    direct_upload,
    landing,
    llm_jobs,
    mail,
    ops_metrics,
    product_auth,
    transcript_store,
)
from .asr import AsrError
from .in_flight import FingerprintLocks
from .llm import ALLOWED_OPERATIONS, SELECTABLE_NOTE_MODELS, Completion, LlmError
from .rate_limit import SlidingWindowRateLimiter

# Request caps -- pinned defaults (library/relay-dedup-design.md
# "Security hardening"), env-overridable for ops exactly like the
# transcript reuse TTL.
LLM_BODY_MAX_BYTES = 250 * 1024  # 250 KB
TRANSCRIBE_MAX_BYTES = 500 * 1024 * 1024  # 500 MB
TRANSCRIBE_MAX_SECONDS = 4 * 3600  # 4 hours
LLM_BODY_MAX_BYTES_ENV = "LLM_BODY_MAX_BYTES"
TRANSCRIBE_MAX_BYTES_ENV = "TRANSCRIBE_MAX_BYTES"
TRANSCRIBE_MAX_SECONDS_ENV = "TRANSCRIBE_MAX_SECONDS"

# Per-route rate limits: activate/redeem per IP, the rest per account.
RATE_LIMIT_ACTIVATE_PER_IP = 10
RATE_LIMIT_REDEEM_PER_IP = 10
RATE_LIMIT_AUTH_PER_IP = 20
RATE_LIMIT_TRANSCRIBE_PER_ACCOUNT = 60
RATE_LIMIT_LLM_PER_ACCOUNT = 60
RATE_LIMIT_EXCHANGE_PER_ACCOUNT = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MESSAGE = "too many requests; slow down and retry later"

# The only callback URIs registered on the Notion integration; any other
# redirect_uri is rejected with 400 before Notion is ever contacted.
ALLOWED_REDIRECT_URIS = frozenset(
    {"http://localhost:8765/callback", "http://localhost:8766/callback"}
)


def _env_int(name: str, default: int) -> int:
    """Int from the environment; the pinned default wins on garbage/absence."""
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def llm_body_max_bytes() -> int:
    return _env_int(LLM_BODY_MAX_BYTES_ENV, LLM_BODY_MAX_BYTES)


def transcribe_max_bytes() -> int:
    return _env_int(TRANSCRIBE_MAX_BYTES_ENV, TRANSCRIBE_MAX_BYTES)


def transcribe_max_seconds() -> int:
    return _env_int(TRANSCRIBE_MAX_SECONDS_ENV, TRANSCRIBE_MAX_SECONDS)


# asr_fn(audio: bytes) -> {"segments": [...], "duration_seconds": int, ...}
AsrFn = Callable[[bytes], dict]
StreamingAsrFn = Callable[[BinaryIO, str], dict]
LlmFn = Callable[[str, str, str], Completion]
NotionExchangeFn = Callable[[str, str], dict]


class AccountProfileRequest(BaseModel):
    revision: int
    profile: dict
    replace_workspace_id: str | None = None


def _account_for(authorization: str | None, db: sqlite3.Connection) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing session token")
    account = accounts.validate_session(db, authorization[len("Bearer "):].strip())
    if account is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return account


def _require_verified(account: sqlite3.Row) -> None:
    try:
        product_auth.require_verified(account)
    except product_auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    return token or None


def _auth_payload(result: product_auth.AuthResult) -> dict:
    return {
        "platform_token": result.token,
        "email": result.email,
        "email_verified": result.email_verified,
        "invite_code": result.invite_code,
        "tier": result.tier,
    }


def _client_ip(request: Request) -> str:
    """Immediate peer of this request; the per-IP rate-limit key.

    The relay sits behind the reverse proxy in deployment, but the
    rate limits exist to slow abuse of the app itself, so the immediate
    client (proxy) address is the honest key -- X-Forwarded-For is not
    trusted here because any client can forge it.
    """
    return request.client.host if request.client is not None else "unknown"


def _require_rate(limiter: SlidingWindowRateLimiter, key: str) -> None:
    """Refuse with 429 once ``key`` crosses its window allowance."""
    if not limiter.allow(key):
        raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)


def _transcribe_response(
    payload: dict, *, reused: bool, seconds_charged: int
) -> dict:
    """Client payload minus the server-side metering field, plus the
    explicit reuse/settlement fields. ``duration_seconds`` stays
    server-side exactly as before; the client only requires ``segments``
    (pku_sync/transcription.py), so the added fields are additive."""
    response = {k: v for k, v in payload.items() if k != "duration_seconds"}
    response["reused"] = reused
    response["seconds_charged"] = seconds_charged
    return response


def _record_cache_metric(
    db: sqlite3.Connection, account_id: int, *, reused: bool, payload: dict
) -> None:
    # Usage remains authoritative if a metrics-only write fails.
    try:
        ops_metrics.record_cache_result(
            db, account_id, reused=reused,
            duration_seconds=math.ceil(payload["duration_seconds"]),
        )
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logging.getLogger("server.ops_metrics").warning(
            "cache metric write failed: %s", type(exc).__name__
        )


def _require_transcription_mode(db, account, mode):
    if mode not in ("basic", "advanced"):
        raise HTTPException(400, "invalid transcription mode")
    if mode == "advanced":
        quota = accounts.quota_breakdown(db, account["id"])
        if quota["tier"] == "lite":
            raise HTTPException(403, "高级转写需要 Pro 或 Max；Lite 支持普通转写。")
        if quota["advanced_seconds_remaining"] <= 0:
            raise HTTPException(402, "advanced transcription quota exhausted")


def _transcribe_miss(
    state: State,
    fingerprint: str,
    spooled: BinaryIO,
    filename: str,
    account_id: int,
    source_id: str = "",
    part_index: int = 0,
    transcribe_mode: str = "basic",
) -> dict:
    """The serialized miss path, run whole in one worker thread."""
    db = accounts.connect(state.db_path)
    try:
        with state.in_flight.hold("lecture:" + source_id + ":" + str(part_index) if source_id else fingerprint):
            row = transcript_store.lookup(db, fingerprint)
            if row is None and source_id:
                row = transcript_store.lookup_lecture_part(db, source_id, part_index)
            if row is not None:
                payload = json.loads(row["transcript_result"])
                if source_id and transcript_store.lookup_lecture_part(db, source_id, part_index) is None:
                    transcript_store.store_lecture_part(db, source_id, part_index, fingerprint, payload)
                _record_cache_metric(db, account_id, reused=True, payload=payload)
                return {
                    "payload": payload,
                    "reused": True,
                    "seconds_charged": 0,
                }
            if state.streaming_asr_fn is not None:
                from . import account_evidence
                with account_evidence.oss_context(state.db_path, account_id, 'asr:' + fingerprint, source_id or None):
                    payload = state.streaming_asr_fn(spooled, filename)
            else:
                payload = state.asr_fn(spooled.read())
            seconds = math.ceil(payload["duration_seconds"])
            if seconds > transcribe_max_seconds():
                raise HTTPException(
                    status_code=413, detail="audio exceeds the 4-hour duration cap"
                )
            accounts.record_transcription(
                db,
                account_id,
                seconds,
                operation_id='asr:' + fingerprint,
                mode=transcribe_mode,
            )
            transcript_store.store_transcript(db, fingerprint, payload)
            if source_id:
                transcript_store.store_lecture_part(
                    db, source_id, part_index, fingerprint, payload
                )
            _record_cache_metric(db, account_id, reused=False, payload=payload)
            return {"payload": payload, "reused": False, "seconds_charged": seconds}
    finally:
        db.close()


async def _clean_direct_uploads(app: FastAPI) -> None:
    while True:
        vendor = app.state.streaming_asr_fn
        if vendor is not None and hasattr(vendor, "store"):
            try:
                await run_in_threadpool(direct_upload.cleanup_database, app.state.db_path, vendor.store)
            except Exception:
                logging.getLogger(__name__).exception("direct upload cleanup failed")
        await asyncio.sleep(300)


async def _clean_llm_jobs(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await run_in_threadpool(llm_jobs.cleanup_expired_db, app.state.db_path)
        except Exception:
            logging.getLogger(__name__).exception("AI job cleanup failed")


@asynccontextmanager
async def _direct_upload_lifespan(app: FastAPI):
    db = accounts.connect(app.state.db_path)
    try:
        llm_jobs.recover_interrupted(db)
        llm_jobs.cleanup_expired(db)
    finally:
        db.close()
    vendor = app.state.streaming_asr_fn
    task = asyncio.create_task(_clean_direct_uploads(app)) if vendor is not None and hasattr(vendor, "store") else None
    jobs_cleanup_task = asyncio.create_task(_clean_llm_jobs(app))
    try:
        yield
    finally:
        app.state.llm_job_executor.shutdown(wait=False, cancel_futures=False)
        jobs_cleanup_task.cancel()
        try:
            await jobs_cleanup_task
        except asyncio.CancelledError:
            pass
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_relay_app(
    db_path: Path | str,
    asr_fn: AsrFn | None = None,
    streaming_asr_fn: StreamingAsrFn | None = None,
    llm_fn: LlmFn | None = None,
    notion_exchange_fn: NotionExchangeFn | None = None,
    mail_config: mail.MailConfig | None = None,
    mail_sender: mail.MailSender | None = None,
    admin_token: str | None = None,
    llm_plus_fn: LlmFn | None = None,
    llm_max_fn: LlmFn | None = None,
    llm_ab_test_account_id: int | None = None,
) -> FastAPI:
    """Build the cloud app; ``asr_fn`` is the upstream ASR vendor call."""
    app = FastAPI(
        title="PKU All in Notion cloud relay", docs_url=None, redoc_url=None,
        lifespan=_direct_upload_lifespan,
    )
    app.state.db_path = Path(db_path)
    app.state.asr_fn = asr_fn
    app.state.streaming_asr_fn = streaming_asr_fn
    app.state.llm_fn = llm_fn
    app.state.llm_plus_fn = llm_plus_fn
    app.state.llm_max_fn = llm_max_fn
    app.state.llm_ab_test_account_id = llm_ab_test_account_id
    app.state.notion_exchange_fn = notion_exchange_fn
    app.state.mail_config = mail_config
    app.state.mail_sender = mail_sender
    app.state.llm_job_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-job")


    # Per-fingerprint in-flight registry: serializes genuinely overlapping
    # identical misses inside this single process (uvicorn single worker;
    # see server/in_flight.py for the documented multi-worker boundary).
    app.state.in_flight = FingerprintLocks()
    # Per-route abuse controls: one in-memory sliding window per route, at
    # the pinned limits (library/relay-dedup-design.md "Security hardening").
    app.state.limiter_activate = SlidingWindowRateLimiter(
        RATE_LIMIT_ACTIVATE_PER_IP, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_redeem = SlidingWindowRateLimiter(
        RATE_LIMIT_REDEEM_PER_IP, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_auth = SlidingWindowRateLimiter(
        RATE_LIMIT_AUTH_PER_IP, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_transcribe = SlidingWindowRateLimiter(
        RATE_LIMIT_TRANSCRIBE_PER_ACCOUNT, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_llm = SlidingWindowRateLimiter(
        RATE_LIMIT_LLM_PER_ACCOUNT, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_llm_status = SlidingWindowRateLimiter(
        120, RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.limiter_exchange = SlidingWindowRateLimiter(
        RATE_LIMIT_EXCHANGE_PER_ACCOUNT, RATE_LIMIT_WINDOW_SECONDS
    )

    @app.middleware("http")
    async def limit_request_size(request: Request, call_next):
        """Reject oversized payloads before they are parsed or buffered.

        Content-Length is an early, cheap upper bound; the authoritative
        checks stay in the routes (the parsed /v1/llm body and the
        spooled /v1/transcribe stream), which also cover absent or
        untrustworthy length headers.
        """
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            size = int(declared)
            path = request.url.path
            if path in ("/v1/llm", "/v1/llm/jobs") and size > llm_body_max_bytes():
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
            if path == "/v1/transcribe" and size > transcribe_max_bytes():
                return JSONResponse(
                    status_code=413, content={"detail": "audio file too large"}
                )
        return await call_next(request)

    @app.post("/v1/transcribe/direct/init")
    def direct_transcribe_init(
        size_bytes: int = Body(..., embed=True),
        source_id: str | None = Body(default=None),
        part_index: int = Body(default=0, strict=True, ge=0, le=999999999),
        transcribe_mode: str = Body(default="basic"),
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_transcribe, str(account["id"]))
            _require_verified(account)
            if accounts.remaining_seconds(db, account["id"]) <= 0:
                raise HTTPException(status_code=402, detail="transcription quota exhausted")
            _require_transcription_mode(db, account, transcribe_mode)
            vendor = app.state.streaming_asr_fn
            if vendor is None or not hasattr(vendor, "transcribe_uploaded") or not hasattr(vendor, "store"):
                raise HTTPException(status_code=503, detail="direct upload unavailable")
            if size_bytes <= 0 or size_bytes > transcribe_max_bytes():
                raise HTTPException(status_code=413, detail="audio size is invalid or too large")
            from . import account_evidence
            account_evidence.validate_context({"source_id": source_id} if source_id is not None else {})
            try:
                return direct_upload.issue(
                    db,
                    account["id"],
                    vendor.store,
                    size_bytes,
                    source_id,
                    part_index=part_index,
                    transcribe_mode=transcribe_mode,
                )
            except Exception as exc:
                logging.getLogger(__name__).exception("direct upload authorization failed")
                raise HTTPException(status_code=502, detail="OSS authorization unavailable") from exc
        finally:
            db.close()

    @app.post("/v1/transcribe/direct/complete")
    async def direct_transcribe_complete(
        upload_id: str = Body(..., embed=True),
        authorization: str | None = Header(default=None),
    ) -> dict:
        if len(upload_id) != 32 or any(c not in "0123456789abcdef" for c in upload_id):
            raise HTTPException(status_code=400, detail="invalid upload ID")
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_transcribe, str(account["id"]))
            _require_verified(account)
            vendor = app.state.streaming_asr_fn
            if vendor is None or not hasattr(vendor, "transcribe_uploaded") or not hasattr(vendor, "store"):
                raise HTTPException(status_code=503, detail="direct upload unavailable")
            account_id = account["id"]
        finally:
            db.close()
        try:
            outcome = await run_in_threadpool(
                direct_upload.complete, app.state, account_id, upload_id,
                transcribe_max_bytes(), transcribe_max_seconds(),
            )
        except direct_upload.DirectUploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).exception("direct transcription failed")
            raise HTTPException(status_code=502, detail="direct transcription failed") from exc
        return _transcribe_response(
            outcome["payload"], reused=outcome["reused"],
            seconds_charged=outcome["seconds_charged"],
        )

    @app.post("/v1/transcribe")
    async def transcribe(
        audio: UploadFile,
        request: Request = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            # Per-account rate limit, after auth so the 401 matrix keeps its
            # precedence and unknown callers never touch an account bucket.
            _require_rate(app.state.limiter_transcribe, str(account["id"]))
            _require_verified(account)
            if accounts.remaining_seconds(db, account["id"]) <= 0:
                raise HTTPException(
                    status_code=402,
                    detail="transcription quota exhausted; redeem a pack"
                    " or switch to local",
                )
            if app.state.asr_fn is None and app.state.streaming_asr_fn is None:
                raise HTTPException(
                    status_code=503, detail="ASR vendor not configured yet"
                )
            source_id = (
                (request.headers.get("x-pku-source-id") or "").strip()
                if request is not None
                else ""
            )
            part_raw = (
                (request.headers.get("x-pku-part-index") or "0").strip()
                if request is not None
                else "0"
            )
            if not part_raw.isascii() or not part_raw.isdigit() or len(part_raw) > 9:
                raise HTTPException(400, "invalid transcription part index")
            part_index = int(part_raw)
            mode_raw = (
                (request.headers.get("x-pku-transcribe-mode") or "basic").strip().lower()
                if request is not None
                else "basic"
            )
            transcribe_mode = mode_raw
            _require_transcription_mode(db, account, transcribe_mode)
            from . import account_evidence
            account_evidence.validate_context({"source_id": source_id} if source_id else {})
            try:
                spooled, fingerprint = await run_in_threadpool(
                    transcript_store.spool_and_hash,
                    audio.file,
                    transcribe_max_bytes(),
                )
            except transcript_store.TranscriptTooLarge:
                raise HTTPException(
                    status_code=413, detail="audio file too large"
                ) from None
            try:
                row = transcript_store.lookup(db, fingerprint)
                if row is None and source_id:
                    row = transcript_store.lookup_lecture_part(db, source_id, part_index)
                if row is not None:
                    payload = json.loads(row["transcript_result"])
                    if source_id and transcript_store.lookup_lecture_part(db, source_id, part_index) is None:
                        transcript_store.store_lecture_part(db, source_id, part_index, fingerprint, payload)
                    _record_cache_metric(db, account["id"], reused=True, payload=payload)
                    return _transcribe_response(
                        payload,
                        reused=True,
                        seconds_charged=0,
                    )
                try:
                    outcome = await run_in_threadpool(
                        _transcribe_miss,
                        app.state,
                        fingerprint,
                        spooled,
                        audio.filename or "audio",
                        account["id"],
                        source_id,
                        part_index,
                        transcribe_mode,
                    )
                except AsrError as exc:
                    raise HTTPException(
                        status_code=502, detail="transcription failed upstream"
                    ) from exc
                return _transcribe_response(
                    outcome["payload"],
                    reused=outcome["reused"],
                    seconds_charged=outcome["seconds_charged"],
                )
            finally:
                spooled.close()
        finally:
            db.close()

    @app.get("/v1/quota")
    def quota(authorization: str | None = Header(default=None)) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            view = product_auth.account_public_view(account)
            breakdown = accounts.quota_breakdown(db, account["id"])
            return {
                "label": account["label"],
                "email": view["email"],
                "email_verified": view["email_verified"],
                "legacy_activate": view["legacy_activate"],
                **breakdown,
            }
        finally:
            db.close()

    @app.post("/v1/quota/exchange")
    def quota_exchange(
        hours: int = Body(default=1, strict=True, ge=1, le=100),
        target: str = Body(default="advanced"),
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_exchange, str(account["id"]))
            _require_verified(account)
            try:
                return accounts.exchange_points_for_seconds(
                    db, int(account["id"]), hours, target=target
                )
            except accounts.InsufficientPoints as exc:
                raise HTTPException(status_code=402, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            db.close()

    @app.post("/v1/llm")
    async def llm(
        request: Request,
        operation: str = Body(...),
        system: str = Body(...),
        user: str = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_llm, str(account["id"]))
            _require_verified(account)
            if len(await request.body()) > llm_body_max_bytes():
                raise HTTPException(status_code=413, detail="request body too large")
            if operation not in ALLOWED_OPERATIONS:
                raise HTTPException(status_code=400, detail="unsupported AI operation")
            bundled = accounts.is_bundled_llm_operation(db, account["id"], operation)
            if not bundled and accounts.remaining_llm_millipoints(db, account["id"]) <= 0:
                raise HTTPException(status_code=402, detail="AI points exhausted")
            if app.state.llm_fn is None:
                raise HTTPException(status_code=503, detail="LLM vendor not configured yet")
            try:
                completion = await run_in_threadpool(
                    app.state.llm_fn, operation, system, user
                )
            except LlmError as exc:
                raise HTTPException(
                    status_code=502, detail="AI operation failed upstream"
                ) from exc
            charged_milli = 0 if bundled else completion.millipoints
            if not accounts.record_llm(
                db, account["id"], operation, completion.model, completion.input_tokens,
                completion.output_tokens, charged_milli
            ):
                raise HTTPException(status_code=402, detail="AI points exhausted")
            return {"content": completion.content, "points_charged": charged_milli / 1000}
        finally:
            db.close()

    @app.post("/v1/llm/jobs", status_code=202)
    async def start_llm_job(
        request: Request,
        operation: str = Body(...),
        system: str = Body(...),
        user: str = Body(...),
        idempotency_key: str = Body(...),
        operation_context: dict | None = Body(default=None),
        model: str | None = Body(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_llm, str(account["id"]))
            _require_verified(account)
            if len(await request.body()) > llm_body_max_bytes():
                raise HTTPException(status_code=413, detail="request body too large")
            if operation not in ALLOWED_OPERATIONS:
                raise HTTPException(status_code=400, detail="unsupported AI operation")
            if model is not None and operation != "notes":
                raise HTTPException(status_code=400, detail="model selection is only available for notes")
            selected_model = ("qwen3.7-plus" if operation == "notes" else "qwen3.5-flash") if model is None else ("qwen3.8-max-0902" if model == "qwen3.8-max" else model)
            if selected_model not in SELECTABLE_NOTE_MODELS:
                raise HTTPException(status_code=400, detail="unsupported AI model")
            if len(system) + len(user) > 200_000:
                raise HTTPException(status_code=413, detail="request is too large")
            if len(idempotency_key) != 32 or any(c not in "0123456789abcdef" for c in idempotency_key):
                raise HTTPException(status_code=400, detail="invalid idempotency key")
            llm_jobs.cleanup_expired(db)
            digest = llm_jobs.request_hash(operation, system, user, selected_model)
            from . import account_evidence
            context = account_evidence.validate_context(operation_context)
            existing = llm_jobs.get_by_key(db, account["id"], idempotency_key)
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise HTTPException(status_code=409, detail="idempotency key reused with different request")
                if json.loads(existing["context_json"] or "{}") != context:
                    raise HTTPException(409, detail="AI operation context changed")
                return {"job_id": existing["job_id"], "status": existing["status"]}
            bundled = accounts.is_bundled_llm_operation(db, account["id"], operation)
            if not bundled and accounts.remaining_llm_millipoints(db, account["id"]) <= 0:
                raise HTTPException(status_code=402, detail="AI points exhausted")
            selected_fn = (app.state.llm_max_fn if selected_model == "qwen3.8-max-0902"
                           else app.state.llm_plus_fn if selected_model == "qwen3.7-plus"
                           else app.state.llm_fn)
            if selected_fn is None:
                raise HTTPException(status_code=503, detail="LLM vendor not configured yet")
            row, inserted = llm_jobs.create_or_get(
                db, account["id"], idempotency_key, digest, operation,
                selected_model, operation_context=context,
            )
            if row["request_hash"] != digest:
                raise HTTPException(status_code=409, detail="idempotency key reused with different request")
            previous_context = json.loads(row['context_json'] or '{}')
            if previous_context != context:
                raise HTTPException(409, detail='AI operation context changed')
            if inserted:
                try:
                    llm_jobs.submit_job(
                        app.state.llm_job_executor, app.state.db_path, row["job_id"],
                        account["id"], operation, system, user, selected_fn,
                    )
                except RuntimeError as exc:
                    llm_jobs.fail_queued(db, row["job_id"])
                    raise HTTPException(status_code=503, detail="AI jobs unavailable") from exc
            return {"job_id": row["job_id"], "status": row["status"]}
        finally:
            db.close()

    @app.get("/v1/llm/jobs/{job_id}")
    def get_llm_job(job_id: str, authorization: str | None = Header(default=None)) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_llm_status, str(account["id"]))
            _require_verified(account)
            if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                raise HTTPException(status_code=404, detail="AI job not found")
            row = llm_jobs.get(db, account["id"], job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="AI job not found")
            return llm_jobs.public_result(row)
        finally:
            db.close()

    @app.post("/v1/redeem")
    def redeem(
        code: str = Body(..., embed=True),
        request: Request = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        """Atomically apply both configured grants (§1.3 extra usage)."""
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_redeem, _client_ip(request))
            _require_verified(account)
            if not accounts.redeem_code(db, code, account["id"]):
                raise HTTPException(
                    status_code=400, detail="invalid or already-used code"
                )
            normalized = accounts.extract_token_from_input(code)
            if normalized.startswith(("PKU-", "GIFT-")):
                return {**accounts.quota_breakdown(db, account["id"]),
                        "kind": "invite" if normalized.startswith("PKU-") else "gift",
                        "invite_bound": normalized.startswith("PKU-")}
            return {
                "transcribe_seconds_remaining": accounts.remaining_seconds(
                    db, account["id"]
                ),
            }
        finally:
            db.close()

    @app.post("/v1/activate")
    def activate(
        code: str = Body(..., embed=True), request: Request = None
    ) -> dict:
        """Create an account from a one-time code, without exposing vendor keys."""
        _require_rate(app.state.limiter_activate, _client_ip(request))
        db = accounts.connect(app.state.db_path)
        try:
            activated = accounts.activate_code(db, code)
            if activated is None:
                raise HTTPException(status_code=400, detail="invalid or already-used code")
            token, seconds = activated
            return {
                "platform_token": token,
                "transcribe_seconds_remaining": seconds,
            }
        finally:
            db.close()

    @app.post("/v1/notion/exchange")
    def notion_exchange(
        code: str = Body(...),
        redirect_uri: str = Body(...),
        request: Request = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_exchange, str(account["id"]))
            if redirect_uri not in ALLOWED_REDIRECT_URIS:
                raise HTTPException(
                    status_code=400, detail="redirect_uri is not allowlisted"
                )
            if app.state.notion_exchange_fn is None:
                raise HTTPException(status_code=503, detail="Notion OAuth is not configured")
            try:
                return app.state.notion_exchange_fn(code, redirect_uri)
            except Exception as exc:
                raise HTTPException(status_code=502, detail="Notion authorization failed") from exc
        finally:
            db.close()

    @app.post("/v1/auth/register")
    def auth_register(
        email: str = Body(...),
        password: str = Body(...),
        invite_code: str | None = Body(default=None),
        request: Request = None,
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            try:
                result = product_auth.register_account(
                    db,
                    email=email,
                    password=password,
                    mail_config=app.state.mail_config,
                    mail_sender=app.state.mail_sender,
                    invite_code=invite_code,
                )
            except product_auth.AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            return _auth_payload(result)
        finally:
            db.close()

    @app.post("/v1/auth/login")
    def auth_login(
        email: str = Body(...),
        password: str = Body(...),
        request: Request = None,
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            try:
                result = product_auth.login_account(db, email=email, password=password)
            except product_auth.AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            return _auth_payload(result)
        finally:
            db.close()

    @app.post("/v1/auth/logout")
    def auth_logout(authorization: str | None = Header(default=None)) -> dict:
        token = _bearer_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="missing session token")
        db = accounts.connect(app.state.db_path)
        try:
            product_auth.logout_session(db, token)
            return {"ok": True}
        finally:
            db.close()

    @app.get("/v1/auth/me")
    def auth_me(authorization: str | None = Header(default=None)) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            view = product_auth.account_public_view(account)
            breakdown = accounts.quota_breakdown(db, account["id"])
            view.update(breakdown)
            return view
        finally:
            db.close()

    @app.post("/v1/auth/bind-campus")
    def auth_bind_campus(
        student_id: str = Body(..., embed=True),
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_rate(app.state.limiter_exchange, str(account["id"]))
            try:
                outcome = accounts.bind_campus_account(
                    db, int(account["id"]), student_id
                )
            except accounts.CampusBindingConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            breakdown = accounts.quota_breakdown(db, int(account["id"]))
            return {
                **breakdown,
                **outcome,
            }
        finally:
            db.close()

    from . import catalog_routes
    catalog_routes.register(app, accounts.connect, _account_for, _require_verified)

    @app.get("/v1/profile")
    def account_profile(authorization: str | None = Header(default=None)) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_verified(account)
            return account_profiles.get(db, int(account["id"]))
        finally:
            db.close()

    @app.put("/v1/profile")
    def update_account_profile(
        body: AccountProfileRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            account = _account_for(authorization, db)
            _require_verified(account)
            try:
                return account_profiles.put(
                    db, int(account["id"]), body.revision, body.profile, replace_workspace_id=body.replace_workspace_id
                )
            except account_profiles.ProfileConflict as exc:
                raise HTTPException(status_code=409, detail=exc.current) from exc
            except account_profiles.ProfileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            db.close()

    @app.post("/v1/auth/resend-verification")
    def auth_resend_verification(
        email: str = Body(..., embed=True),
        request: Request = None,
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            try:
                product_auth.resend_verification(
                    db,
                    email=email,
                    mail_config=app.state.mail_config,
                    mail_sender=app.state.mail_sender,
                )
            except product_auth.AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            return {"ok": True}
        finally:
            db.close()

    @app.post("/v1/auth/forgot")
    def auth_forgot(
        email: str = Body(..., embed=True),
        request: Request = None,
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            try:
                product_auth.request_password_reset(
                    db,
                    email=email,
                    mail_config=app.state.mail_config,
                    mail_sender=app.state.mail_sender,
                )
            except product_auth.AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            return {"ok": True}
        finally:
            db.close()

    @app.post("/v1/auth/reset")
    def auth_reset(
        token: str = Body(...),
        password: str = Body(...),
        request: Request = None,
    ) -> dict:
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            if app.state.mail_config is None:
                raise HTTPException(status_code=503, detail="邮件服务未配置")
            try:
                email = product_auth.reset_password_with_token(
                    db,
                    app.state.mail_config,
                    token=token,
                    password=password,
                )
            except product_auth.AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            return {"ok": True, "email": email}
        finally:
            db.close()

    @app.get("/invite/{code}", response_class=HTMLResponse)
    def invite_landing(code: str) -> HTMLResponse:
        normalized = accounts.extract_token_from_input(code)
        db = accounts.connect(app.state.db_path)
        try:
            inviter = db.execute(
                "SELECT email FROM accounts WHERE invite_code = ?", (normalized,)
            ).fetchone()
            if inviter is None:
                return HTMLResponse(
                    auth_pages.verify_result_page(ok=False, message="邀请链接无效或不存在。"),
                    status_code=404,
                )
            return HTMLResponse(
                auth_pages.invite_landing_page(
                    invite_code=normalized, inviter_label=str(inviter["email"] or ""), mode="register"
                )
            )
        finally:
            db.close()

    @app.post("/invite/{code}/claim", response_class=HTMLResponse)
    def invite_claim_submit(
        code: str,
        email: str = Form(default=""),
        password: str = Form(default=""),
        mode: str = Form(default="login"),
        password2: str = Form(default=""),
        request: Request = None,
    ) -> HTMLResponse:
        normalized = accounts.extract_token_from_input(code)
        db = accounts.connect(app.state.db_path)
        auth_res = None
        try:
            inviter = db.execute(
                "SELECT id, email FROM accounts WHERE invite_code = ?", (normalized,)
            ).fetchone()
            if inviter is None:
                return HTMLResponse(
                    auth_pages.verify_result_page(ok=False, message="邀请链接无效或不存在。"),
                    status_code=404,
                )

            def render(*, error: str = "", ok_message: str = "", status: int = 200) -> HTMLResponse:
                return HTMLResponse(
                    auth_pages.invite_landing_page(
                        invite_code=normalized, inviter_label=str(inviter["email"] or ""),
                        mode=mode, email=email, error=error, ok_message=ok_message,
                    ), status_code=status, headers={"Cache-Control": "no-store"},
                )

            try:
                _require_rate(app.state.limiter_auth, _client_ip(request))
            except HTTPException as exc:
                return render(error="操作过于频繁，请稍后重试。", status=exc.status_code)
            if mode not in {"login", "register"}:
                return render(error="请选择登录或注册。", status=400)
            if mode == "register" and password2 and password != password2:
                return render(error="两次输入的密码不一致，请重新输入。", status=400)
            try:
                if mode == "register":
                    auth_res = product_auth.register_account(
                        db, email=email, password=password,
                        mail_config=app.state.mail_config, mail_sender=app.state.mail_sender,
                        invite_code=normalized,
                    )
                else:
                    auth_res = product_auth.login_account(db, email=email, password=password)
                    if not accounts.apply_invite_code(db, auth_res.account_id, normalized):
                        existing = db.execute(
                            "SELECT invited_by FROM accounts WHERE id = ?", (auth_res.account_id,)
                        ).fetchone()
                        if existing["invited_by"] != inviter["id"]:
                            return render(error="无法绑定：不能邀请自己，且每个账号只能绑定一位邀请人。", status=400)
            except product_auth.AuthError as exc:
                return render(error=exc.message, status=exc.status_code)
            account = db.execute(
                "SELECT invite_rewarded_at FROM accounts WHERE id = ?", (auth_res.account_id,)
            ).fetchone()
            message = "注册成功，邀请已绑定！请查收邮箱验证邮件。" if mode == "register" else "邀请已成功绑定！"
            if account["invite_rewarded_at"] is not None:
                message += "你与邀请人的 1 个月 Lite 普通转写奖励已到账，每周 25 小时自动滚存。"
            else:
                message += "在客户端连接新的北大教学网学号后，你与邀请人将自动各得 1 个月 Lite 普通转写额度，每周 25 小时自动滚存。"
            return render(ok_message=message)
        finally:
            # This form binds an invite; it does not establish a browser session.
            if auth_res is not None:
                product_auth.logout_session(db, auth_res.token)
            db.close()

    def _gift_summary(gift: sqlite3.Row) -> str:
        parts: list[str] = []
        if gift["tier"]:
            days = int(gift["duration_days"] or 30)
            parts.append(f"{str(gift['tier']).upper()} 会员 {days} 天")
        if int(gift["weekly_rollover_seconds"] or 0) > 0:
            hrs = int(gift["weekly_rollover_seconds"]) // 3600
            parts.append(f"每周 {hrs} 小时转写自动滚存")
        if int(gift["advanced_seconds"] or 0) > 0:
            parts.append(f"高级转写 {int(gift['advanced_seconds']) // 60} 分钟")
        if int(gift["basic_seconds"] or 0) > 0:
            parts.append(f"普通转写 {int(gift['basic_seconds']) // 60} 分钟")
        if int(gift["llm_millipoints"] or 0) > 0:
            pts = int(gift["llm_millipoints"]) / 1000
            parts.append(f"{pts:g} AI 点")
        return " · ".join(parts) if parts else "学习额度礼包"

    @app.get("/gift/{token}", response_class=HTMLResponse)
    def gift_landing(token: str) -> HTMLResponse:
        normalized = accounts.extract_token_from_input(token)
        db = accounts.connect(app.state.db_path)
        try:
            gift = accounts.get_gift_link(db, normalized)
            if gift is None:
                return HTMLResponse(
                    auth_pages.verify_result_page(ok=False, message="礼包链接无效或已失效。"),
                    status_code=404,
                )
            return HTMLResponse(
                auth_pages.gift_landing_page(
                    token=normalized, gift_summary=_gift_summary(gift)
                )
            )
        finally:
            db.close()

    @app.post("/gift/{token}", response_class=HTMLResponse)
    def gift_claim_submit(
        token: str,
        email: str = Form(...),
        password: str = Form(...),
        request: Request = None,
    ) -> HTMLResponse:
        normalized = accounts.extract_token_from_input(token)
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_redeem, _client_ip(request))
            gift = accounts.get_gift_link(db, normalized)
            if gift is None:
                return HTMLResponse(
                    auth_pages.verify_result_page(ok=False, message="礼包链接无效或已失效。"),
                    status_code=404,
                )
            summary = _gift_summary(gift)
            try:
                auth_res = product_auth.login_account(db, email=email, password=password)
            except product_auth.AuthError as exc:
                return HTMLResponse(
                    auth_pages.gift_landing_page(
                        token=normalized, gift_summary=summary, error=exc.message
                    ),
                    status_code=exc.status_code,
                )
            account = db.execute(
                "SELECT * FROM accounts WHERE id = ?", (auth_res.account_id,)
            ).fetchone()
            if account is None or not product_auth.email_is_verified(account):
                return HTMLResponse(
                    auth_pages.gift_landing_page(
                        token=normalized,
                        gift_summary=summary,
                        error="请先完成邮箱验证后再领取礼包。",
                    ),
                    status_code=403,
                )
            if not accounts.claim_gift_link(db, normalized, auth_res.account_id):
                return HTMLResponse(
                    auth_pages.gift_landing_page(
                        token=normalized,
                        gift_summary=summary,
                        error="该礼包已被领完或你已领取过。",
                    ),
                    status_code=400,
                )
            return HTMLResponse(
                auth_pages.gift_landing_page(
                    token=normalized,
                    gift_summary=summary,
                    ok_message=f"已成功绑定至 {auth_res.email}！请回到客户端查看最新额度。",
                )
            )
        finally:
            db.close()

    @app.get("/verify", response_class=HTMLResponse)
    def verify_landing(token: str = "") -> HTMLResponse:
        db = accounts.connect(app.state.db_path)
        try:
            if not token or app.state.mail_config is None:
                body = auth_pages.verify_result_page(
                    ok=False, message="验证链接无效或已过期。"
                )
                return HTMLResponse(body, status_code=400)
            try:
                email = product_auth.verify_email_with_token(
                    db, app.state.mail_config, token
                )
            except (product_auth.AuthError, mail.MailError):
                body = auth_pages.verify_result_page(
                    ok=False, message="验证链接无效或已过期。"
                )
                return HTMLResponse(body, status_code=400)
            body = auth_pages.verify_result_page(
                ok=True,
                message=f"{email} 已验证。可以回到客户端兑换额度或开始学习。",
            )
            return HTMLResponse(body)
        finally:
            db.close()

    @app.get("/reset", response_class=HTMLResponse)
    def reset_landing(token: str = "") -> HTMLResponse:
        if not token:
            return HTMLResponse(
                auth_pages.reset_result_page(ok=False, message="重置链接无效或已过期。"),
                status_code=400,
            )
        return HTMLResponse(auth_pages.reset_form_page(token=token))

    @app.post("/reset", response_class=HTMLResponse)
    def reset_submit(
        token: str = Form(...),
        password: str = Form(...),
        password2: str = Form(""),
        request: Request = None,
    ) -> HTMLResponse:
        if password != password2:
            return HTMLResponse(
                auth_pages.reset_form_page(token=token, error="两次输入的密码不一致。"),
                status_code=400,
            )
        db = accounts.connect(app.state.db_path)
        try:
            _require_rate(app.state.limiter_auth, _client_ip(request))
            if app.state.mail_config is None:
                return HTMLResponse(
                    auth_pages.reset_result_page(ok=False, message="邮件服务未配置。"),
                    status_code=503,
                )
            try:
                product_auth.reset_password_with_token(
                    db,
                    app.state.mail_config,
                    token=token,
                    password=password,
                )
            except product_auth.AuthError as exc:
                return HTMLResponse(
                    auth_pages.reset_form_page(token=token, error=exc.message),
                    status_code=exc.status_code,
                )
            return HTMLResponse(
                auth_pages.reset_result_page(
                    ok=True, message="密码已更新，请回到客户端重新登录。"
                )
            )
        finally:
            db.close()

    @app.post("/v1/recharge")
    def recharge() -> dict:
        """Reserved payment interface; refuses loudly until M4 wires it up.

        Intended contract once a provider is integrated: pay → provider
        webhook → credit this account (or auto-issue a code redeemed via
        /v1/redeem). Until then the only live path is manual collection +
        redemption codes.
        """
        raise HTTPException(
            status_code=501,
            detail="payment not integrated yet; use a redemption code via"
            " /v1/redeem (manual collection)",
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    admin.add_admin_routes(app, app.state.db_path, admin_token=admin_token)
    landing.add_landing_routes(app)
    from . import account_evidence
    account_evidence.register_routes(app, _account_for, _require_verified)
    return app
