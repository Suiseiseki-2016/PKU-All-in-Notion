#!/usr/bin/env python
"""Window F1 runner (VAL-CROSS-002 / VAL-CROSS-004 / VAL-CROSS-006).

Drives the local-only Window F part-1 legs through product surfaces with
ZERO relay spend (no /v1/transcribe, no /v1/llm, no OAuth exchange):

- Scratch activation: one code created in a fresh scratch relay DB; the panel
  child runs against a scratch env whose .env lives in the scratch root, so
  activation writes PLATFORM_TOKEN + TRANSCRIPTION_BACKEND=cloud ONLY there.
- Real PKU reads: the scratch data root is seeded from the user's real tree
  (transcripts/notes/recording markers + materials; no videos, no keyframes),
  so the panel-triggered daily performs a REAL Teaching Network sync while
  download/process stay honest no-ops (nothing to download, nothing to
  transcribe) and the LLM summary backend is deliberately unreachable in the
  scratch env (the summary failure is logged and non-fatal by design).
- Panel-triggered daily/automate: job-state + EXIT_CODE agreement evidence,
  review/lecture reports, and read-only Notion snapshots proving the second
  full run registers nothing new.

Secrets discipline: the activation-code value and the platform token are
read from files/env only and never printed or written to evidence.

Run via the client repo's uv environment:
  uv run --directory <repo> python scripts/e2e_window_f1.py <command> ...

The scratch layout (created by the operator before panel-start):
  <scratch>/client-env/.env   the scratch .env (activation target)
  <scratch>/client-data/      the scratch DATA_DIR (seeded real tree)
  <scratch>/code.txt          the activation code value (file only)
  <scratch>/state.json        runner state (panel pid/port)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")
ACTIVATION_KEYS = ("CLOUD_TRANSCRIBE_URL", "PLATFORM_TOKEN", "TRANSCRIPTION_BACKEND")
# The scratch summary backend: unroutable loopback so the summarize step
# fails fast with zero external traffic (a failing LLM backend must never
# turn a successful sync into a failed daily run -- service.run_daily).
UNROUTABLE_LLM_BASE = "http://127.0.0.1:1/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_keys(path: Path) -> list[str]:
    """Key NAMES only (never values) of a .env-style file."""
    if not path.exists():
        return []
    keys = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def _scratch_paths(scratch: str) -> dict:
    root = Path(scratch)
    return {
        "root": root,
        "client_env": root / "client-env",
        "scratch_env": root / "client-env" / ".env",
        "data_dir": root / "client-data",
        "state": root / "state.json",
        "code": root / "code.txt",
    }


def _load_state(scratch: str) -> dict:
    path = _scratch_paths(scratch)["state"]
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(scratch: str, state: dict) -> None:
    _write_json(_scratch_paths(scratch)["state"], state)


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _child_env(scratch: Path, relay: str, allowlist: str = "") -> dict[str, str]:
    """The scratch client env (env vars beat every .env file, verified host fact).

    NOTION_TOKEN and the PKU credentials are NOT copied here: the panel child
    loads them from the real package-root .env (reads only, never printed).
    PLATFORM_TOKEN starts empty so the panel is un-activated until the real
    activation writes the token into the scratch .env.
    """
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(scratch) / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{relay.rstrip('/')}/v1/transcribe",
            "PLATFORM_TOKEN": "",
            "TRANSCRIPTION_BACKEND": "local",
            "OPENAI_API_KEY": "",
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": UNROUTABLE_LLM_BASE,
            "PYTHONUTF8": "1",
        }
    )
    if allowlist:
        env["COURSE_ALLOWLIST"] = allowlist
    return env


# -- panel child -------------------------------------------------------------------


def _wait_for_panel(stdout_log: Path, timeout: float = 60.0) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stdout_log.exists():
            text = stdout_log.read_text(encoding="utf-8", errors="replace")
            match = PANEL_STARTUP_PORT_RE.search(text)
            if match:
                return int(match.group(1)), text.strip().splitlines()[-1]
        time.sleep(0.2)
    raise SystemExit("panel startup line did not appear in time")


def cmd_panel_start(args) -> int:
    import httpx

    paths = _scratch_paths(args.scratch)
    paths["client_env"].mkdir(parents=True, exist_ok=True)
    if not paths["scratch_env"].exists():
        # The pristine scratch .env: activation upserts into exactly this file
        # (env_file_path prefers the CWD .env -- the panel child runs here).
        paths["scratch_env"].write_text(
            "# Window F1 scratch client env -- deleted at window cleanup.\n"
            "# Activation writes CLOUD_TRANSCRIBE_URL / PLATFORM_TOKEN /\n"
            "# TRANSCRIPTION_BACKEND=cloud into THIS file only.\n",
            encoding="utf-8",
        )
    env = _child_env(paths["root"], args.relay, args.allowlist)
    stdout_log = Path(args.stdout_log)
    stderr_log = Path(args.stderr_log)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    out_handle = open(stdout_log, "wb")
    err_handle = open(stderr_log, "wb")
    proc = subprocess.Popen(
        [str(_pku_sync_exe()), "panel", "--no-browser", "--port", str(args.port)],
        cwd=str(paths["client_env"]),
        env=env,
        stdout=out_handle,
        stderr=err_handle,
    )
    out_handle.close()
    err_handle.close()
    bound_port, startup_line = _wait_for_panel(stdout_log)
    health = httpx.get(f"http://127.0.0.1:{bound_port}/healthz", timeout=15)
    health.raise_for_status()
    state = _load_state(args.scratch)
    state["panel"] = {
        "pid": proc.pid,
        "port": bound_port,
        "started_at": _now_iso(),
        "startup_line": startup_line,
        "relay": args.relay.rstrip("/"),
        "allowlist": args.allowlist or "",
    }
    _save_state(args.scratch, state)
    print(f"panel child pid={proc.pid} port={bound_port} healthz=ok")
    print(f"startup line: {startup_line}")
    return 0


def cmd_panel_stop(args) -> int:
    state = _load_state(args.scratch)
    panel = state.get("panel") or {}
    pid = panel.get("pid")
    port = panel.get("port")
    if pid:
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            print(f"taskkill pid={pid} exit={proc.returncode}")
        except OSError as exc:
            print(f"taskkill failed for pid={pid}: {exc}")
    if port:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            connections = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"],
                capture_output=True, text=True,
            ).stdout.strip()
            if connections == "0":
                break
            time.sleep(0.5)
        final = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"port {port} listeners after stop: {final}")
    state["panel"] = None
    _save_state(args.scratch, state)
    return 0


# -- real PKU probe sync -------------------------------------------------------------


def cmd_probe_sync(args) -> int:
    paths = _scratch_paths(args.scratch)
    env = _child_env(paths["root"], args.relay, args.allowlist)
    env.pop("PLATFORM_TOKEN", None)  # the CLI probe is un-activated by design
    log_path = Path(args.out)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(_pku_sync_exe()), "sync"],
        cwd=str(paths["client_env"]),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    log_path.write_text(
        f"command: pku-sync sync (scratch env, real Teaching Network read)\n"
        f"exit_code: {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )
    print(f"sync exit={proc.returncode} log={log_path}")
    if proc.returncode != 0:
        print((proc.stdout or "")[-1500:])
        print((proc.stderr or "")[-1500:])
    return proc.returncode


def cmd_scan_danger(args) -> int:
    """Recordings the panel-triggered download/process steps would act on.

    Uses the pipeline's own collect_jobs + recording_stage (the single source
    of truth for what a run will see), so the gate mirrors the real code path:

    - download_cmd selects jobs whose transcript does NOT exist yet;
    - process_cmd only acts on jobs whose video.mp4 exists locally.

    DANGEROUS = selected by download AND not unavailable: the daily would
    re-fetch the media URL from the real host and could then transcribe it
    (relay spend). Must be empty before panel-triggered daily/automate runs.
    """
    from pku_sync.pipeline import RecordingJob, collect_jobs, recording_stage

    paths = _scratch_paths(args.scratch)
    jobs = collect_jobs(paths["data_dir"])
    rows: list[dict] = []
    stages: dict[str, int] = {}
    would_download: list[dict] = []
    dangerous: list[dict] = []
    videos_exist: list[str] = []
    for job in jobs:
        stage, unavailable = recording_stage(job)
        stages[stage] = stages.get(stage, 0) + 1
        row = {
            "course": job.course_name,
            "slug": job.recording.slug,
            "stage": stage,
            "unavailable": bool(unavailable),
            "media_url": bool(
                job.recording.media_url
                or (job.directory / "recording.json").exists()
            ),
        }
        rows.append(row)
        if not (job.directory / "transcript.json").exists():
            would_download.append(row)
            if stage != "unavailable":
                dangerous.append(row)
        if job.video.exists():
            videos_exist.append(job.course_name + "/" + job.recording.slug)
    summary = {
        "scanned_at": _now_iso(),
        "recordings": len(rows),
        "stages": stages,
        "would_download": would_download,
        "dangerous": dangerous,
        "dangerous_count": len(dangerous),
        "videos_exist": videos_exist,
    }
    _write_json(Path(args.out), summary)
    print(
        f"recordings={len(rows)} stages={stages} "
        f"would_download={len(would_download)} "
        f"videos_exist={len(videos_exist)} "
        f"dangerous={len(dangerous)} "
        "(dangerous = download-selected AND resolvable → real media fetch + transcribe)"
    )
    for row in dangerous:
        print(f"  DANGEROUS: {row['course']}/{row['slug']}")
    return 1 if dangerous else 0


# -- env evidence -------------------------------------------------------------------


def cmd_env_snap(args) -> int:
    paths = _scratch_paths(args.scratch)
    real_env = REPO_ROOT / ".env"
    payload = {
        "tag": args.tag,
        "captured_at": _now_iso(),
        "scratch_env": {
            "path": str(paths["scratch_env"]),
            "exists": paths["scratch_env"].exists(),
            "sha256": _sha256(paths["scratch_env"]) if paths["scratch_env"].exists() else None,
            "keys": _env_keys(paths["scratch_env"]),
        },
        "real_env": {
            "path": str(real_env),
            "exists": real_env.exists(),
            "sha256": _sha256(real_env) if real_env.exists() else None,
            "keys": _env_keys(real_env),
        },
    }
    _write_json(Path(args.out), payload)
    print(
        f"scratch .env sha256={payload['scratch_env']['sha256']} "
        f"keys={payload['scratch_env']['keys']}"
    )
    print(f"real .env sha256={payload['real_env']['sha256']}")
    return 0


def cmd_token_scope(args) -> int:
    """Prove the platform token value exists ONLY in the scratch .env.

    The token VALUE is read but never printed; each checked location gets a
    boolean `token_value_present`.
    """
    paths = _scratch_paths(args.scratch)
    token = _env_value(paths["scratch_env"], "PLATFORM_TOKEN")
    if not token:
        raise SystemExit("scratch .env has no PLATFORM_TOKEN (activate first)")

    def _contains(value: str) -> bool:
        return value and value.find(token) >= 0

    locations: dict[str, bool | str] = {}
    real_env = REPO_ROOT / ".env"
    locations["real_repo_env"] = (
        _contains(real_env.read_text(encoding="utf-8", errors="replace"))
        if real_env.exists()
        else False
    )
    hits: list[str] = []
    for path in paths["root"].rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(paths["root"]))
        if rel == r"client-env\.env" or rel == r"code.txt":
            continue
        try:
            if _contains(path.read_text(encoding="utf-8", errors="replace")):
                hits.append(rel)
        except OSError:
            continue
    locations["other_scratch_files_with_token"] = hits
    payload = {
        "checked_at": _now_iso(),
        "token_in_scratch_env": True,
        "token_in_real_repo_env": locations["real_repo_env"],
        "other_scratch_files_with_token": hits,
        "only_in_scratch_env": locations["real_repo_env"] is False and not hits,
    }
    _write_json(Path(args.out), payload)
    print(f"token only in scratch .env: {payload['only_in_scratch_env']}")
    return 0 if payload["only_in_scratch_env"] else 1


# -- relay scratch DB ---------------------------------------------------------------


def cmd_relay_db(args) -> int:
    """Redemption status of the scratch relay DB (the code VALUE never printed)."""
    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"scratch relay DB missing: {db}")
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        codes = [dict(row) for row in conn.execute("SELECT * FROM codes")]
        accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        usage = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        llm_usage = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
        dedup = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='transcript_reuse'"
        ).fetchone()[0]
    finally:
        conn.close()
    code_rows = [
        {
            "seconds": row.get("seconds"),
            "redeemed": bool(row.get("redeemed_by")),
        }
        for row in codes
    ]
    payload = {
        "checked_at": _now_iso(),
        "db": str(db),
        "codes_total": len(codes),
        "code_rows": code_rows,
        "accounts": accounts,
        "sessions": sessions,
        "usage_rows": usage,
        "llm_usage_rows": llm_usage,
        "transcript_reuse_table": bool(dedup),
    }
    _write_json(Path(args.out), payload)
    print(
        f"codes={len(codes)} redeemed={sum(1 for r in code_rows if r['redeemed'])} "
        f"accounts={accounts} sessions={sessions} usage={usage} llm_usage={llm_usage}"
    )
    return 0


# -- panel API probes ----------------------------------------------------------------


def _panel_url(args) -> str:
    if args.url:
        return args.url.rstrip("/")
    state = _load_state(args.scratch)
    port = (state.get("panel") or {}).get("port")
    if not port:
        raise SystemExit("no panel port in state (pass --url or run panel-start)")
    return f"http://127.0.0.1:{port}"


def cmd_status_snap(args) -> int:
    import httpx

    response = httpx.get(f"{_panel_url(args)}/api/status", timeout=30)
    response.raise_for_status()
    payload = response.json()
    payload["_captured_at"] = _now_iso()
    _write_json(Path(args.out), payload)
    job = payload.get("running") or payload.get("last")
    daily = payload.get("daily") or {}
    print(
        f"status: running={bool(payload.get('running'))} "
        f"last={ (job or {}).get('kind') }/{ (job or {}).get('state') }/exit={ (job or {}).get('exit_code') } "
        f"daily_exit={daily.get('exit_code')} recordings={len(payload.get('recordings') or [])}"
    )
    return 0


def cmd_directory_snap(args) -> int:
    import httpx

    response = httpx.get(f"{_panel_url(args)}/api/directory", timeout=120)
    response.raise_for_status()
    payload = response.json()
    payload["_captured_at"] = _now_iso()
    _write_json(Path(args.out), payload)
    stats = payload.get("stats") or {}
    sync = payload.get("sync") or {}
    print(
        f"directory: state={sync.get('state')} courses={stats.get('courses')} "
        f"lectures={stats.get('lectures')} materials={stats.get('materials')} "
        f"exercises={len(payload.get('exercises') or [])}"
    )
    return 0


def cmd_launch_check(args) -> int:
    import httpx

    response = httpx.post(
        f"{_panel_url(args)}/api/launch",
        json={"target_id": args.target_id, "target_type": "page",
              "course_id": args.course_id or None},
        timeout=30,
    )
    body = response.json()
    payload = {
        "checked_at": _now_iso(),
        "http_status": response.status_code,
        "response": body,
        "expected_stored_url": args.expected_url,
        "url_matches_stored_identity": (
            response.status_code == 200
            and body.get("status") == "opened"
            and body.get("url") == args.expected_url
        ),
    }
    _write_json(Path(args.out), payload)
    print(
        f"launch http={response.status_code} status={body.get('status')} "
        f"url_matches_stored_identity={payload['url_matches_stored_identity']}"
    )
    return 0 if payload["url_matches_stored_identity"] else 1


# -- read-only Notion snapshots (automate idempotency) --------------------------------


def _notion_client():
    from pku_sync.notion import get_client

    return get_client()


def _block_text(block: dict) -> str:
    value = block.get(block.get("type") or "") or {}
    for key in ("rich_text", "text", "plain_text"):
        pieces = value.get(key)
        if isinstance(pieces, list):
            return "".join(
                piece.get("plain_text", "") for piece in pieces if isinstance(piece, dict)
            )
        if isinstance(pieces, str):
            return pieces
    return ""


def _find_database(client, title: str) -> dict:
    for row in client.search(title, object_type="database"):
        if row.get("object") != "database":
            continue
        names = "".join(
            piece.get("plain_text", "")
            for piece in (row.get("title") or [])
        )
        if title in names:
            schema = client.get_database(row["id"])
            title_props = [
                name
                for name, prop in (schema.get("properties") or {}).items()
                if isinstance(prop, dict) and prop.get("type") == "title"
            ]
            return {"id": row["id"], "url": row.get("url"), "name": names,
                    "title_property": title_props[0] if title_props else ""}
    raise SystemExit(f"database not found by title: {title}")


def _database_row_ids(client, database: dict) -> list[dict]:
    rows = client.query_database(
        database["id"],
        filter={"property": database["title_property"], "title": {"is_not_empty": True}},
        page_size=100,
    )
    identities = []
    for row in rows:
        title = ""
        for prop in (row.get("properties") or {}).values():
            if isinstance(prop, dict):
                for piece in prop.get("title") or []:
                    title += piece.get("plain_text", "")
        identities.append({"id": row.get("id"), "title": title})
    return identities


def cmd_notion_snap(args) -> int:
    client = _notion_client()
    directory = json.loads(Path(args.directory_file).read_text(encoding="utf-8"))
    courses = {
        course["title"]: [
            {"id": block.get("id"), "title": block.get("title") or ""}
            for block in client.list_child_pages(course["id"])
        ]
        for course in directory.get("courses") or []
    }
    tasks_db = _find_database(client, "学习任务")
    wrong_db = _find_database(client, "知识点与错题")
    learning_center = None
    for row in client.search("学习中心", object_type="page"):
        if row.get("object") == "page" and "学习中心" in (row.get("title") or ""):
            learning_center = row
            break
    morning_summary = None
    if learning_center:
        blocks = client.list_children(learning_center["id"])
        today = datetime.now().strftime("%Y-%m-%d")
        status_lines = [
            _block_text(block)
            for block in blocks
            if "管道状态" in _block_text(block)
        ]
        morning_summary = {
            "page_id": learning_center["id"],
            "block_count": len(blocks),
            "pipeline_status_lines": status_lines[-3:],
            "today_mentioned": any(today in line for line in status_lines),
        }
    payload = {
        "tag": args.tag,
        "captured_at": _now_iso(),
        "courses": courses,
        "tasks_database": {"id": tasks_db["id"], "row_count": None},
        "task_rows": _database_row_ids(client, tasks_db),
        "wrong_answer_rows": _database_row_ids(client, wrong_db),
        "learning_center": morning_summary,
    }
    payload["tasks_database"]["row_count"] = len(payload["task_rows"])
    _write_json(Path(args.out), payload)
    print(
        f"notion snap[{args.tag}]: courses={len(courses)} "
        f"lecture_pages={sum(len(v) for v in courses.values())} "
        f"task_rows={len(payload['task_rows'])} "
        f"wrong_rows={len(payload['wrong_answer_rows'])}"
    )
    return 0


def cmd_notion_diff(args) -> int:
    pre = json.loads(Path(args.pre).read_text(encoding="utf-8"))
    post = json.loads(Path(args.post).read_text(encoding="utf-8"))

    def _ids(rows):
        return {row["id"] for row in rows}

    new_task_rows = [
        row for row in post["task_rows"] if row["id"] not in _ids(pre["task_rows"])
    ]
    new_wrong_rows = [
        row for row in post["wrong_answer_rows"] if row["id"] not in _ids(pre["wrong_answer_rows"])
    ]
    course_new_pages = {}
    for title, pages in (post.get("courses") or {}).items():
        before = {page["id"] for page in (pre.get("courses") or {}).get(title, [])}
        added = [page for page in pages if page["id"] not in before]
        if added:
            course_new_pages[title] = added
    pre_status = (pre.get("learning_center") or {}).get("pipeline_status_lines") or []
    post_status = (post.get("learning_center") or {}).get("pipeline_status_lines") or []
    payload = {
        "diffed_at": _now_iso(),
        "pre_tag": pre.get("tag"),
        "post_tag": post.get("tag"),
        "new_task_rows": new_task_rows,
        "new_wrong_answer_rows": new_wrong_rows,
        "new_course_child_pages": course_new_pages,
        "pipeline_status_lines_pre": pre_status,
        "pipeline_status_lines_post": post_status,
        "zero_new_writes": not new_task_rows and not new_wrong_rows and not course_new_pages,
    }
    _write_json(Path(args.out), payload)
    print(
        f"diff: new_task_rows={len(new_task_rows)} "
        f"new_wrong_rows={len(new_wrong_rows)} new_pages={len(course_new_pages)} "
        f"zero_new_writes={payload['zero_new_writes']}"
    )
    return 0


# -- report capture -----------------------------------------------------------------


def cmd_fixture_automate(args) -> int:
    """Panel-entry automate idempotency + failure-continue fixture.

    Drives the REAL panel entry (panel.pipelines.run_pipeline bound through
    the JobRunner the panel always uses) with the deterministic fake seam on
    daily/download/process/summarize/review/lecture -- the same seam the
    mission's e2e_automate_workflow.py patches. Zero external spend: no agent
    host, no Notion write. The fake review/lecture write report heads
    ``review_<date>.md`` / ``lecture_<date>.md`` into the fixture logs dir
    exactly as the agent runner would, so the panel's report-head surface
    (REPORT_GLOBS) shows them. A state ledger models the idempotency
    contract: review registers only NEW items, lecture creates only MISSING
    pages, and a second full run writes nothing new.
    """
    from types import SimpleNamespace

    from typer import Exit

    from pku_sync import cli
    from pku_sync.panel import pipelines

    root = Path(args.scratch) / "fixture-automate"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    ledger_path = root / "ledger.json"
    _write_json(
        ledger_path,
        {"review_items": [], "lecture_pages": []},
    )

    def _ledger() -> dict:
        if not ledger_path.exists():
            return {"review_items": [], "lecture_pages": []}
        return json.loads(ledger_path.read_text(encoding="utf-8"))

    state = {"fail_daily": False, "events": []}

    def fake_sync(*, course: str, skip_recordings: bool) -> None:
        assert course == ""
        assert skip_recordings is False
        state["events"].append("sync")
        if state["fail_daily"]:
            raise Exit(1)

    def fake_download(*, course: str, limit: int) -> None:
        assert course == ""
        assert limit == 0
        state["events"].append("download")

    def fake_process(*, course: str, limit: int) -> None:
        assert course == ""
        assert limit == 0
        state["events"].append("process")

    def fake_summarize(log=None) -> None:
        state["events"].append("summarize")

    def fake_review() -> None:
        state["events"].append("review")
        current = _ledger()
        candidate = [f"morning-check-{day}"]
        new = [c for c in candidate if c not in current["review_items"]]
        (logs / f"review_{day}.md").write_text(
            f"review registered {len(new)} NEW item(s): {new}\n"
            "TUI_RESULT=success\n",
            encoding="utf-8",
        )
        if new:
            current["review_items"].extend(new)
            _write_json(ledger_path, current)

    def fake_lecture() -> None:
        state["events"].append("lecture-batch")
        current = _ledger()
        candidate = ["lecture-page:计算机网络/第二讲(missing)"]
        new = [c for c in candidate if c not in current["lecture_pages"]]
        (logs / f"lecture_{day}.md").write_text(
            f"lecture batch created {len(new)} MISSING page(s): {new}\n"
            "TUI_RESULT=success\n",
            encoding="utf-8",
        )
        if new:
            current["lecture_pages"].extend(new)
            _write_json(ledger_path, current)

    originals = (
        cli.sync_cmd,
        cli.download_cmd,
        cli.process_cmd,
        cli.summarize_cmd,
        cli.review_cmd,
        cli.lecture_batch_cmd,
    )
    cli.sync_cmd = fake_sync
    cli.download_cmd = fake_download
    cli.process_cmd = fake_process
    cli.summarize_cmd = fake_summarize
    cli.review_cmd = fake_review
    cli.lecture_batch_cmd = fake_lecture
    fixture_settings = SimpleNamespace(data_dir=root)

    def _run(label: str, fail_daily: bool) -> dict:
        state["fail_daily"] = fail_daily
        state["events"] = []
        runner = pipelines.make_runner(fixture_settings)
        job = runner.submit("automate")
        assert job is not None, "submit returned busy on an idle fixture"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and job.state == "running":
            time.sleep(0.05)
        latest = logs / "latest.daily.log"
        text = latest.read_text(encoding="utf-8", errors="replace") if latest.exists() else ""
        match = re.search(r"=== EXIT_CODE=(\d+) ===", text)
        return {
            "label": label,
            "job": job.as_dict(),
            "events": list(state["events"]),
            "ledger": _ledger(),
            "logged": {
                "exit_code": match.group(1) if match else "?",
                "tail": text[-600:],
            },
        }

    try:
        runs = []
        runs.append(_run("run1-success", False))
        runs.append(_run("run2-success", False))
        runs.append(_run("run3-failure", True))
    finally:
        (
            cli.sync_cmd,
            cli.download_cmd,
            cli.process_cmd,
            cli.summarize_cmd,
            cli.review_cmd,
            cli.lecture_batch_cmd,
        ) = originals

    run1, run2, run3 = runs
    assertions = []
    checks = [
        ("run1 job done exit 0", run1["job"]["state"] == "done" and run1["job"]["exit_code"] == 0),
        ("run1 runs daily then review then lecture-batch",
         run1["events"] == ["sync", "download", "process", "summarize", "review", "lecture-batch"]),
        ("run1 registers one new review item + one missing lecture page",
         run1["ledger"]["review_items"] == [f"morning-check-{day}"]
         and run1["ledger"]["lecture_pages"] == ["lecture-page:计算机网络/第二讲(missing)"]),
        ("run2 job done exit 0 (second full run)", run2["job"]["state"] == "done" and run2["job"]["exit_code"] == 0),
        ("run2 writes nothing new (ledger identical to run1)",
         run2["ledger"] == run1["ledger"]),
        ("run3 job failed with the daily exit code 1",
         run3["job"]["state"] == "failed" and run3["job"]["exit_code"] == 1),
        ("run3 review + lecture-batch still execute after daily failure",
         "review" in run3["events"] and "lecture-batch" in run3["events"]
         and run3["events"].index("review") > run3["events"].index("sync")),
        ("EXIT_CODE=1 marker written to the daily log on failure", "=== EXIT_CODE=1 ===" in run3["logged"]["tail"]),
    ]
    for name, ok in checks:
        assertions.append({"name": name, "result": "pass" if ok else "fail", "detail": "" if ok else name})
    payload = {
        "fixture": str(root),
        "completed_at": _now_iso(),
        "runs": runs,
        "zero_new_writes_run2": run2["ledger"] == run1["ledger"],
        "failure_then_continue": (
            "review" in run3["events"] and "lecture-batch" in run3["events"]
        ),
        "assertions": assertions,
        "all_pass": all(a["result"] == "pass" for a in assertions),
    }
    _write_json(Path(args.out), payload)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(
        f"events run1={run1['events']} run2={run2['events']} "
        f"run3={run3['events']} (panel job {run3['job']['state']}/{run3['job']['exit_code']})"
    )
    return 0 if payload["all_pass"] else 1


def cmd_reports_copy(args) -> int:
    paths = _scratch_paths(args.scratch)
    logs = paths["data_dir"] / "logs"
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    patterns = ("review_*.md", "lecture_*.md", "latest.daily.log", "daily_*.log", "summary_*.md")
    for pattern in patterns:
        for path in sorted(logs.glob(pattern)) if logs.is_dir() else []:
            target = dest / f"{args.prefix}{path.name}"
            target.write_bytes(path.read_bytes())
            copied.append(target.name)
    _write_json(dest / f"{args.prefix}index.json", {"copied_at": _now_iso(), "files": copied})
    print(f"copied {len(copied)} report/log files to {dest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="e2e_window_f1.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p):
        p.add_argument("--scratch", required=True)

    panel_start = sub.add_parser("panel-start")
    _common(panel_start)
    panel_start.add_argument("--relay", required=True)
    panel_start.add_argument("--port", type=int, default=8791)
    panel_start.add_argument("--stdout-log", required=True)
    panel_start.add_argument("--stderr-log", required=True)
    panel_start.add_argument("--allowlist", default="")
    panel_start.set_defaults(func=cmd_panel_start)

    panel_stop = sub.add_parser("panel-stop")
    _common(panel_stop)
    panel_stop.set_defaults(func=cmd_panel_stop)

    probe_sync = sub.add_parser("probe-sync")
    _common(probe_sync)
    probe_sync.add_argument("--relay", required=True)
    probe_sync.add_argument("--out", required=True)
    probe_sync.add_argument("--allowlist", default="")
    probe_sync.set_defaults(func=cmd_probe_sync)

    scan = sub.add_parser("scan-danger")
    _common(scan)
    scan.add_argument("--out", required=True)
    scan.set_defaults(func=cmd_scan_danger)

    env_snap = sub.add_parser("env-snap")
    _common(env_snap)
    env_snap.add_argument("--tag", required=True)
    env_snap.add_argument("--out", required=True)
    env_snap.set_defaults(func=cmd_env_snap)

    token_scope = sub.add_parser("token-scope")
    _common(token_scope)
    token_scope.add_argument("--out", required=True)
    token_scope.set_defaults(func=cmd_token_scope)

    relay_db = sub.add_parser("relay-db")
    relay_db.add_argument("--db", required=True)
    relay_db.add_argument("--out", required=True)
    relay_db.set_defaults(func=cmd_relay_db)

    status_snap = sub.add_parser("status-snap")
    _common(status_snap)
    status_snap.add_argument("--url", default="")
    status_snap.add_argument("--out", required=True)
    status_snap.set_defaults(func=cmd_status_snap)

    directory_snap = sub.add_parser("directory-snap")
    _common(directory_snap)
    directory_snap.add_argument("--url", default="")
    directory_snap.add_argument("--out", required=True)
    directory_snap.set_defaults(func=cmd_directory_snap)

    launch_check = sub.add_parser("launch-check")
    _common(launch_check)
    launch_check.add_argument("--url", default="")
    launch_check.add_argument("--target-id", required=True)
    launch_check.add_argument("--expected-url", required=True)
    launch_check.add_argument("--course-id", default="")
    launch_check.add_argument("--out", required=True)
    launch_check.set_defaults(func=cmd_launch_check)

    notion_snap = sub.add_parser("notion-snap")
    _common(notion_snap)
    notion_snap.add_argument("--tag", required=True)
    notion_snap.add_argument("--directory-file", required=True)
    notion_snap.add_argument("--out", required=True)
    notion_snap.set_defaults(func=cmd_notion_snap)

    notion_diff = sub.add_parser("notion-diff")
    notion_diff.add_argument("--pre", required=True)
    notion_diff.add_argument("--post", required=True)
    notion_diff.add_argument("--out", required=True)
    notion_diff.set_defaults(func=cmd_notion_diff)

    reports_copy = sub.add_parser("reports-copy")
    _common(reports_copy)
    reports_copy.add_argument("--dest", required=True)
    reports_copy.add_argument("--prefix", default="")
    reports_copy.set_defaults(func=cmd_reports_copy)

    fixture_automate = sub.add_parser("fixture-automate")
    _common(fixture_automate)
    fixture_automate.add_argument("--out", required=True)
    fixture_automate.set_defaults(func=cmd_fixture_automate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
