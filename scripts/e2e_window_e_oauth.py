#!/usr/bin/env python
"""Window E (M4) OAuth-family attended-session probe (VAL-CROSS-003/012/014).

Runs the relay-mediated Notion OAuth family on a scratch client environment
against a scratch native relay, in ONE attended consent session with the
human performing the real consent clicks:

  Leg 1 (executed first, "deny"): `pku-sync notion login` on the free
    primary callback port 8765 -> the human clicks DENY on the Notion
    authorize page -> the callback carries error=access_denied -> the CLI
    exits 1 with the clean denial message, no token is written, no exchange
    happens (a denial is not an exchange).
  Leg 2 ("retry", shares its single exchange with VAL-CROSS-003/014): with
    8765 occupied by a validator-owned dummy (liveness proof), a fresh
    login binds the approved fallback 8766 and the authorize URL carries
    redirect_uri=http://localhost:8766/callback -> the human clicks ALLOW
    -> the client exchanges the single-use code through the relay
    /v1/notion/exchange (server-held secret; Bearer = scratch platform
    token) -> NOTION_TOKEN lands ONLY in the scratch .env -> whoami
    reports the integration @ workspace -> the scratch panel shows the
    connected state. Ledger for the whole window: exactly ONE successful
    OAuth exchange + one deny click (ceiling: 1 exchange).

Isolation: every child runs with an explicitly controlled process
environment (pydantic env vars beat every .env file, and the package-root
.env would otherwise win duplicate keys over a scratch cwd .env -- verified
on this host): CLOUD_TRANSCRIBE_URL points at the loopback relay (never the
deployed one), PLATFORM_TOKEN/NOTION_TOKEN come from the scratch .env, and
PKU/OPENAI/NOTION credentials are blanked so the scratch env can never
touch the real workspace tokens, the Teaching Network, or production.

Secret discipline: the platform token, the Notion token, the activation
code, the authorization code (never observed by the probe anyway), and the
OAuth client secret (never read by the probe; the relay helper injects it
into the relay process env only) never reach stdout, evidence files, or any
committed file. Evidence records carry key presence + value lengths only.
The authorize URL IS recorded (public client id + redirect_uri + ephemeral
state only).

The human clicks happen in the coordinated agent-browser window; this
probe only starts/waits the CLI children and captures evidence. Scratch
artifacts (env file, code file, relay DB, logs) are deleted at window
cleanup.

Subcommands:
  stage          build the scratch client env + data root; assert the
                 window's ports are free
  create-code    stage one single-use activation code in the scratch relay
                 DB (value to a scratch file, never stdout)
  activate       activate the scratch client against the loopback relay via
                 the real platform.activate path (writes the scratch .env)
  login-start    spawn a detached real `pku-sync notion login` child
                 (--phase deny|retry), surface the authorize URL + the
                 redirect port it will use
  login-status   non-blocking poll of the login child
  login-wait     wait for the login child to exit; capture exit code +
                 full output + post-leg checks
  env-inspect    scratch .env key inventory (lengths only)
  whoami         verify the scratch Notion token -> "integration @ workspace"
  relay-scan     scan the relay stdout/stderr logs for token material +
                 count exchange calls; write a sanitized log excerpt
  relay-db-scan  scan every table of the scratch relay DB for token
                 material (the Notion token must appear nowhere; the
                 platform token only where sessions live legitimately)
  dummy-start/probe/stop  validator-owned occupancy dummy on 8765 with
                 liveness proof (SO_EXCLUSIVEADDRUSE)
  panel-start/connection/stop  scratch panel child; the Notion connection
                 snapshot (connected state) + healthz
  cleanup        stop every child this probe started, verify ports free,
                 delete the scratch root (inventory recorded first)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # client dev dependency

RELAY_REPO = Path(r"E:\remote_project\pku-server")
RELAY_REPO_PYTHON = RELAY_REPO / ".venv" / "Scripts" / "python.exe"
REAL_CLIENT_ENV = Path(r"E:\remote_project\pku-course-sync\.env")

RELAY_URL = "http://127.0.0.1:8800"
CALLBACK_PRIMARY = 8765
CALLBACK_FALLBACK = 8766
PANEL_PORT = 8791
RELAY_PORT = 8800
CLI_LOGIN_TIMEOUT_SECONDS = 900

STATE_NAME = "window-e-state.json"
ENV_KEYS = (
    "DATA_DIR",
    "CLOUD_TRANSCRIBE_URL",
    "PLATFORM_TOKEN",
    "TRANSCRIPTION_BACKEND",
    "OPENAI_API_KEY",
    "NOTION_TOKEN",
    "PKU_USERNAME",
    "PKU_PASSWORD",
)

WRAPPER_SRC = (
    "import subprocess, sys\n"
    "proc = subprocess.run([sys.argv[1]] + sys.argv[2:])\n"
    "print('EXITCODE:%d' % proc.returncode)\n"
)

ACTIVATE_SRC = (
    "import sys\n"
    "from pathlib import Path\n"
    "from pku_sync.config import Settings\n"
    "from pku_sync import platform\n"
    "result = platform.activate(Path(sys.argv[1]).read_text(encoding='utf-8').strip(), Settings())\n"
    "print('ACTIVATED transcribe_seconds_remaining=%d' % int(result['transcribe_seconds_remaining']))\n"
)

WHOAMI_SRC = (
    "from pku_sync.config import Settings\n"
    "from pku_sync.notion_login import verify_token\n"
    "print(verify_token(Settings()))\n"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _check(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "result": "pass" if ok else "fail", "detail": detail})


def _state_path(root: Path) -> Path:
    return root / STATE_NAME


def _load_state(root: Path) -> dict:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def _save_state(root: Path, state: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _state_path(root).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            result[match.group(1)] = match.group(2).strip()
    return result


def _scratch_env_file(root: Path) -> Path:
    return root / "client-env" / ".env"


def _real_env_presence() -> dict:
    """Presence booleans for the keys this window needs from the real client
    .env (values are never copied out; only the public client id is used)."""
    parsed = _parse_env_file(REAL_CLIENT_ENV)
    return {
        "notion_oauth_client_id_present": bool(parsed.get("NOTION_OAUTH_CLIENT_ID")),
        "notion_oauth_client_secret_present": bool(parsed.get("NOTION_OAUTH_CLIENT_SECRET")),
        "parsed_client_id": parsed.get("NOTION_OAUTH_CLIENT_ID", ""),
    }


def _port_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _ports_free(ports) -> dict[str, bool]:
    return {str(port): _port_free(port) for port in ports}


def _http_get_with_retry(url: str, attempts: int = 40, token: str = "", timeout: float = 20.0):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            response = httpx.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response
            last_error = None
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.25)
    raise SystemExit(f"endpoint did not answer in time: {url} ({last_error})")


def _child_env(root: Path, *, platform_token: str, notion_token: str = "") -> dict[str, str]:
    """The scratch client env for every probe-owned child (env vars beat .env)."""
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(root / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{RELAY_URL}/v1/transcribe",
            "PLATFORM_TOKEN": platform_token or "",
            "TRANSCRIPTION_BACKEND": "cloud",
            "NOTION_OAUTH_CLIENT_ID": _real_env_presence()["parsed_client_id"],
            "NOTION_TOKEN": notion_token or "",
            "OPENAI_API_KEY": "",
            "PKU_USERNAME": "",
            "PKU_PASSWORD": "",
            "DELETE_VIDEO_AFTER_PROCESSING": "false",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            # rich wraps console output at 80 columns when stdout is a pipe,
            # which would split the authorize URL across lines in the capture.
            "COLUMNS": "500",
        }
    )
    return env


def _scratch_env_text(root: Path) -> str:
    """The scratch client .env (children read authoritative process env vars;
    this file documents the scratch env and receives the real activation +
    OAuth writes, exactly like the real .env would)."""
    return (
        "# Window E scratch client env -- deleted at window cleanup.\n"
        "# The probe passes these values as process env vars to every child\n"
        "# (pydantic env vars beat every .env file; the package-root .env would\n"
        "# otherwise win duplicate keys, verified on this host). This file\n"
        "# documents the scratch client env and receives the real writes of\n"
        "# activation (PLATFORM_TOKEN/CLOUD_TRANSCRIBE_URL/TRANSCRIPTION_BACKEND)\n"
        "# and the OAuth login (NOTION_TOKEN).\n"
        f"DATA_DIR={root / 'client-data'}\n"
        f"CLOUD_TRANSCRIBE_URL={RELAY_URL}/v1/transcribe\n"
        "PLATFORM_TOKEN=\n"
        "TRANSCRIPTION_BACKEND=cloud\n"
        "OPENAI_API_KEY=\n"
        "NOTION_TOKEN=\n"
        "PKU_USERNAME=\n"
        "PKU_PASSWORD=\n"
    )


def _taskkill(pid: int) -> dict:
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True
    )
    return {
        "pid": pid,
        "exit_code": result.returncode,
        "stdout_tail": (result.stdout or "").strip()[-120:],
    }


def _pid_alive(pid: int) -> bool:
    # tasklist exit code: 0 = process exists, 128 = not found.
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
    )
    return "No tasks are running" not in (result.stdout or "")


def _env_inventory(path: Path) -> list[dict]:
    parsed = _parse_env_file(path)
    return [
        {"key": key, "present": key in parsed, "value_length": len(parsed.get(key, ""))}
        for key in ENV_KEYS
    ]


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------

def cmd_stage(args) -> int:
    root = Path(args.root)
    ports = _ports_free((CALLBACK_PRIMARY, CALLBACK_FALLBACK, PANEL_PORT, RELAY_PORT))
    if not all(ports.values()):
        print(f"FAIL: window ports not free: {ports}")
        return 1
    (root / "client-env").mkdir(parents=True, exist_ok=True)
    (root / "client-data").mkdir(parents=True, exist_ok=True)
    _scratch_env_file(root).write_text(_scratch_env_text(root), encoding="utf-8")
    presence = _real_env_presence()
    if not (presence["notion_oauth_client_id_present"] and presence["notion_oauth_client_secret_present"]):
        print("FAIL: NOTION_OAUTH_CLIENT_ID/SECRET missing from the real client .env")
        return 1
    if not RELAY_REPO_PYTHON.exists():
        print(f"FAIL: relay repo python not found: {RELAY_REPO_PYTHON}")
        return 1
    state = {
        "root": str(root),
        "relay": RELAY_URL,
        "stage_started_at": _utcnow(),
        "pids": {},
        "logs": {},
        "urls": {},
    }
    _save_state(root, state)
    evidence = {
        "step": "stage",
        "generated_at": _utcnow(),
        "root": str(root),
        "relay": RELAY_URL,
        "ports_free_before_window": ports,
        "scratch_client_env": {
            "env_file": str(_scratch_env_file(root)),
            "initial_keys": list(ENV_KEYS),
            "note": "values blank at staging; activation and the OAuth login upsert real values into this file; the file is deleted at cleanup",
        },
        "real_env_presence": {
            "notion_oauth_client_id_present": presence["notion_oauth_client_id_present"],
            "notion_oauth_client_secret_present": presence["notion_oauth_client_secret_present"],
            "client_id_recorded": False,
        },
        "relay_repo_python_present": True,
    }
    _write_json(args.out, evidence)
    print(f"staged scratch window at {root} (ports free: {ports})")
    return 0


# --------------------------------------------------------------------------
# create-code
# --------------------------------------------------------------------------

def cmd_create_code(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    code_file = root / "code.txt"
    result = subprocess.run(
        [
            str(RELAY_REPO_PYTHON),
            "scripts/window_e_create_code.py",
            "--db",
            str(root / "relay.db"),
            "--out",
            str(code_file),
        ],
        cwd=str(RELAY_REPO),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    ok = result.returncode == 0 and code_file.exists() and code_file.read_text(encoding="utf-8").strip() != ""
    state["code_staged"] = ok
    state["code_file"] = str(code_file)
    _save_state(root, state)
    evidence = {
        "step": "create-code",
        "generated_at": _utcnow(),
        "child_exit_code": result.returncode,
        "child_stdout": (result.stdout or "").strip(),
        "code_file": str(code_file),
        "code_value_recorded_in_evidence": False,
        "note": "one single-use activation code staged in the scratch relay DB via the relay's own accounts.create_code; the value lives only in the scratch code file and is deleted at cleanup",
    }
    _write_json(args.out, evidence)
    print(f"create-code: exit={result.returncode} value_in_file_only={ok}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# activate
# --------------------------------------------------------------------------

def cmd_activate(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    code_file = Path(state["code_file"])
    if not code_file.exists():
        print("FAIL: scratch activation code file missing (run create-code first)")
        return 1
    env_file = _scratch_env_file(root)
    result = subprocess.run(
        [str(sys.executable), "-c", ACTIVATE_SRC, str(code_file)],
        cwd=str(root / "client-env"),
        env=_child_env(root, platform_token=""),
        capture_output=True,
        text=True,
        timeout=60,
    )
    parsed = _parse_env_file(env_file)
    platform_token = parsed.get("PLATFORM_TOKEN", "")
    checks: list = []
    _check(checks, "activation child exited 0", result.returncode == 0)
    _check(checks, "activation child printed the scratch activation marker", "ACTIVATED" in (result.stdout or ""))
    _check(checks, "PLATFORM_TOKEN written to the scratch .env only", bool(platform_token))
    _check(
        checks,
        "CLOUD_TRANSCRIBE_URL in the scratch .env points at the loopback relay",
        parsed.get("CLOUD_TRANSCRIBE_URL", "") == f"{RELAY_URL}/v1/transcribe",
    )
    _check(checks, "TRANSCRIPTION_BACKEND=cloud written", parsed.get("TRANSCRIPTION_BACKEND") == "cloud")
    quota = None
    if platform_token:
        response = httpx.get(
            f"{RELAY_URL}/v1/quota",
            headers={"Authorization": f"Bearer {platform_token}"},
            timeout=15,
        )
        quota = {"status_code": response.status_code, "body": response.json() if response.status_code == 200 else None}
        _check(checks, "the scratch session validates against the loopback relay", response.status_code == 200)
    ok = all(item["result"] == "pass" for item in checks)
    state["activated"] = ok
    _save_state(root, state)
    evidence = {
        "step": "activate",
        "generated_at": _utcnow(),
        "child_exit_code": result.returncode,
        "child_stdout": (result.stdout or "").strip(),
        "child_stderr_tail": (result.stderr or "").strip()[-200:],
        "scratch_env_inventory": _env_inventory(env_file),
        "relay_quota": quota,
        "checks": checks,
        "token_value_recorded_in_evidence": False,
    }
    _write_json(args.out, evidence)
    print(f"activate: exit={result.returncode} checks={'all pass' if ok else 'FAIL'}")
    for item in checks:
        print(f"  [{'pass' if item['result'] == 'pass' else 'FAIL'}] {item['name']}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# login legs
# --------------------------------------------------------------------------

def _wait_for_authorize_url(stdout_log: Path, timeout: float = 25.0) -> str:
    """The authorize URL from the CLI's captured stdout.

    Whitespace is stripped before matching: rich soft-wraps a long URL when
    stdout is a pipe, so the captured line may be split across lines even
    with a wide COLUMNS.
    """
    deadline = time.monotonic() + timeout
    pattern = re.compile(r"https://api\.notion\.com/v1/oauth/authorize\?[A-Za-z0-9%&=_.:/+-]+")
    while time.monotonic() < deadline:
        if stdout_log.exists():
            text = stdout_log.read_text(encoding="utf-8", errors="replace")
            match = pattern.search("".join(text.split()))
            if match:
                return match.group(0)
        time.sleep(0.2)
    raise SystemExit("authorize URL did not appear in the login child's stdout in time")


def cmd_login_start(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    phase = args.phase
    platform_token = _parse_env_file(_scratch_env_file(root)).get("PLATFORM_TOKEN", "")
    if not platform_token:
        print("FAIL: scratch PLATFORM_TOKEN missing (run activate first)")
        return 1
    checks: list = []
    if phase == "deny":
        ports = _ports_free((CALLBACK_PRIMARY, CALLBACK_FALLBACK))
        _check(checks, "both approved callback ports free for the primary-port leg", all(ports.values()))
        expected_port = CALLBACK_PRIMARY
    elif phase == "retry":
        try:
            response = httpx.get(f"http://127.0.0.1:{CALLBACK_PRIMARY}/", timeout=5)
            dummy_alive = response.status_code == 200
        except httpx.HTTPError:
            dummy_alive = False
        _check(checks, "validator-owned dummy holds the primary callback port 8765 (liveness proof)", dummy_alive)
        _check(checks, "the approved fallback callback port 8766 is free", _port_free(CALLBACK_FALLBACK))
        expected_port = CALLBACK_FALLBACK
    else:
        raise SystemExit(f"unknown phase: {phase}")
    if not all(item["result"] == "pass" for item in checks):
        _write_json(args.out, {"step": f"login-start-{phase}", "generated_at": _utcnow(), "checks": checks})
        print(f"login-start[{phase}]: preconditions FAIL")
        return 1

    stdout_log = root / f"login-{phase}-stdout.log"
    stderr_log = root / f"login-{phase}-stderr.log"
    out_handle = open(stdout_log, "wb")
    err_handle = open(stderr_log, "wb")
    wrapper = subprocess.Popen(
        [
            str(sys.executable),
            "-c",
            WRAPPER_SRC,
            str(_pku_sync_exe()),
            "notion",
            "login",
            "--no-browser",
            "--port",
            str(CALLBACK_PRIMARY),
            "--timeout",
            str(CLI_LOGIN_TIMEOUT_SECONDS),
        ],
        cwd=str(root / "client-env"),
        env=_child_env(root, platform_token=platform_token, notion_token=""),
        stdout=out_handle,
        stderr=err_handle,
    )
    out_handle.close()
    err_handle.close()
    url = _wait_for_authorize_url(stdout_log)
    query = parse_qs(urlparse(url).query)
    redirect_uri = (query.get("redirect_uri") or [""])[0]
    redirect_port = urlparse(redirect_uri).port if redirect_uri else None
    client_id_matches = (query.get("client_id") or [""])[0] == _real_env_presence()["parsed_client_id"]
    state["pids"][f"login_wrapper_{phase}"] = wrapper.pid
    state["logs"][f"login_{phase}_stdout"] = str(stdout_log)
    state["logs"][f"login_{phase}_stderr"] = str(stderr_log)
    state["urls"][phase] = url
    _save_state(root, state)
    evidence = {
        "step": f"login-start-{phase}",
        "generated_at": _utcnow(),
        "phase": phase,
        "cli_command": (
            "pku-sync notion login --no-browser --port 8765 "
            f"--timeout {CLI_LOGIN_TIMEOUT_SECONDS} (real CLI, probe-owned child)"
        ),
        "authorize_url": url,
        "redirect_uri_as_sent": redirect_uri,
        "redirect_port": redirect_port,
        "expected_redirect_port": expected_port,
        "redirect_port_matches_expected": redirect_port == expected_port,
        "client_id_matches_public_integration": client_id_matches,
        "wrapper_pid": wrapper.pid,
        "started_at": _utcnow(),
        "precondition_checks": checks,
        "note": "state is ephemeral per login and never recorded; the authorize URL carries only the public client id, the redirect URI, and the ephemeral state",
    }
    _write_json(args.out, evidence)
    print(f"login-start[{phase}]: wrapper_pid={wrapper.pid} redirect_port={redirect_port} (expected {expected_port})")
    print(f"AUTHORIZE_URL[{phase}]={url}")
    return 0 if redirect_port == expected_port else 1


def _read_exitcode(stdout_log: Path) -> int | None:
    pattern = re.compile(r"(?m)^EXITCODE:(-?\d+)\s*$")
    match = pattern.search(stdout_log.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def cmd_login_status(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    stdout_log = Path(state["logs"][f"login_{args.phase}_stdout"])
    exit_code = _read_exitcode(stdout_log)
    print(json.dumps({"phase": args.phase, "done": exit_code is not None, "exit_code": exit_code}))
    return 0


def cmd_login_wait(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    phase = args.phase
    stdout_log = Path(state["logs"][f"login_{phase}_stdout"])
    stderr_log = Path(state["logs"][f"login_{phase}_stderr"])
    deadline = time.monotonic() + args.timeout
    exit_code: int | None = None
    while time.monotonic() < deadline:
        exit_code = _read_exitcode(stdout_log)
        if exit_code is not None:
            break
        time.sleep(0.5)
    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log.exists() else ""
    parsed = _parse_env_file(_scratch_env_file(root))
    notion_token = parsed.get("NOTION_TOKEN", "")
    platform_token = parsed.get("PLATFORM_TOKEN", "")

    checks: list = []
    if exit_code is None:
        _check(checks, "login child exited within the wait window", False, f"no EXITCODE marker after {args.timeout}s")
    else:
        _check(checks, "login child exit code captured", True, f"exit={exit_code}")
    if phase == "deny":
        _check(checks, "the CLI exited non-zero (denial)", exit_code == 1)
        _check(
            checks,
            "the denial message is the pinned clean copy with no code/secret echo",
            "授权被拒绝：access_denied" in stdout_text,
        )
        _check(checks, "no authorization code parameter appears anywhere in the CLI output", "code=" not in stdout_text)
        _check(checks, "no token written to the scratch .env (NOTION_TOKEN still empty)", notion_token == "")
    elif phase == "retry":
        _check(checks, "the CLI exited 0 (fresh login succeeded)", exit_code == 0)
        _check(checks, "授权成功 line printed", "授权成功：workspace「" in stdout_text)
        _check(checks, "the CLI surfaced the fallback callback port 8766", "本地回调端口：8766" in stdout_text)
        _check(
            checks,
            "the CLI reports the token written to the scratch .env (path only, never the value)",
            "NOTION_TOKEN 已写入" in stdout_text and str(_scratch_env_file(root)) in stdout_text,
        )
        _check(checks, "NOTION_TOKEN now present in the scratch .env", notion_token != "")
    # Token-echo scans for both phases: neither scratch secret may appear in
    # the CLI output.
    for name, value in (("NOTION_TOKEN", notion_token), ("PLATFORM_TOKEN", platform_token)):
        if value:
            _check(
                checks,
                f"the {name} value never appears in the CLI output",
                value not in stdout_text and value not in stderr_text,
            )
    ok = all(item["result"] == "pass" for item in checks)
    evidence = {
        "step": f"login-wait-{phase}",
        "generated_at": _utcnow(),
        "phase": phase,
        "exit_code": exit_code,
        "cli_stdout": stdout_text,
        "cli_stderr": stderr_text[-500:],
        "scratch_env_inventory_after_leg": _env_inventory(_scratch_env_file(root)),
        "checks": checks,
        "token_values_recorded_in_evidence": False,
    }
    _write_json(args.out, evidence)
    print(f"login-wait[{phase}]: exit={exit_code} checks={'all pass' if ok else 'FAIL'}")
    for item in checks:
        print(f"  [{'pass' if item['result'] == 'pass' else 'FAIL'}] {item['name']}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# env-inspect / whoami
# --------------------------------------------------------------------------

def cmd_env_inspect(args) -> int:
    root = Path(args.root)
    env_file = _scratch_env_file(root)
    parsed = _parse_env_file(env_file)
    evidence = {
        "step": "env-inspect",
        "phase": args.phase,
        "generated_at": _utcnow(),
        "inventory": _env_inventory(env_file),
        "cloud_transcribe_url_value": parsed.get("CLOUD_TRANSCRIBE_URL", ""),
        "transcription_backend_value": parsed.get("TRANSCRIPTION_BACKEND", ""),
        "notion_token_present": bool(parsed.get("NOTION_TOKEN")),
        "notion_token_length": len(parsed.get("NOTION_TOKEN", "")),
        "platform_token_present": bool(parsed.get("PLATFORM_TOKEN")),
        "cloud_transcribe_url_is_loopback": parsed.get("CLOUD_TRANSCRIBE_URL") == f"{RELAY_URL}/v1/transcribe",
        "token_values_recorded_in_evidence": False,
    }
    _write_json(args.out, evidence)
    print(
        f"env-inspect[{args.phase}]: notion_token_present={evidence['notion_token_present']} "
        f"notion_token_length={evidence['notion_token_length']}"
    )
    return 0


def cmd_whoami(args) -> int:
    root = Path(args.root)
    parsed = _parse_env_file(_scratch_env_file(root))
    notion_token = parsed.get("NOTION_TOKEN", "")
    if not notion_token:
        print("FAIL: scratch NOTION_TOKEN missing (no exchange landed)")
        return 1
    result = subprocess.run(
        [str(sys.executable), "-c", WHOAMI_SRC],
        cwd=str(root / "client-env"),
        env=_child_env(root, platform_token="", notion_token=notion_token),
        capture_output=True,
        text=True,
        timeout=60,
    )
    identity = (result.stdout or "").strip()
    ok = result.returncode == 0 and " @ " in identity
    evidence = {
        "step": "whoami",
        "generated_at": _utcnow(),
        "child_exit_code": result.returncode,
        "identity": identity,
        "child_stderr_tail": (result.stderr or "").strip()[-200:],
        "note": "whoami (GET /users/me) run with the scratch OAuth token passed as a process env var; a real Notion read, no mutation",
        "token_value_recorded_in_evidence": False,
    }
    _write_json(args.out, evidence)
    print(f"whoami: exit={result.returncode} identity={identity!r}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# relay log + DB scans
# --------------------------------------------------------------------------

_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsecret_[A-Za-z0-9]{16,}"),
    re.compile(r"\bntn_[A-Za-z0-9]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"[?&]code=[A-Za-z0-9_-]{16,}"),
)


def _scan_text_for_secrets(text: str, values: dict[str, str]) -> dict:
    hits = []
    for name, pattern in enumerate(_SECRET_SHAPES):
        for match in pattern.finditer(text):
            hits.append({"shape_index": name, "excerpt": match.group(0)[:8] + "...REDACTED"})
    value_findings = {}
    for key, value in values.items():
        if value:
            value_findings[key] = value in text
    return {"shape_hits": hits, "value_found": value_findings}


def cmd_relay_scan(args) -> int:
    root = Path(args.root)
    parsed = _parse_env_file(_scratch_env_file(root))
    secret_values = {
        "notion_token": parsed.get("NOTION_TOKEN", ""),
        "platform_token": parsed.get("PLATFORM_TOKEN", ""),
    }
    files_report = []
    excerpt_lines: list[str] = []
    exchange_200 = 0
    exchange_any = 0
    for path in (Path(args.relay_stdout), Path(args.relay_stderr)):
        if not path.exists():
            files_report.append({"file": str(path), "exists": False})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        scan = _scan_text_for_secrets(text, secret_values)
        request_lines = [line for line in text.splitlines() if "POST /v1/notion/exchange" in line]
        count_any = len(request_lines)
        count_200 = sum(1 for line in request_lines if '" 200' in line)
        exchange_200 += count_200
        exchange_any += count_any
        files_report.append(
            {
                "file": str(path),
                "exists": True,
                "bytes": path.stat().st_size,
                "notion_token_found": scan["value_found"].get("notion_token", False),
                "platform_token_found": scan["value_found"].get("platform_token", False),
                "credential_shape_hits": len(scan["shape_hits"]),
                "exchange_request_lines": count_any,
                "exchange_200_lines": count_200,
            }
        )
        # Sanitized excerpt: only access-log/startup lines that pass the
        # credential-shape scan and carry no scratch secret value.
        for line in text.splitlines():
            if secret_values["notion_token"] and secret_values["notion_token"] in line:
                continue
            if secret_values["platform_token"] and secret_values["platform_token"] in line:
                continue
            if any(pattern.search(line) for pattern in _SECRET_SHAPES):
                continue
            if "/v1/" in line or "Uvicorn running" in line or "Application startup" in line or "Started server process" in line:
                excerpt_lines.append(line)
    excerpt_file = Path(args.out).parent / "relay-log-excerpt.txt"
    excerpt_file.write_text("\n".join(excerpt_lines) + "\n", encoding="utf-8")
    evidence = {
        "step": "relay-scan",
        "generated_at": _utcnow(),
        "files": files_report,
        "exchange_calls_total": exchange_any,
        "exchange_200_total": exchange_200,
        "excerpt_file": excerpt_file.name,
        "note": "the relay stdout IS the uvicorn access log (access lines go to stdout); token values scanned by content, never recorded",
    }
    _write_json(args.out, evidence)
    print(
        f"relay-scan: exchange_200={exchange_200} notion_token_in_logs="
        f"{any(f.get('notion_token_found') for f in files_report)}"
    )
    return 0


def cmd_relay_db_scan(args) -> int:
    root = Path(args.root)
    parsed = _parse_env_file(_scratch_env_file(root))
    notion_token = parsed.get("NOTION_TOKEN", "")
    platform_token = parsed.get("PLATFORM_TOKEN", "")
    db_path = root / "relay.db"
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    tables_report = []
    notion_hits: list[str] = []
    platform_hits: list[str] = []
    usage_rows = 0
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        for name in names:
            rows = conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608 - fixed table list from sqlite_master
            count = len(rows)
            found_platform = False
            found_notion = False
            for row in rows:
                for value in tuple(row):
                    text = str(value) if value is not None else ""
                    if notion_token and notion_token in text:
                        found_notion = True
                    if platform_token and platform_token in text:
                        found_platform = True
            if name == "usage":
                usage_rows = count
            if found_notion:
                notion_hits.append(name)
            if found_platform:
                platform_hits.append(name)
            tables_report.append(
                {
                    "table": name,
                    "rows": count,
                    "notion_token_found": found_notion,
                    "platform_token_found": found_platform,
                }
            )
    finally:
        conn.close()
    evidence = {
        "step": "relay-db-scan",
        "generated_at": _utcnow(),
        "db": str(db_path),
        "tables": tables_report,
        "notion_token_in_db": bool(notion_hits),
        "notion_token_hit_tables": notion_hits,
        "platform_token_tables": platform_hits,
        "usage_rows": usage_rows,
        "note": "the platform session token legitimately lives in the sessions table (relay design); the Notion OAuth material must appear in NO table",
    }
    _write_json(args.out, evidence)
    print(
        f"relay-db-scan: notion_token_in_db={bool(notion_hits)} platform_token_tables={platform_hits} usage_rows={usage_rows}"
    )
    return 0


# --------------------------------------------------------------------------
# occupancy dummy (validator-owned, liveness-proven)
# --------------------------------------------------------------------------

class _DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        body = (
            "window-e occupancy dummy: loopback 8765 reserved by the validator "
            "for the OAuth callback fallback leg (liveness proof)."
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        return


class _DummyServer(ThreadingHTTPServer):
    def server_bind(self):
        # Occupancy-honest bind: SO_EXCLUSIVEADDRUSE, exactly like the client's
        # own callback listener (a second bind onto this port must raise).
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def cmd_dummy_serve(args) -> int:
    server = _DummyServer(("127.0.0.1", args.port), _DummyHandler)
    server.serve_forever()
    return 0


def cmd_dummy_start(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    if not _port_free(CALLBACK_PRIMARY):
        print("FAIL: 8765 is already occupied before the dummy start")
        return 1
    log = root / "dummy.log"
    handle = open(log, "wb")
    child = subprocess.Popen(
        [str(sys.executable), str(Path(__file__).resolve()), "dummy-serve", "--port", str(CALLBACK_PRIMARY)],
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    handle.close()
    response = _http_get_with_retry(f"http://127.0.0.1:{CALLBACK_PRIMARY}/", attempts=40, timeout=5)
    state["pids"]["dummy"] = child.pid
    _save_state(root, state)
    evidence = {
        "step": "dummy-start",
        "generated_at": _utcnow(),
        "pid": child.pid,
        "port": CALLBACK_PRIMARY,
        "liveness_status": response.status_code,
        "liveness_body": response.text,
        "bind_semantics": "SO_EXCLUSIVEADDRUSE loopback dummy (validator-owned); never kills or contacts any other process",
    }
    _write_json(args.out, evidence)
    print(f"dummy-start: pid={child.pid} liveness={response.status_code}")
    return 0


def cmd_dummy_probe(args) -> int:
    label = args.label
    try:
        response = httpx.get(f"http://127.0.0.1:{CALLBACK_PRIMARY}/", timeout=5)
        alive = response.status_code == 200
        body = response.text
    except httpx.HTTPError as exc:
        alive = False
        body = f"probe failed: {type(exc).__name__}"
    evidence = {
        "step": "dummy-probe",
        "label": label,
        "generated_at": _utcnow(),
        "port": CALLBACK_PRIMARY,
        "alive": alive,
        "liveness_body": body,
    }
    _write_json(args.out, evidence)
    print(f"dummy-probe[{label}]: alive={alive}")
    return 0 if alive else 1


def cmd_dummy_stop(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    pid = state.get("pids", {}).get("dummy")
    result = _taskkill(pid) if pid else {"pid": None, "exit_code": None, "stdout_tail": "no pid recorded"}
    free = _port_free(CALLBACK_PRIMARY)
    evidence = {
        "step": "dummy-stop",
        "generated_at": _utcnow(),
        "kill": result,
        "port_free_after": free,
    }
    _write_json(args.out, evidence)
    print(f"dummy-stop: kill_exit={result['exit_code']} port_free={free}")
    return 0 if free else 1


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------

def cmd_panel_start(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    parsed = _parse_env_file(_scratch_env_file(root))
    notion_token = parsed.get("NOTION_TOKEN", "")
    platform_token = parsed.get("PLATFORM_TOKEN", "")
    if not (notion_token and platform_token):
        print("FAIL: scratch tokens missing (run the legs first)")
        return 1
    if not _port_free(PANEL_PORT):
        print(f"FAIL: panel port {PANEL_PORT} occupied")
        return 1
    stdout_log = root / "panel-stdout.log"
    stderr_log = root / "panel-stderr.log"
    out_handle = open(stdout_log, "wb")
    err_handle = open(stderr_log, "wb")
    child = subprocess.Popen(
        [str(_pku_sync_exe()), "panel", "--no-browser", "--port", str(PANEL_PORT)],
        cwd=str(root / "client-env"),
        env=_child_env(root, platform_token=platform_token, notion_token=notion_token),
        stdout=out_handle,
        stderr=err_handle,
    )
    out_handle.close()
    err_handle.close()
    health = _http_get_with_retry(f"http://127.0.0.1:{PANEL_PORT}/healthz")
    startup_line = ""
    deadline = time.monotonic() + 15
    pattern = re.compile(r"面板已启动：(http://127\.0\.0\.1:\d+/)")
    while time.monotonic() < deadline:
        text = stdout_log.read_text(encoding="utf-8", errors="replace")
        match = pattern.search(text)
        if match:
            startup_line = match.group(0)
            break
        time.sleep(0.2)
    state["pids"]["panel"] = child.pid
    state["logs"]["panel_stdout"] = str(stdout_log)
    state["logs"]["panel_stderr"] = str(stderr_log)
    _save_state(root, state)
    evidence = {
        "step": "panel-start",
        "generated_at": _utcnow(),
        "pid": child.pid,
        "requested_port": PANEL_PORT,
        "startup_line": startup_line,
        "healthz": health.json(),
        "panel_url": f"http://127.0.0.1:{PANEL_PORT}/",
        "note": "panel child reads the scratch env via authoritative process env vars (scratch NOTION_TOKEN + PLATFORM_TOKEN + loopback relay); never the real workspace .env",
    }
    _write_json(args.out, evidence)
    print(f"panel-start: pid={child.pid} startup_line={startup_line!r}")
    return 0


def cmd_panel_connection(args) -> int:
    panel = f"http://127.0.0.1:{PANEL_PORT}"
    connection = httpx.get(f"{panel}/api/connection", timeout=15)
    health = httpx.get(f"{panel}/healthz", timeout=15)
    quota = httpx.get(f"{panel}/api/platform/quota", timeout=15)
    checks: list = []
    _check(checks, "panel connection snapshot returns connected=true", (connection.json().get("connected") is True))
    _check(
        checks,
        "the connected state carries the approved copy 内容将同步至你的空间 · 已连接",
        connection.json().get("status_label") == "内容将同步至你的空间 · 已连接",
    )
    _check(checks, "connection payload carries no token", "token" not in json.dumps(connection.json()).lower())
    _check(checks, "panel healthz ok", health.json().get("ok") is True)
    _check(
        checks,
        "panel quota reads the scratch relay account (active, loopback)",
        quota.json().get("active") is True and quota.json().get("available") is True,
    )
    ok = all(item["result"] == "pass" for item in checks)
    evidence = {
        "step": "panel-connection",
        "generated_at": _utcnow(),
        "connection": connection.json(),
        "healthz": health.json(),
        "platform_quota": quota.json(),
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"panel-connection: state={connection.json().get('state')} checks={'all pass' if ok else 'FAIL'}")
    for item in checks:
        print(f"  [{'pass' if item['result'] == 'pass' else 'FAIL'}] {item['name']}")
    return 0 if ok else 1


def cmd_panel_dom_check(args) -> int:
    """Check the panel DOM text captured by the browser for the approved copy.

    The comparisons run here rather than inside a browser expression:
    PowerShell 5.1 re-encodes non-ASCII arguments to native commands, so
    Chinese literals sent into a browser eval arrive mangled and every
    comparison silently returns false.
    """
    from pku_sync.panel.connection import CONNECTED_COPY, DISCONNECTED_COPY, REVOKE_LABEL

    COURSES_HEADING = "\u6211\u7684\u8bfe\u7a0b"
    raw = Path(args.dom).read_text(encoding="utf-8").strip()
    payload = json.loads(raw)
    if isinstance(payload, str):  # agent-browser returns the value JSON-quoted
        payload = json.loads(payload)
    text = payload.get("text", "")
    html_token_shape = bool(payload.get("token_shape_in_html"))
    checks: list = []
    _check(checks, "the panel renders the approved connected copy", CONNECTED_COPY in text)
    _check(checks, "the connected row offers the revoke action", REVOKE_LABEL in text)
    _check(checks, "the disconnected copy is absent", DISCONNECTED_COPY not in text)
    _check(
        checks,
        "the directory rendered real workspace metadata read with the scratch token",
        COURSES_HEADING in text,
    )
    _check(checks, "no token-shaped string anywhere in the panel HTML", not html_token_shape)
    ok = all(item["result"] == "pass" for item in checks)
    connection_excerpt = "\n".join(
        line
        for line in text.splitlines()
        if line.strip()
        and any(token in line for token in (CONNECTED_COPY, REVOKE_LABEL, "\u8f6c\u5199", "AI \u70b9"))
    )
    evidence = {
        "step": "panel-dom-check",
        "generated_at": _utcnow(),
        "url": payload.get("url", ""),
        "connection_block_excerpt": connection_excerpt,
        "checks": checks,
        "note": (
            "copy comparisons run inside this UTF-8 script; the browser returns raw "
            "text only because PowerShell 5.1 re-encodes non-ASCII arguments passed "
            "to native commands, which silently breaks in-browser comparisons"
        ),
    }
    _write_json(args.out, evidence)
    print(f"panel-dom-check: {'all pass' if ok else 'FAIL'}")
    for item in checks:
        print(f"  [{'pass' if item['result'] == 'pass' else 'FAIL'}] {item['name']}")
    return 0 if ok else 1


def cmd_panel_stop(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    pid = state.get("pids", {}).get("panel")
    result = _taskkill(pid) if pid else {"pid": None, "exit_code": None, "stdout_tail": "no pid recorded"}
    deadline = time.monotonic() + 10
    still_listening = True
    while time.monotonic() < deadline:
        if _port_free(PANEL_PORT):
            still_listening = False
            break
        time.sleep(0.25)
    evidence = {
        "step": "panel-stop",
        "generated_at": _utcnow(),
        "kill": result,
        "port_free_after": not still_listening,
    }
    _write_json(args.out, evidence)
    print(f"panel-stop: kill_exit={result['exit_code']} port_free={not still_listening}")
    return 0 if not still_listening else 1


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

def cmd_cleanup(args) -> int:
    root = Path(args.root)
    state = _load_state(root)
    stopped = []
    pids = state.get("pids", {})
    for name, pid in pids.items():
        if pid and _pid_alive(int(pid)):
            stopped.append({"name": name, **_taskkill(int(pid))})
        else:
            stopped.append({"name": name, "pid": pid, "already_gone": True})
    if args.relay_wrapper_pid:
        if _pid_alive(int(args.relay_wrapper_pid)):
            stopped.append({"name": "relay_wrapper", **_taskkill(int(args.relay_wrapper_pid))})
        else:
            stopped.append({"name": "relay_wrapper", "pid": args.relay_wrapper_pid, "already_gone": True})
    # Belt-and-braces: any listener still owning a window port is one of this
    # window's own children; stop it by the recorded tree if it survived.
    time.sleep(1.0)
    ports = _ports_free((CALLBACK_PRIMARY, CALLBACK_FALLBACK, PANEL_PORT, RELAY_PORT))
    inventory = sorted(
        (
            {"name": str(p.relative_to(root)), "bytes": p.stat().st_size}
            for p in root.rglob("*")
            if p.is_file()
        ),
        key=lambda entry: entry["name"],
    )
    shutil.rmtree(root, ignore_errors=False)
    root_removed = not root.exists()
    evidence = {
        "step": "cleanup",
        "generated_at": _utcnow(),
        "stopped": stopped,
        "ports_free_after_cleanup": ports,
        "scratch_root": str(root),
        "removed_entry_count": len(inventory),
        "removed_entry_names": [entry["name"] for entry in inventory],
        "root_removed": root_removed,
        "note": "the whole scratch root (env file with both scratch tokens, activation code file, scratch relay DB, login/panel/relay capture logs, dummy log) is deleted; no token value was ever copied into evidence",
    }
    _write_json(args.out, evidence)
    print(f"cleanup: stopped={len(stopped)} ports_free={ports} root_removed={root_removed} entries={len(inventory)}")
    return 0 if all(ports.values()) and root_removed else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2e_window_e_oauth.py",
        description=(
            "Window E (M4) OAuth-family attended-session probe "
            "(scratch env; secrets never recorded)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="build the scratch client env; assert window ports free")
    stage.add_argument("--root", required=True)
    stage.add_argument("--out", required=True)

    create = sub.add_parser("create-code", help="stage one single-use activation code in the scratch relay DB")
    create.add_argument("--root", required=True)
    create.add_argument("--out", required=True)

    activate = sub.add_parser("activate", help="activate the scratch client via the real platform.activate path")
    activate.add_argument("--root", required=True)
    activate.add_argument("--out", required=True)

    login_start = sub.add_parser("login-start", help="spawn the real notion login child and surface the authorize URL")
    login_start.add_argument("--root", required=True)
    login_start.add_argument("--out", required=True)
    login_start.add_argument("--phase", required=True, choices=("deny", "retry"))

    login_status = sub.add_parser("login-status", help="non-blocking poll of the login child")
    login_status.add_argument("--root", required=True)
    login_status.add_argument("--phase", required=True, choices=("deny", "retry"))

    login_wait = sub.add_parser("login-wait", help="wait for the login child; capture exit code + checks")
    login_wait.add_argument("--root", required=True)
    login_wait.add_argument("--out", required=True)
    login_wait.add_argument("--phase", required=True, choices=("deny", "retry"))
    login_wait.add_argument("--timeout", type=float, default=300.0)

    env_inspect = sub.add_parser("env-inspect", help="scratch .env key inventory (lengths only)")
    env_inspect.add_argument("--root", required=True)
    env_inspect.add_argument("--out", required=True)
    env_inspect.add_argument("--phase", required=True)

    whoami = sub.add_parser("whoami", help="verify the scratch Notion token -> integration @ workspace")
    whoami.add_argument("--root", required=True)
    whoami.add_argument("--out", required=True)

    relay_scan = sub.add_parser("relay-scan", help="scan relay logs for token material; count exchanges")
    relay_scan.add_argument("--root", required=True)
    relay_scan.add_argument("--out", required=True)
    relay_scan.add_argument("--relay-stdout", required=True)
    relay_scan.add_argument("--relay-stderr", required=True)

    relay_db = sub.add_parser("relay-db-scan", help="scan every scratch relay DB table for token material")
    relay_db.add_argument("--root", required=True)
    relay_db.add_argument("--out", required=True)

    dummy_serve = sub.add_parser("dummy-serve", help="internal: the occupancy dummy loop (started by dummy-start)")
    dummy_serve.add_argument("--port", type=int, required=True)

    dummy_start = sub.add_parser("dummy-start", help="occupy 8765 with the validator-owned dummy (liveness)")
    dummy_start.add_argument("--root", required=True)
    dummy_start.add_argument("--out", required=True)

    dummy_probe = sub.add_parser("dummy-probe", help="liveness probe of the 8765 dummy")
    dummy_probe.add_argument("--root", required=True)
    dummy_probe.add_argument("--out", required=True)
    dummy_probe.add_argument("--label", required=True)

    dummy_stop = sub.add_parser("dummy-stop", help="stop the dummy and verify 8765 free")
    dummy_stop.add_argument("--root", required=True)
    dummy_stop.add_argument("--out", required=True)

    panel_start = sub.add_parser("panel-start", help="launch the scratch panel child on 8791")
    panel_start.add_argument("--root", required=True)
    panel_start.add_argument("--out", required=True)

    panel_conn = sub.add_parser("panel-connection", help="capture the panel connection snapshot + healthz + quota")
    panel_conn.add_argument("--root", required=True)
    panel_conn.add_argument("--out", required=True)

    panel_dom = sub.add_parser("panel-dom-check", help="check the captured panel DOM text against the approved copy")
    panel_dom.add_argument("--dom", required=True, help="path to the JSON payload captured from the browser")
    panel_dom.add_argument("--out", required=True)

    panel_stop = sub.add_parser("panel-stop", help="stop the scratch panel child and verify the port free")
    panel_stop.add_argument("--root", required=True)
    panel_stop.add_argument("--out", required=True)

    cleanup = sub.add_parser("cleanup", help="stop children, verify ports, delete the scratch root")
    cleanup.add_argument("--root", required=True)
    cleanup.add_argument("--out", required=True)
    cleanup.add_argument("--relay-wrapper-pid", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "stage": cmd_stage,
        "create-code": cmd_create_code,
        "activate": cmd_activate,
        "login-start": cmd_login_start,
        "login-status": cmd_login_status,
        "login-wait": cmd_login_wait,
        "env-inspect": cmd_env_inspect,
        "whoami": cmd_whoami,
        "relay-scan": cmd_relay_scan,
        "relay-db-scan": cmd_relay_db_scan,
        "dummy-serve": cmd_dummy_serve,
        "dummy-start": cmd_dummy_start,
        "dummy-probe": cmd_dummy_probe,
        "dummy-stop": cmd_dummy_stop,
        "panel-start": cmd_panel_start,
        "panel-connection": cmd_panel_connection,
        "panel-dom-check": cmd_panel_dom_check,
        "panel-stop": cmd_panel_stop,
        "cleanup": cmd_cleanup,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
