"""Relay dev launcher: the approved local start path (VAL-CROSS-021).

    uv run --directory <repo> python -m server.dev_launcher [--port 8800]

Checks the approved relay dev ports 8800 → 8801 → 8802, binds 127.0.0.1 on
the first available, never kills or contacts a port's occupant, surfaces the
chosen port in the startup line below, and fails with the pinned actionable
copy (:data:`server.dev_ports.RELAY_DEV_PORTS_BUSY_MESSAGE`) when all three
are occupied — never an arbitrary port, never 0.0.0.0.

``RELAY_DB_PATH`` stays the authoritative accounts-DB setting and MUST point
at a disposable scratch SQLite file for local runs (never the deployed
``/data/accounts.db``). When it is unset, the launcher defaults it to a
per-port scratch file under the system temp dir (the services.yaml dev
naming, ``pku-relay-dev-<port>.db``) so a bare dev launch can never touch a
deployment path.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Loopback only — the dev launcher never binds 0.0.0.0/::.
RELAY_DEV_HOST = "127.0.0.1"


def force_utf8_stdio() -> None:
    """Pin stdout/stderr to UTF-8 so the startup line and the pinned failure
    copy survive every capture path as clean UTF-8 (a redirected stdout would
    otherwise follow the console codepage and mojibake the actionable copy;
    same reconfigure pattern as the client's scheduled runs). A real console
    keeps its native wide-char writer, so interactive display is unchanged.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def scratch_db_path(port: int) -> Path:
    """A per-port disposable scratch DB (the services.yaml dev naming)."""
    return Path(tempfile.gettempdir()) / f"pku-relay-dev-{port}.db"


def resolve_db_path_env(port: int) -> str:
    """RELAY_DB_PATH when set, else the per-port scratch default (never /data)."""
    return os.environ.get("RELAY_DB_PATH") or str(scratch_db_path(port))


def startup_line(port: int) -> str:
    """The surfaced-port evidence: the chosen port in the startup output."""
    return f"中转服务已启动：http://{RELAY_DEV_HOST}:{port}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.dev_launcher",
        description="启动本地中转服务（8800→8801→8802 首个空闲，只绑 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8800,
        help="优先端口（仅支持 8800/8801/8802；默认 8800）",
    )
    force_utf8_stdio()

    args = parser.parse_args(argv)

    import uvicorn

    from .dev_ports import RelayDevPortError, bind_relay_dev
    from .main import app as build_app

    try:
        sock, bound_port = bind_relay_dev(args.port)
    except RelayDevPortError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # A bare dev launch never falls back to the deployed /data/accounts.db.
    db_path = resolve_db_path_env(bound_port)
    os.environ["RELAY_DB_PATH"] = db_path

    # Build the app (the same uvicorn --factory server.main:app path) before
    # the startup line so a broken environment fails honestly, never as a
    # service that "already started".
    app = build_app()

    # The chosen port is surfaced in startup output; uvicorn's own banner is
    # suppressed at warning level (and it logs no banner in the handed-socket
    # path), so this line is the user-facing proof (VAL-CROSS-021).
    print(startup_line(bound_port))
    print(f"数据库：{db_path}")

    # Serve on the pre-bound socket (the port we probed is the port uvicorn
    # binds — never a check-then-rebind race, never an arbitrary port).
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=RELAY_DEV_HOST,
            port=bound_port,
            log_level="warning",
        )
    )
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
