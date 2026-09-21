#!/usr/bin/env python3
"""Window F3 runner (VAL-CROSS-007 / 017 / 018 / 019 -- agent/TUI/entry surfaces).

Leaf actions only; the per-scenario sequencing lives in the window runbook.
Everything a leg touches is validator-owned scratch (scratch root under
%TEMP%, probe-owned panels on 8791, the probe-owned relay on 8800 via the
mission manifest). Zero upstream spend by design: no /v1/llm, no
/v1/transcribe, no OAuth exchange, no real --yes assignment run.

Subcommands:
  stage-scratch    --root --out          seed <root>/client-data from the real
                                         data tree (read-only copy, heavy media
                                         excluded) + create the client-env dir
  tui-console-entry --root --out         launch the REAL pkutui console script
                                         (piped stdio, scratch DATA_DIR, PKU
                                         creds blanked -> zero network), wait
                                         for the render, terminate via the
                                         platform's mechanism, capture markers
  doctor-run       --root --out         run the REAL `pku-sync doctor` entry
                                         against the scratch env; exit 0 proof
  fake-panel-start --variant --port [--scratch] --stdout-log --stderr-log --state
  fake-panel-stop  --state
  action-probe     --panel --kind --out  POST /actions/{kind} (200/404/409 matrix)
  status-probe     --panel --out         GET /healthz + /api/status + quota shape

Secrets discipline: the scratch .env/token/code values live in files only and
are never printed or written to evidence (key NAMES, booleans, and sizes only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")
# Heavy media excluded from the scratch copy (the F1 staging contract: the
# scratch tree carries transcripts/notes/recording markers + materials, no
# videos and no keyframes, so download/process stay honest no-ops).
MEDIA_EXCLUDE = {".mp4", ".ts", ".m4a", ".wav", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MINIMAL_INHERIT = {
    "PATH", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "COMPUTERNAME", "PATHEXT", "SystemDrive", "ProgramFiles", "ProgramData",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_real_env() -> dict:
    env: dict[str, str] = {}
    env_file = CLIENT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text("utf-8", errors="replace").splitlines():
            match = DOTENV_RE.match(line)
            if match and not line.strip().startswith("#"):
                env[match.group(1)] = match.group(2)
    return env


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _pkutui_exe() -> Path:
    exe = Path(sys.executable).with_name("pkutui.exe")
    if not exe.exists():
        raise SystemExit(f"pkutui console script not found next to {sys.executable}")
    return exe


def _child_env() -> dict[str, str]:
    """A minimal explicit child env (no repo .env values leak into children)."""
    env = {key: os.environ[key] for key in MINIMAL_INHERIT if os.environ.get(key)}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# -- staging ------------------------------------------------------------------------


def cmd_stage_scratch(args) -> int:
    real_env = _read_real_env()
    data_source = Path(real_env.get("DATA_DIR", "").strip())
    if not data_source.is_dir():
        raise SystemExit("DATA_DIR is not configured or does not exist (read from the real .env in memory)")
    root = Path(args.root)
    data_dir = root / "client-data"
    client_env = root / "client-env"
    client_env.mkdir(parents=True, exist_ok=True)
    copied = 0
    copied_bytes = 0
    excluded = 0
    excluded_bytes = 0
    if not data_dir.exists():
        for source in data_source.rglob("*"):
            if source.is_dir():
                continue
            relative = source.relative_to(data_source)
            if source.suffix.lower() in MEDIA_EXCLUDE:
                excluded += 1
                excluded_bytes += source.stat().st_size
                continue
            target = data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
            copied_bytes += source.stat().st_size
    else:
        raise SystemExit(f"scratch data dir already exists: {data_dir}")
    payload = {
        "staged_at": _now_iso(),
        "data_source": "real local data tree (read-only copy; never mutated)",
        "copy_rule": "everything except heavy media extensions (no videos, no keyframes/audio), "
                     "so download/process stay honest no-ops and the danger scan must be clean",
        "copied_files": copied,
        "copied_mb": round(copied_bytes / 1e6, 1),
        "excluded_media_files": excluded,
        "excluded_media_mb": round(excluded_bytes / 1e6, 1),
        "client_env_created": True,
        "scratch_root": str(root),
    }
    _write_json(Path(args.out), payload)
    print(
        f"staged scratch tree: copied={copied} files ({copied_bytes / 1e6:.1f} MB), "
        f"excluded_media={excluded} files ({excluded_bytes / 1e6:.1f} MB)"
    )
    return 0


# -- TUI console-script entry ---------------------------------------------------------


def cmd_tui_console_entry(args) -> int:
    root = Path(args.root)
    data_dir = root / "client-data"
    if not data_dir.is_dir():
        raise SystemExit(f"scratch data dir missing: {data_dir}")
    env = _child_env()
    env.update(
        {
            "DATA_DIR": str(data_dir),
            # Credentials blanked in the child process env (env vars beat the
            # package-root .env -- verified host fact): the live fetch fails
            # fast and this leg performs ZERO network I/O by construction.
            "PKU_USERNAME": "",
            "PKU_PASSWORD": "",
        }
    )
    out_path = Path(args.root) / "tui-entry-stdout.txt"
    err_path = Path(args.root) / "tui-entry-stderr.txt"
    started = time.monotonic()
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            [str(_pkutui_exe())], stdin=subprocess.PIPE, stdout=out, stderr=err, env=env
        )
        rendered = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if proc.poll() is not None:
                break
            if out_path.exists():
                text = out_path.read_text("utf-8", errors="replace")
                if "pku-sync 控制台" in text and "课程" in text:
                    rendered = True
                    break
        alive_at_capture = proc.poll() is None
        if not rendered and not alive_at_capture:
            tail = out_path.read_text("utf-8", errors="replace")[-500:]
            errtail = err_path.read_text("utf-8", errors="replace")[-500:]
            raise SystemExit(f"pkutui exited before rendering: rc={proc.returncode} tail={tail!r} err={errtail!r}")
        if not rendered:
            raise SystemExit("pkutui did not render the course table within the window")
        # The platform's termination mechanism for a detached console-less
        # child on Windows (TerminateProcess via Popen.terminate), the
        # equivalent of the POSIX harness's SIGTERM leg.
        proc.terminate()
        try:
            rc = proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait(5)
    elapsed = round(time.monotonic() - started, 1)
    out_text = out_path.read_text("utf-8", errors="replace")
    err_text = err_path.read_text("utf-8", errors="replace")
    payload = {
        "executed_at": _now_iso(),
        "entry": "pkutui console script (real .venv\\Scripts\\pkutui.exe)",
        "render_markers": {
            "title_bar": "pku-sync 控制台" in out_text,
            "course_table": "课程" in out_text,
        },
        "render_stream_bytes": len(out_text.encode("utf-8", errors="replace")),
        "stderr_bytes": len(err_text.encode("utf-8", errors="replace")),
        "alive_before_termination": alive_at_capture,
        "termination_mechanism": "TerminateProcess via Popen.terminate (Windows equivalent of the POSIX SIGTERM leg)",
        "exit_code": rc,
        "elapsed_seconds": elapsed,
        "network": "zero (PKU credentials blanked in the child env; local courses render from the scratch tree)",
    }
    _write_json(Path(args.out), payload)
    print(
        f"pkutui entry: rendered={payload['render_markers']} "
        f"terminate_rc={rc} elapsed={elapsed}s stderr_bytes={payload['stderr_bytes']}"
    )
    return 0


# -- doctor entry ---------------------------------------------------------------------


def cmd_doctor_run(args) -> int:
    root = Path(args.root)
    data_dir = root / "client-data"
    if not data_dir.is_dir():
        raise SystemExit(f"scratch data dir missing: {data_dir}")
    env = _child_env()
    env["DATA_DIR"] = str(data_dir)
    proc = subprocess.run(
        [str(_pku_sync_exe()), "doctor"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    payload = {
        "executed_at": _now_iso(),
        "entry": "pku-sync console script -> doctor",
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr[-2000:],
        "env": "scratch DATA_DIR; credentials/agent host from the package-root .env (read-only)",
    }
    _write_json(Path(args.out), payload)
    print(proc.stdout)
    print(f"doctor exit={proc.returncode}")
    return proc.returncode


# -- fake panel (agent-protocol variants) --------------------------------------------


def cmd_fake_panel_start(args) -> int:
    import httpx

    variant = args.variant
    env = _child_env()
    if args.scratch:
        data_dir = Path(args.scratch) / "client-data"
        if not data_dir.is_dir():
            raise SystemExit(f"scratch data dir missing: {data_dir}")
        env["DATA_DIR"] = str(data_dir)
    out_log = Path(args.stdout_log)
    err_log = Path(args.stderr_log)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    with open(out_log, "wb") as out, open(err_log, "wb") as err:
        proc = subprocess.Popen(
            [
                str(_pku_sync_exe()), "panel", "--no-browser", "--port", str(args.port),
                "--fake", "--fake-variant", variant,
            ],
            cwd=str(Path(args.root if args.root else out_log.parent)),
            env=env,
            stdout=out,
            stderr=err,
        )
        bound_port = None
        startup_line = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = out_log.read_text("utf-8", errors="replace")[-400:]
                raise SystemExit(f"fake panel exited early rc={proc.returncode}: {tail!r}")
            if out_log.exists():
                text = out_log.read_text("utf-8", errors="replace")
                match = PANEL_STARTUP_PORT_RE.search(text)
                if match:
                    bound_port = int(match.group(1))
                    startup_line = text.strip().splitlines()[-1]
                    break
            time.sleep(0.2)
        if bound_port is None:
            raise SystemExit("fake panel startup line did not appear in time")
    health = httpx.get(f"http://127.0.0.1:{bound_port}/healthz", timeout=15)
    health.raise_for_status()
    state = {
        "pid": proc.pid,
        "port": bound_port,
        "variant": variant,
        "startup_line": startup_line,
        "started_at": _now_iso(),
    }
    _write_json(Path(args.state), state)
    print(f"fake panel variant={variant} pid={proc.pid} port={bound_port} healthz=ok")
    print(f"startup line: {startup_line}")
    return 0


def cmd_fake_panel_stop(args) -> int:
    state_path = Path(args.state)
    if not state_path.exists():
        print("no fake panel state file; nothing to stop")
        return 0
    state = json.loads(state_path.read_text("utf-8"))
    pid = state.get("pid")
    port = state.get("port")
    if pid:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"taskkill pid={pid} exit={proc.returncode}")
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
    state_path.unlink(missing_ok=True)
    return 0


# -- panel probes ----------------------------------------------------------------------


def cmd_action_probe(args) -> int:
    import httpx

    url = f"{args.panel.rstrip('/')}/actions/{args.kind}"
    response = httpx.post(url, timeout=30)
    payload = {
        "executed_at": _now_iso(),
        "url": url,
        "kind": args.kind,
        "status_code": response.status_code,
        "body": response.text[:500],
        "ok": response.status_code in (200, 409, 404),
    }
    _write_json(Path(args.out), payload)
    print(f"POST /actions/{args.kind} -> {response.status_code} {response.text[:120]!r}")
    return 0


def cmd_status_probe(args) -> int:
    import httpx

    panel = args.panel.rstrip("/")
    health = httpx.get(f"{panel}/healthz", timeout=15)
    status = httpx.get(f"{panel}/api/status", timeout=15)
    quota = httpx.get(f"{panel}/api/platform/quota", timeout=15)
    payload = {
        "executed_at": _now_iso(),
        "healthz": {"status_code": health.status_code, "body": health.json()},
        "status": {"status_code": status.status_code, "body": status.json()},
        "quota": {"status_code": quota.status_code, "body": quota.json()},
    }
    _write_json(Path(args.out), payload)
    print(
        f"healthz={health.status_code} status={status.status_code} quota={quota.status_code} "
        f"running={payload['status']['body'].get('running') is not None}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2e_window_f3.py")
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage-scratch")
    stage.add_argument("--root", required=True)
    stage.add_argument("--out", required=True)
    stage.set_defaults(func=cmd_stage_scratch)

    tui = sub.add_parser("tui-console-entry")
    tui.add_argument("--root", required=True)
    tui.add_argument("--out", required=True)
    tui.set_defaults(func=cmd_tui_console_entry)

    doctor = sub.add_parser("doctor-run")
    doctor.add_argument("--root", required=True)
    doctor.add_argument("--out", required=True)
    doctor.set_defaults(func=cmd_doctor_run)

    fake_start = sub.add_parser("fake-panel-start")
    fake_start.add_argument("--variant", required=True)
    fake_start.add_argument("--port", type=int, default=8791)
    fake_start.add_argument("--scratch", default="")
    fake_start.add_argument("--root", default="")
    fake_start.add_argument("--stdout-log", required=True)
    fake_start.add_argument("--stderr-log", required=True)
    fake_start.add_argument("--state", required=True)
    fake_start.set_defaults(func=cmd_fake_panel_start)

    fake_stop = sub.add_parser("fake-panel-stop")
    fake_stop.add_argument("--state", required=True)
    fake_stop.set_defaults(func=cmd_fake_panel_stop)

    action = sub.add_parser("action-probe")
    action.add_argument("--panel", required=True)
    action.add_argument("--kind", required=True)
    action.add_argument("--out", required=True)
    action.set_defaults(func=cmd_action_probe)

    status = sub.add_parser("status-probe")
    status.add_argument("--panel", required=True)
    status.add_argument("--out", required=True)
    status.set_defaults(func=cmd_status_probe)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
