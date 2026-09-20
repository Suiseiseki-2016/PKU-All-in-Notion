#!/usr/bin/env python
"""Window B (M4) client cloud-transcription stage probe (VAL-CROSS-005).

Stages a scratch client environment (own scratch .env + scratch DATA_DIR)
holding ONE downloaded recording built from the window's synthetic clip,
then drives the real client path end to end:

  pku-sync process (client pipeline: local audio extraction -> relay
  /v1/transcribe -> real DashScope via private OSS) -> transcript.json written
  -> recording stage ladder advances -> quota decrements by the actual
  duration -> no OSS object remains (proved by the relay-side probe's
  log-key diff + head_object 404) -> the panel's recording state reflects
  the advance.

Isolation: the panel and the process run as probe-owned CHILDREN with an
explicitly controlled environment (DATA_DIR, PLATFORM_TOKEN,
CLOUD_TRANSCRIBE_URL, TRANSCRIPTION_BACKEND=cloud, blanked OPENAI_API_KEY /
NOTION_TOKEN / PKU credentials, DELETE_VIDEO_AFTER_PROCESSING=false). The
package-root .env would otherwise win over a scratch cwd .env for duplicate
keys (verified on this host), so the env dict is the load-bearing override
and the scratch .env documents the scratch client env; the PLATFORM_TOKEN
value never reaches stdout, evidence, or any committed file.

Scratch artifacts (env file, tokens, tree, clips) are deleted at window
cleanup. Evidence goes under the mission validation pack, never into the
repo.

Subcommands:
  stage        build the scratch client env + data tree + staged video;
               verify the recording starts at ladder stage "downloaded"
  panel-start  launch the loopback panel child against the scratch env;
               surface the bound port, PID, and healthz
  capture      capture the ladder/quota/tree state; --phase after also runs
               the full VAL-CROSS-005 checks (needs --before <before.json>)
  run-process  run `pku-sync process --limit 1` as a scratch-env child and
               record the exit code + console output
  panel-stop   stop the probe-owned panel child by PID and verify the port
               is free
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # client dev dependency

from pku_sync.models import Recording
from pku_sync.store import safe_name

COURSE_NAME = "窗口B课程（E2E）"
COURSE_ID = "_windowb_1"
RECORDING_TITLE = "第一讲 云端转写演示"
RECORDING_AT = "2026-09-20 08:00:00"
TEACHER = "E2E"

PANEL_STARTUP_PORT_RE = None  # compiled lazily below (keep imports stdlib-first)
import re

PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _check(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "result": "pass" if ok else "fail", "detail": detail})


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _child_env(root: Path, tokens: dict, relay: str) -> dict[str, str]:
    """The scratch client env for panel/process children (env vars beat .env)."""
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(root) / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{relay}/v1/transcribe",
            "PLATFORM_TOKEN": tokens["client"],
            "TRANSCRIPTION_BACKEND": "cloud",
            "OPENAI_API_KEY": "",
            "NOTION_TOKEN": "",
            "PKU_USERNAME": "",
            "PKU_PASSWORD": "",
            "DELETE_VIDEO_AFTER_PROCESSING": "false",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _scratch_env_text(root: Path, tokens: dict, relay: str) -> str:
    """The scratch client .env (documents the env; the child env dict is
    authoritative because the package-root .env would win duplicate keys)."""
    return (
        "# Window B scratch client env -- deleted at window cleanup.\n"
        "# NOTE: the probe passes these values as process env vars to the\n"
        "# panel/process children (pydantic env vars beat every .env file); this\n"
        "# file documents the scratch client env, mirroring activation output.\n"
        f"DATA_DIR={Path(root) / 'client-data'}\n"
        f"CLOUD_TRANSCRIBE_URL={relay}/v1/transcribe\n"
        f"PLATFORM_TOKEN={tokens['client']}\n"
        "TRANSCRIPTION_BACKEND=cloud\n"
        "DELETE_VIDEO_AFTER_PROCESSING=false\n"
        "OPENAI_API_KEY=\n"
        "NOTION_TOKEN=\n"
        "PKU_USERNAME=\n"
        "PKU_PASSWORD=\n"
    )


def _staged_recording() -> Recording:
    return Recording(
        course_id=COURSE_ID,
        title=RECORDING_TITLE,
        recorded_at=RECORDING_AT,
        teacher=TEACHER,
    )


def _relaunch_probe_paths(root: Path) -> dict:
    from pku_sync.pipeline import RecordingJob

    recording = _staged_recording()
    course_dir = Path(root) / "client-data" / safe_name(COURSE_NAME)
    job = RecordingJob(COURSE_NAME, course_dir, recording)
    return {
        "course_dir": course_dir,
        "recordings_dir": course_dir / "recordings",
        "recording_dir": job.directory,
        "video": job.video,
        "transcript": job.directory / "transcript.json",
        "notes": job.directory / "notes.md",
    }


def _probe_paths(root: Path) -> dict:
    return _relaunch_probe_paths(root)


def cmd_stage(args) -> int:
    root = Path(args.root)
    tokens = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    relay = args.relay.rstrip("/")
    clip = Path(args.clip)
    if not clip.exists():
        raise SystemExit(f"clip not found: {clip}")

    env_dir = root / "client-env"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".env").write_text(_scratch_env_text(root, tokens, relay), encoding="utf-8")

    paths = _probe_paths(root)
    paths["course_dir"].mkdir(parents=True, exist_ok=True)
    (paths["course_dir"] / "course.json").write_text(
        json.dumps({"course_id": COURSE_ID, "name": COURSE_NAME}, ensure_ascii=False),
        encoding="utf-8",
    )
    index = [ _staged_recording().model_dump() ]
    (paths["recordings_dir"]).mkdir(parents=True, exist_ok=True)
    (paths["recordings_dir"] / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # Build the staged video: a real mp4 whose audio track IS the window clip
    # (the client pipeline extracts 16 kHz mono audio from it before upload).
    video = paths["video"]
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=0x2b5797:s=320x240:r=10",
            "-i", str(clip),
            "-shortest",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            str(video),
        ],
        check=True,
    )
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    from pku_sync.pipeline import collect_jobs, recording_stage

    jobs = collect_jobs(root / "client-data")
    stage, unavailable = recording_stage(jobs[0]) if len(jobs) == 1 else ("", "")

    evidence = {
        "step": "stage",
        "generated_at": _utcnow(),
        "root": str(root),
        "relay": relay,
        "course": {"name": COURSE_NAME, "id": COURSE_ID, "dir": str(paths["course_dir"])},
        "recording": {
            "title": RECORDING_TITLE,
            "recorded_at": RECORDING_AT,
            "index_entry": index[0],
            "directory": str(paths["recording_dir"]),
        },
        "video": {
            "path": str(video),
            "bytes": video.stat().st_size,
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "duration_seconds": duration,
        },
        "source_clip": {
            "path": clip.name,
            "bytes": clip.stat().st_size,
            "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
        },
        "scratch_client_env": {
            "env_file": str(env_dir / ".env"),
            "keys": [
                "DATA_DIR", "CLOUD_TRANSCRIBE_URL", "PLATFORM_TOKEN",
                "TRANSCRIPTION_BACKEND", "DELETE_VIDEO_AFTER_PROCESSING",
                "OPENAI_API_KEY", "NOTION_TOKEN", "PKU_USERNAME", "PKU_PASSWORD",
            ],
            "token_recorded_in_evidence": False,
            "note": (
                "the panel/process children receive these as process env vars "
                "(authoritative; the package-root .env wins duplicate keys in a "
                "cwd .env, verified on this host); OPENAI_API_KEY is blanked so "
                "the notes stage skips with zero LLM spend, NOTION_TOKEN/PKU "
                "credentials are blanked so the scratch env never touches the "
                "real workspace or the Teaching Network"
            ),
        },
        "jobs_collected": len(jobs),
        "stage_before_processing": stage,
        "unavailable_reason": unavailable,
    }
    _write_json(args.out, evidence)
    print(
        f"staged: course={COURSE_NAME} recording={RECORDING_TITLE} "
        f"video_bytes={evidence['video']['bytes']} duration={duration}s stage={stage}"
    )
    if len(jobs) != 1 or stage != "downloaded":
        print("FAIL: expected exactly one job at stage 'downloaded'")
        return 1
    return 0


def _wait_for_panel(stdout_log: Path, timeout: float = 25.0) -> tuple[int, str]:
    """Poll the child's captured startup line for the surfaced port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stdout_log.exists():
            text = stdout_log.read_text(encoding="utf-8", errors="replace")
            match = PANEL_STARTUP_PORT_RE.search(text)
            if match:
                return int(match.group(1)), text.strip().splitlines()[-1]
        time.sleep(0.2)
    raise SystemExit("panel startup line did not appear in time")


def _http_get_with_retry(url: str, attempts: int = 20, token: str = "") -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(attempts):
        try:
            response = httpx.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise SystemExit(f"endpoint did not answer in time: {url}")


def cmd_panel_start(args) -> int:
    root = Path(args.root)
    tokens = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    relay = args.relay.rstrip("/")
    stdout_log = Path(args.stdout_log)
    stderr_log = Path(args.stderr_log)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    out_handle = open(stdout_log, "wb")
    err_handle = open(stderr_log, "wb")
    proc = subprocess.Popen(
        [str(_pku_sync_exe()), "panel", "--no-browser", "--port", str(args.port)],
        cwd=str(root / "client-env"),
        env=_child_env(root, tokens, relay),
        stdout=out_handle,
        stderr=err_handle,
    )
    out_handle.close()
    err_handle.close()
    bound_port, startup_line = _wait_for_panel(stdout_log)
    health = _http_get_with_retry(f"http://127.0.0.1:{bound_port}/healthz")
    evidence = {
        "step": "panel-start",
        "generated_at": _utcnow(),
        "pid": proc.pid,
        "requested_port": args.port,
        "bound_port": bound_port,
        "startup_line": startup_line,
        "healthz": health.json(),
        "panel_url": f"http://127.0.0.1:{bound_port}/",
    }
    _write_json(args.out, evidence)
    print(f"panel started: pid={proc.pid} port={bound_port} healthz={health.json()}")
    return 0


def _relay_quota(relay: str, token: str) -> dict:
    response = _http_get_with_retry(f"{relay}/v1/quota", token=token)
    return response.json()


def _relay_db_snapshot(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        usage_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, account_id, kind, seconds, recorded_at FROM usage ORDER BY id"
            )
        ]
        accounts = [
            dict(row)
            for row in conn.execute(
                "SELECT id, label, transcribe_seconds_remaining FROM accounts ORDER BY id"
            )
        ]
    finally:
        conn.close()
    return {"usage_rows": usage_rows, "accounts": accounts}


def cmd_capture(args) -> int:
    root = Path(args.root)
    tokens = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    relay = args.relay.rstrip("/")
    panel = args.panel.rstrip("/")

    from pku_sync.pipeline import collect_jobs, recording_stage

    jobs = collect_jobs(root / "client-data")
    if len(jobs) != 1:
        raise SystemExit(f"expected exactly one staged recording, found {len(jobs)}")
    job = jobs[0]
    stage, unavailable = recording_stage(job)
    transcript_path = job.directory / "transcript.json"
    notes_path = job.directory / "notes.md"

    transcript_payload = None
    transcript_parse_ok = False
    if transcript_path.exists():
        try:
            transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            transcript_parse_ok = True
        except json.JSONDecodeError:
            transcript_parse_ok = False

    panel_status = _http_get_with_retry(f"{panel}/api/status").json()
    panel_quota = _http_get_with_retry(f"{panel}/api/platform/quota").json()
    relay_quota = _relay_quota(relay, tokens["client"])
    db = _relay_db_snapshot(args.relay_db)
    panel_recordings = [
        course for course in panel_status.get("recordings", [])
        if course.get("name") == COURSE_NAME
    ]

    capture = {
        "step": "capture",
        "phase": args.phase,
        "generated_at": _utcnow(),
        "recording_stage_direct": stage,
        "unavailable_reason": unavailable,
        "files": {
            "transcript_exists": transcript_path.exists(),
            "notes_exists": notes_path.exists(),
            "video_exists": job.video.exists(),
        },
        "transcript": (
            {
                "parse_ok": transcript_parse_ok,
                "segment_count": len(transcript_payload.get("segments") or [])
                if isinstance(transcript_payload, dict) else 0,
                "language": transcript_payload.get("language")
                if isinstance(transcript_payload, dict) else None,
                "reused": transcript_payload.get("reused")
                if isinstance(transcript_payload, dict) else None,
                "seconds_charged": transcript_payload.get("seconds_charged")
                if isinstance(transcript_payload, dict) else None,
                "last_segment_end": (
                    (transcript_payload.get("segments") or [{}])[-1].get("end")
                    if isinstance(transcript_payload, dict) and transcript_payload.get("segments")
                    else None
                ),
            }
        ),
        "panel": {
            "recordings": panel_recordings,
            "platform_active": panel_status.get("platform_active"),
            "platform_quota": panel_quota,
        },
        "relay_quota_client_account": relay_quota,
        "relay_db": db,
    }

    checks: list[dict] = []
    if args.phase == "after":
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        transcribe_rows_before = [
            r for r in before["relay_db"]["usage_rows"] if r["kind"] == "transcribe"
        ]
        transcribe_rows_after = [
            r for r in db["usage_rows"] if r["kind"] == "transcribe"
        ]
        new_rows = [
            row
            for row in db["usage_rows"]
            if row["id"] not in {b["id"] for b in before["relay_db"]["usage_rows"]}
        ]
        client_account = next(
            (a for a in db["accounts"] if a["label"] == relay_quota["label"]), None
        )
        seconds_charged = capture["transcript"]["seconds_charged"]

        _check(
            checks,
            "transcript file written and parses with non-empty segments",
            transcript_path.exists() and transcript_parse_ok
            and capture["transcript"]["segment_count"] > 0,
            f"segments={capture['transcript']['segment_count']}",
        )
        _check(
            checks,
            "transcript is a fresh miss (reused=false) -- one real DashScope task for the leg",
            capture["transcript"]["reused"] is False,
            f"reused={capture['transcript']['reused']}",
        )
        _check(
            checks,
            "stage ladder advanced: downloaded -> transcribed (direct pipeline read)",
            before["recording_stage_direct"] == "downloaded" and stage == "transcribed",
            f"before={before['recording_stage_direct']} after={stage}",
        )
        panel_before_stage = None
        for course in before["panel"]["recordings"]:
            for rec in course.get("recordings", []):
                if rec.get("title") == RECORDING_TITLE:
                    panel_before_stage = rec.get("stage")
        panel_after_stage = None
        for course in panel_recordings:
            for rec in course.get("recordings", []):
                if rec.get("title") == RECORDING_TITLE:
                    panel_after_stage = rec.get("stage")
        _check(
            checks,
            "panel recording state reflects the advance (downloaded -> transcribed)",
            panel_before_stage == "downloaded" and panel_after_stage == "transcribed",
            f"panel before={panel_before_stage} after={panel_after_stage}",
        )
        quota_before = before["relay_quota_client_account"]["transcribe_seconds_remaining"]
        quota_after = relay_quota["transcribe_seconds_remaining"]
        _check(
            checks,
            "quota decremented by exactly the charged seconds (ceil of the actual duration)",
            isinstance(seconds_charged, int)
            and quota_before - quota_after == seconds_charged,
            f"before={quota_before} after={quota_after} seconds_charged={seconds_charged} "
            f"last_segment_end={capture['transcript']['last_segment_end']}",
        )
        _check(
            checks,
            "exactly one new transcribe usage row, on the client account, for the charged seconds",
            len(new_rows) == 1
            and new_rows[0]["kind"] == "transcribe"
            and client_account is not None
            and new_rows[0]["account_id"] == client_account["id"]
            and new_rows[0]["seconds"] == seconds_charged,
            f"new_rows={new_rows} client_account_id={client_account['id'] if client_account else None}",
        )
        _check(
            checks,
            "panel platform quota agrees with the relay balance",
            panel_quota.get("active") is True
            and panel_quota.get("transcribe_seconds_remaining") == quota_after,
            f"panel_quota={panel_quota}",
        )
        _check(
            checks,
            "notes stage skipped (no notes.md) -- zero LLM spend in the scratch env",
            not notes_path.exists(),
            f"notes_exists={notes_path.exists()}",
        )
        _check(
            checks,
            "video retained (DELETE_VIDEO_AFTER_PROCESSING=false in the scratch env)",
            job.video.exists(),
        )
        capture["panel_stage_before"] = panel_before_stage
        capture["panel_stage_after"] = panel_after_stage
        capture["transcribe_rows_before_leg"] = len(transcribe_rows_before)
        capture["transcribe_rows_after_leg"] = len(transcribe_rows_after)
        capture["checks"] = checks

    _write_json(args.out, capture)
    print(
        f"capture ({args.phase}): stage={stage} transcript={capture['files']['transcript_exists']} "
        f"quota={relay_quota['transcribe_seconds_remaining']}"
    )
    if checks:
        for check in checks:
            marker = "PASS" if check["result"] == "pass" else "FAIL"
            print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
        return 0 if all(check["result"] == "pass" for check in checks) else 1
    return 0


def cmd_run_process(args) -> int:
    root = Path(args.root)
    tokens = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    relay = args.relay.rstrip("/")
    console_log = Path(args.console_log)
    console_log.parent.mkdir(parents=True, exist_ok=True)
    with open(console_log, "wb") as handle:
        started = _utcnow()
        t0 = time.monotonic()
        result = subprocess.run(
            [str(_pku_sync_exe()), "process", "--limit", "1"],
            cwd=str(root / "client-env"),
            env=_child_env(root, tokens, relay),
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
        elapsed = time.monotonic() - t0
    finished = _utcnow()
    console_text = console_log.read_text(encoding="utf-8", errors="replace")
    evidence = {
        "step": "run-process",
        "generated_at": _utcnow(),
        "timestamps": {"started_at": started, "finished_at": finished},
        "command": f"pku-sync process --limit 1 (probe-owned child; cwd=<scratch client-env>)",
        "child_env_keys": sorted(_child_env(root, tokens, relay).keys()),
        "exit_code": result.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "console_output": console_text,
    }
    _write_json(args.out, evidence)
    print(f"process run: exit={result.returncode} elapsed={elapsed:.1f}s")
    for line in console_text.splitlines()[-6:]:
        print(f"  | {line}")
    return result.returncode


def cmd_panel_stop(args) -> int:
    subprocess.run(["taskkill", "/PID", str(args.pid), "/F", "/T"], capture_output=True)
    deadline = time.monotonic() + 10
    still_listening = True
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", args.port))
        except OSError:
            still_listening = False
            break
        finally:
            probe.close()
        time.sleep(0.25)
    evidence = {
        "step": "panel-stop",
        "generated_at": _utcnow(),
        "pid": args.pid,
        "port": args.port,
        "port_free": not still_listening,
    }
    _write_json(args.out, evidence)
    print(f"panel stopped: pid={args.pid} port_free={not still_listening}")
    return 0 if not still_listening else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2e_window_b_client_stage.py",
        description=(
            "Window B (M4) client cloud-transcription stage probe "
            "(scratch env; secrets never recorded)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="build the scratch client env + one downloaded recording")
    stage.add_argument("--root", required=True)
    stage.add_argument("--tokens", required=True)
    stage.add_argument("--relay", required=True)
    stage.add_argument("--clip", required=True)
    stage.add_argument("--out", required=True)
    stage.set_defaults(func=cmd_stage)

    panel_start = sub.add_parser("panel-start", help="launch the probe-owned panel child")
    panel_start.add_argument("--root", required=True)
    panel_start.add_argument("--tokens", required=True)
    panel_start.add_argument("--relay", required=True)
    panel_start.add_argument("--port", type=int, required=True)
    panel_start.add_argument("--stdout-log", required=True)
    panel_start.add_argument("--stderr-log", required=True)
    panel_start.add_argument("--out", required=True)
    panel_start.set_defaults(func=cmd_panel_start)

    capture = sub.add_parser("capture", help="capture ladder/quota/tree state; after-phase runs checks")
    capture.add_argument("--phase", required=True, choices=["before", "after"])
    capture.add_argument("--root", required=True)
    capture.add_argument("--tokens", required=True)
    capture.add_argument("--relay", required=True)
    capture.add_argument("--panel", required=True)
    capture.add_argument("--relay-db", required=True)
    capture.add_argument("--before", default="", help="before-phase capture JSON (after phase)")
    capture.add_argument("--out", required=True)
    capture.set_defaults(func=cmd_capture)

    run_process = sub.add_parser("run-process", help="run the real client path once")
    run_process.add_argument("--root", required=True)
    run_process.add_argument("--tokens", required=True)
    run_process.add_argument("--relay", required=True)
    run_process.add_argument("--console-log", required=True)
    run_process.add_argument("--out", required=True)
    run_process.set_defaults(func=cmd_run_process)

    panel_stop = sub.add_parser("panel-stop", help="stop the probe-owned panel child by PID")
    panel_stop.add_argument("--pid", type=int, required=True)
    panel_stop.add_argument("--port", type=int, required=True)
    panel_stop.add_argument("--out", required=True)
    panel_stop.set_defaults(func=cmd_panel_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
