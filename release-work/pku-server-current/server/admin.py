"""Private operator API for aggregate usage, code issuance, and account status."""
from __future__ import annotations

import hmac
import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import accounts, ops_metrics

TOKEN_ENV = "PKU_ADMIN_TOKEN"
MIN_TOKEN_LENGTH = 32
NO_STORE = "no-store"


def _issue_parameters(payload: dict) -> tuple[int, int, int]:
    minutes = payload.get("transcribe_minutes", 0)
    count = payload.get("count", 1)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 7200:
        raise HTTPException(status_code=422, detail="transcribe_minutes must be 0..7200")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise HTTPException(status_code=422, detail="count must be 1..20")
    try:
        points = Decimal(str(payload.get("llm_points", 0)))
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="llm_points must be decimal") from exc
    milli = points * 1000
    if not points.is_finite() or points < 0 or points > 10000 or milli != milli.to_integral_value():
        raise HTTPException(status_code=422, detail="llm_points must be 0..10000 with at most 3 decimals")
    if minutes == 0 and milli == 0:
        raise HTTPException(status_code=422, detail="at least one grant must be positive")
    return minutes * 60, int(milli), count


def add_admin_routes(
    app: FastAPI,
    db_path: Path | str,
    *,
    admin_token: str | None = None,
    connect: Callable[[Path | str], sqlite3.Connection] | None = None,
) -> None:
    token = os.environ.get(TOKEN_ENV, "") if admin_token is None else admin_token
    if not token:
        return
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"{TOKEN_ENV} must have at least {MIN_TOKEN_LENGTH} characters")
    connect_db = connect or accounts.connect

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        prefix = "Bearer "
        candidate = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
        if not candidate or not hmac.compare_digest(candidate, token):
            raise HTTPException(status_code=401, detail="admin authorization required")

    @app.middleware("http")
    async def admin_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = NO_STORE
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> HTMLResponse:
        from .admin_ui import ADMIN_HTML

        return HTMLResponse(
            ADMIN_HTML,
            headers={
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            },
        )

    @app.get("/admin/app.js")
    def admin_script() -> PlainTextResponse:
        script = Path(__file__).with_name("admin.js").read_text(encoding="utf-8")
        return PlainTextResponse(script, media_type="text/javascript")

    @app.get("/admin/api/overview", dependencies=[Depends(require_admin)])
    def admin_overview() -> dict:
        conn = connect_db(db_path)
        try:
            return ops_metrics.overview(conn)
        finally:
            conn.close()

    @app.get("/admin/api/accounts", dependencies=[Depends(require_admin)])
    def admin_accounts() -> dict:
        conn = connect_db(db_path)
        try:
            rows = conn.execute(
                "SELECT id, label, email, status, email_verified_at, tier, "
                "transcribe_seconds_remaining, basic_seconds_remaining, "
                "advanced_seconds_remaining, weekly_rollover_seconds, "
                "llm_millipoints_remaining, invite_code, campus_id_hash, created_at "
                "FROM accounts ORDER BY id DESC LIMIT 200"
            ).fetchall()
            return {"accounts": [
                {
                    "id": row["id"],
                    "label": row["label"],
                    "email": row["email"],
                    "status": row["status"],
                    "tier": row["tier"],
                    "email_verified": row["email_verified_at"] is not None if row["email"] else None,
                    "transcribe_seconds_remaining": row["transcribe_seconds_remaining"],
                    "basic_seconds_remaining": row["basic_seconds_remaining"],
                    "advanced_seconds_remaining": row["advanced_seconds_remaining"],
                    "weekly_rollover_seconds": row["weekly_rollover_seconds"],
                    "llm_points_remaining": row["llm_millipoints_remaining"] / 1000,
                    "invite_code": row["invite_code"],
                    "campus_bound": bool(row["campus_id_hash"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]}
        finally:
            conn.close()

    @app.post("/admin/api/codes", dependencies=[Depends(require_admin)])
    def admin_issue_codes(payload: dict = Body(...)) -> dict:
        seconds, millipoints, count = _issue_parameters(payload)
        conn = connect_db(db_path)
        try:
            codes = [
                accounts.create_code(conn, seconds, llm_millipoints=millipoints)
                for _ in range(count)
            ]
            return {
                "codes": codes,
                "transcribe_seconds": seconds,
                "llm_millipoints": millipoints,
            }
        finally:
            conn.close()

    @app.post("/admin/api/gifts", dependencies=[Depends(require_admin)])
    def admin_issue_gifts(payload: dict = Body(...)) -> dict:
        tier = payload.get("tier")
        if tier is not None and tier not in accounts.VALID_TIERS:
            raise HTTPException(status_code=422, detail="tier must be lite, pro, or max")
        def integer(name, default, minimum=0, maximum=1_000_000):
            value = payload.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise HTTPException(422, f"invalid {name}")
            return value
        duration_days = integer("duration_days", 30, maximum=3650)
        basic_minutes = integer("basic_minutes", 0)
        advanced_minutes = integer("advanced_minutes", 0)
        weekly_rollover_hours = integer("weekly_rollover_hours", 25 if tier else 0)
        max_claims = integer("max_claims", 1, minimum=1)
        count = integer("count", 1, minimum=1, maximum=20)
        try:
            points = Decimal(str(payload.get("llm_points", 0)))
        except InvalidOperation as exc:
            raise HTTPException(422, "invalid llm_points") from exc
        if not points.is_finite() or not 0 <= points <= 10000 or points * 1000 != (points * 1000).to_integral_value():
            raise HTTPException(422, "invalid llm_points")
        llm_millipoints = int(points * 1000)
        note = str(payload.get("note", ""))
        conn = connect_db(db_path)
        try:
            tokens = [
                accounts.create_gift_link(
                    conn,
                    tier=tier,
                    duration_days=duration_days,
                    basic_seconds=basic_minutes * 60,
                    advanced_seconds=advanced_minutes * 60,
                    weekly_rollover_seconds=weekly_rollover_hours * 3600,
                    llm_millipoints=llm_millipoints,
                    max_claims=max_claims,
                    note=note,
                )
                for _ in range(count)
            ]
            return {
                "tokens": tokens,
                "links": [f"/gift/{tok}" for tok in tokens],
                "tier": tier,
                "duration_days": duration_days,
            }
        finally:
            conn.close()

    @app.patch("/admin/api/accounts/{account_id}/status", dependencies=[Depends(require_admin)])
    def admin_set_account_status(account_id: int, payload: dict = Body(...)) -> dict:
        status = payload.get("status")
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=422, detail="status must be active or disabled")
        conn = connect_db(db_path)
        try:
            with conn:
                updated = conn.execute(
                    "UPDATE accounts SET status = ? WHERE id = ?", (status, account_id)
                )
                if updated.rowcount != 1:
                    raise HTTPException(status_code=404, detail="account not found")
                if status == "disabled":
                    conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
            return {"id": account_id, "status": status}
        finally:
            conn.close()
