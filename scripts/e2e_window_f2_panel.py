#!/usr/bin/env python
"""Window F2 hybrid panel launcher (M4 fault-injection recovery legs).

The student panel with a REAL platform bridge and seeded-fake Notion
adapters, so organize/grade/activation/quota run against the REAL local
relay (real charges in the scratch DB) while every Notion read/write stays
on the in-memory fake adapters (this local-only window carries no [E2E]
workspace authorization -- no real Notion page is ever touched):

- directory/connection: the seeded fake workspace, with the generated
  exercise pre-seeded as answered/pending-grade (the M3 low-balance wiring
  shape), so the grade/organize triggers are reachable;
- platform: the REAL ``RealPlatformBridge`` -- activation, quota, quiz and
  grade all go through the real relay;
- organizer/grader: the real services over the fake page adapters.

Every page-adapter call is appended to ``--adapter-log`` (JSONL: ts,
adapter, method, args) -- that file is the "no partial write on failure"
and "exactly one result set on retry" evidence. Nothing secret is ever
written (the fake workspace is synthetic).

Run under the client repo's venv python with the SCRATCH client env in the
process environment and cwd = the scratch client-env directory (the panel
child writes activation output into that directory's .env, exactly like
``pku-sync panel`` started there):

  <client-venv>\\Scripts\\python.exe scripts\\e2e_window_f2_panel.py \
      --port 8791 --adapter-log <log.jsonl>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pku_sync.config import Settings
from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.exercise_grader import ExerciseGrader, FakeGradingPageAdapter
from pku_sync.panel.exercise_organizer import (
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
)
from pku_sync.panel.fake_directory import build_fake_directory_service
from pku_sync.panel.fake_panel import _FakeNotes, _FakePages
from pku_sync.panel.platform_bridge import RealPlatformBridge
from pku_sync.panel.webapi import create_app


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = value if not isinstance(value, str) else value
        return text
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    return repr(type(value).__name__)


class CallLoggingProxy:
    """Delegate every inner call and journal it to the adapter log.

    ``__getattr__`` returns a wrapping callable for methods, so calls made
    through feature-detected references (``getattr(adapter, "write_result")``)
    are journaled too. Missing attributes raise AttributeError as usual so
    ``getattr(..., None)`` feature detection keeps working.
    """

    def __init__(self, inner, log_path: str, label: str):
        self._inner = inner
        self._log_path = log_path
        self._label = label

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                record = {
                    "ts": _now_iso(),
                    "adapter": self._label,
                    "method": name,
                    "args": [_safe_value(item) for item in args],
                    "kwargs": {key: _safe_value(item) for key, item in kwargs.items()},
                }
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                return attr(*args, **kwargs)
            return wrapper
        return attr


def build_scratch_settings() -> Settings:
    """The scratch client env, built from the (authoritative) process env."""
    data_dir = os.environ.get("DATA_DIR", "").strip()
    relay = os.environ.get("CLOUD_TRANSCRIBE_URL", "").strip()
    if not data_dir or not relay:
        raise SystemExit("DATA_DIR and CLOUD_TRANSCRIBE_URL must be in the process env")
    return Settings(
        data_dir=Path(data_dir),
        cloud_transcribe_url=relay,
        platform_token=os.environ.get("PLATFORM_TOKEN", ""),
        transcription_backend=os.environ.get("TRANSCRIPTION_BACKEND", "local"),
        # This panel never touches the real workspace: the fake adapters are
        # the only Notion surface, and the token is blanked defensively.
        notion_token="",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="e2e_window_f2_panel.py")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--adapter-log", required=True)
    args = parser.parse_args()

    settings = build_scratch_settings()
    directory_service = build_fake_directory_service(variant="low-balance")
    connection_service = build_fake_connection_service(
        directory_service=directory_service
    )
    platform_service = RealPlatformBridge(settings)
    organizer_service = ExerciseOrganizer(
        directory_service=directory_service,
        relay=platform_service,
        page_adapter=CallLoggingProxy(_FakePages(), args.adapter_log, "organizer_pages"),
        notes_provider=_FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
    )
    grading_service = ExerciseGrader(
        directory_service=directory_service,
        relay=platform_service,
        page_adapter=CallLoggingProxy(
            FakeGradingPageAdapter(), args.adapter_log, "grading_pages"
        ),
    )

    from pku_sync.panel.ports import PanelPortError, bind_panel

    try:
        sock, bound_port = bind_panel(args.port)
    except PanelPortError as exc:
        print(f"panel bind failed: {exc}", file=sys.stderr)
        return 1
    print(f"面板已启动：http://127.0.0.1:{bound_port}/", flush=True)

    import uvicorn

    app = create_app(
        settings=settings,
        directory_service=directory_service,
        connection_service=connection_service,
        platform_service=platform_service,
        organizer_service=organizer_service,
        grading_service=grading_service,
    )
    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=bound_port, log_level="warning")
    ).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
