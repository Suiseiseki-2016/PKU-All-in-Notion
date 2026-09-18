"""Panel Notion-connection routes (VAL-META-020).

Three loopback endpoints behind the onboarding connection row and the shell's
connection block. Every response is the connection snapshot — state, the
approved label, and an optional generic error — and never the token itself.
A concurrent reconnect is refused with HTTP 409 and the pinned busy copy.
"""

from __future__ import annotations

from fastapi import HTTPException

from .connection import ConnectionBusy, ConnectionService


def add_connection_routes(app, service: ConnectionService) -> None:
    @app.get("/api/connection")
    def connection_state() -> dict:
        return service.snapshot()

    @app.post("/api/connection/disconnect")
    def connection_disconnect() -> dict:
        return service.disconnect()

    @app.post("/api/connection/connect")
    def connection_connect() -> dict:
        try:
            return service.connect()
        except ConnectionBusy as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc


__all__ = ["add_connection_routes"]
