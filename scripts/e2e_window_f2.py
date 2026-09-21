#!/usr/bin/env python
"""Window F2 orchestrating runner (M4 fault-injection recovery family).

Leaf actions only; the per-scenario sequencing lives in the window runbook.
Everything a leg touches is validator-owned scratch: the scratch root under
%TEMP%, the scratch DB (via the relay launcher), the relay/panel children
started by this runner and tracked by PID, evidence JSONs under the pack.

Env discipline: children receive an EXPLICIT process env (env vars beat the
CWD .env -- verified host fact) built from the real client .env read
in-memory. Secrets (DASHSCOPE_API_KEY, OSS_*, BAILIAN_*, tokens, codes) are
injected into child envs but never printed and never written to evidence.

Usage (from the client repo venv python):
  python scripts/e2e_window_f2.py <subcommand> [args]

Subcommands:
  stage          --root --out               build scratch layout + synthetic clip
  relay-start    --root --port --llm-mode --asr-mode --out [--stub-llm-millipoints ...]
  relay-stop     --pid --port --out
  panel-start    --root --env-dir --port --out
  panel-stop     --pid --port --out
  process-run    --root --course --backend --out
  tree-snap      --root --out
  quota-curl     --root --relay --token-file --out
  activate-curl  --root --relay --code-file --out
  redeem-curl    --root --relay --token-file --code-file --out
  llm-probe      --root --relay --token-file --out
  db-snap        --root --out
  create-code    --root --out [--seconds]
  set-llm-balance --root --token-file --millipoints
  env-snap       --env-dir --tag --out
  stub-snap      --log --out
  adapter-snap   --log --out
  oss-check      --asr-log --relay-access-log --out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELAY_ROOT = Path(r"E:\remote_project\pku-server")
DOTENV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_BLANK_KEYS = (
    "DASHSCOPE_API_KEY",
    "BAILIAN_OPENAI_BASE_URL",
    "BAILIAN_OPENAI_API_KEY",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_BUCKET",
    "OSS_ENDPOINT",
    "PKU_NOTION_KEY",
    "NOTION_TOKEN",
    "NOTION_INTEGRATION_TOKEN",
    "OPENAI_API_KEY",
)
_ALLOW_INHERIT = {
    "PATH", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "COMPUTERNAME", "PATHEXT", "SystemDrive", "ProgramFiles", "ProgramData",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "CUDA_PATH",
    "CUDA_PATH_V12_8", "CUDA_PATH_V13_1", "PYTHONUTF8", "PYTHONIOENCODING", "FORCE_COLOR",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_real_env() -> dict:
    """Read the repo-root .env into memory only; never print values."""
    env: dict[str, str] = {}
    env_file = CLIENT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text("utf-8", errors="replace").splitlines():
            match = DOTENV_RE.match(line)
            if match and not line.strip().startswith("#"):
                env[match.group(1)] = match.group(2)
    return env


def base_child_env(real: dict, overrides: dict | None = None, allow: set | None = None) -> dict:
    env = {}
    for key in _ALLOW_INHERIT:
        if os.environ.get(key):
            env[key] = os.environ[key]
    for key, value in real.items():
        if key not in _BLANK_KEYS and key not in (allow or set()):
            env[key] = value
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("FORCE_COLOR", "1")
    env["NOTION_TOKEN"] = ""
    if overrides:
        env.update({key: value for key, value in overrides.items()})
    return env


def read_code_file(path: Path) -> str:
    return path.read_text("utf-8", errors="replace").strip()


def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<absent>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")


def wait_for_line(log_path: Path, pattern: str, timeout: float, needle: bool = False) -> bool:
    regex = re.compile(pattern)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text("utf-8", errors="replace")
            if needle:
                if regex.findall(text):
                    return True
            elif regex.search(text):
                return True
        time.sleep(0.25)
    return False


def port_free(port: int) -> bool:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue) -ne $null"],
            capture_output=True, text=True, timeout=30,
        )
        return "True" not in proc.stdout
    except Exception:
        return False


def taskkill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=30)


class Scratch:
    def __init__(self, root: Path, relay_root: Path):
        self.root = root
        self.relay_root = relay_root
        self.env = root / "client-env"
        self.env2 = root / "client-env2"
        self.env3 = root / "client-env3"
        self.data = root / "client-data"
        self.db = root / "relay.db"
        self.logs = root / "logs"
        self.token_dir = root / "tokens"

    def ensure(self):
        for part in (self.env, self.env2, self.env3, self.data, self.logs, self.token_dir):
            part.mkdir(parents=True, exist_ok=True)


def cmd_stage(args) -> int:
    root = Path(args.root)
    relay_root = Path(args.relay_root)
    scratch = Scratch(root, relay_root)
    scratch.ensure()
    (root / "pack").mkdir(parents=True, exist_ok=True)
    for env_dir in (scratch.env, scratch.env2, scratch.env3):
        env_file = env_dir / ".env"
        if not env_file.exists():
            env_file.write_text("# window F2 scratch client env (created by e2e_window_f2.py)\n", "utf-8")

    from pku_sync.models import Recording
    from pku_sync.pipeline import RecordingJob, collect_jobs, recording_stage
    from pku_sync.store import safe_name

    clip = scratch.logs / "synthetic.wav"
    if not clip.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=18", "-f", "lavfi", "-i",
             "anoisesrc=duration=18:color=pink:amplitude=0.5", "-filter_complex",
             "amix=inputs=2", "-ac", "1", "-ar", "16000", str(clip)],
            check=True, capture_output=True, timeout=120,
        )
    recs = {
        "course1": {"course_id": "course-window-f2-one", "course_name": "窗口F2复现课程一",
                    "title": "第一讲 云端转写演示", "recorded_at": "2026-09-21 08:00:00"},
        "course2": {"course_id": "course-window-f2-two", "course_name": "窗口F2复现课程二",
                    "title": "第一讲 云端转写演示", "recorded_at": "2026-09-21 09:00:00"},
        "course3": {"course_id": "course-window-f2-three", "course_name": "窗口F2复现课程三",
                    "title": "第一讲 云端转写演示", "recorded_at": "2026-09-21 10:00:00"},
        "course4": {"course_id": "course-window-f2-four", "course_name": "窗口F2复现课程四",
                    "title": "第一讲 云端转写演示", "recorded_at": "2026-09-21 11:00:00"},
    }
    staged: dict[str, dict] = {}
    for key, spec in recs.items():
        recording = Recording(
            course_id=spec["course_id"], title=spec["title"],
            recorded_at=spec["recorded_at"], teacher="E2E",
        )
        course_dir = scratch.data / safe_name(spec["course_name"])
        course_dir.mkdir(parents=True, exist_ok=True)
        (course_dir / "course.json").write_text(
            json.dumps({"course_id": spec["course_id"], "name": spec["course_name"]},
                       ensure_ascii=False), "utf-8")
        recordings_dir = course_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        (recordings_dir / "index.json").write_text(
            json.dumps([recording.model_dump()], ensure_ascii=False), "utf-8")
        job = RecordingJob(spec["course_name"], course_dir, recording)
        video = job.video
        video.parent.mkdir(parents=True, exist_ok=True)
        if not video.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", "color=c=0x2b5797:s=320x240:r=10",
                 "-i", str(clip), "-shortest",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "96k", str(video)],
                check=True, capture_output=True, timeout=180,
            )
        staged[key] = {
            "course_id": spec["course_id"], "course_dir": str(course_dir),
            "recording_dir": str(job.directory), "video": str(video),
        }
    jobs = collect_jobs(scratch.data)
    stages = {}
    for job in jobs:
        stage, _ = recording_stage(job)
        stages[job.course_name] = stage
    write_json(Path(args.out), {
        "ts": now_iso(), "root": str(root.resolve()),
        "staged": staged, "clip": str(clip),
        "env_dirs": [str(scratch.env), str(str(scratch.env2)), str(str(scratch.env3))],
        "jobs_collected": len(jobs), "stages": stages,
    })
    print(json.dumps({"staged": len(staged), "jobs": len(jobs),
                      "db": str(scratch.db)}, ensure_ascii=False))
    if len(jobs) != 4:
        return 1
    return 0


def cmd_relay_start(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    scratch.ensure()
    relay_py = scratch.relay_root / "scripts" / "e2e_window_f2_relay.py"
    run = [scratch.relay_root / ".venv" / "Scripts" / "python.exe", str(relay_py),
           "serve", "--db", str(scratch.db), "--port", str(args.port),
           "--llm-mode", args.llm_mode, "--asr-mode", args.asr_mode]
    if args.stub_llm_millipoints is not None:
        run += ["--stub-llm-millipoints", str(args.stub_llm_millipoints)]
    if args.stub_asr_seconds is not None:
        run += ["--stub-asr-seconds", str(args.stub_asr_seconds)]
    if args.llm_hang is not None:
        run += ["--llm-hang-seconds", str(args.llm_hang)]
    if args.asr_hang is not None:
        run += ["--asr-hang-seconds", str(args.asr_hang)]
    stub_log = scratch.logs / f"stub-calls-{args.llm_mode}-{args.asr_mode}-{args.port}.jsonl"
    run += ["--stub-call-log", str(stub_log)]

    real = read_real_env()
    out_log = scratch.logs / f"relay-{args.port}-{args.llm_mode}-{args.asr_mode}.log"
    err_log = scratch.logs / f"relay-{args.port}-{args.llm_mode}-{args.asr_mode}.err.log"
    env = base_child_env(real)
    # OSS only for the fault-ASR leg (real temp upload + delete); the relay
    # launcher itself holds the INVALID DashScope key for the fault mode.
    if args.asr_mode == "fault":
        for key in ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT"):
            if real.get(key):
                env[key] = real[key]
    with open(out_log, "w", encoding="utf-8") as o, open(err_log, "w", encoding="utf-8") as e:
        proc = subprocess.Popen(run, cwd=str(scratch.relay_root), env=env,
                                stdout=o, stderr=e,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    healthz_url = f"http://127.0.0.1:{args.port}/healthz"
    ok = False
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import httpx
            resp = httpx.get(healthz_url, timeout=1.0)
            if resp.status_code == 200:
                ok = True
                break
        except Exception:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    write_json(Path(args.out), {
        "ts": now_iso(), "pid": proc.pid, "port": args.port,
        "llm_mode": args.llm_mode, "asr_mode": args.asr_mode,
        "healthz_ok": ok, "stub_call_log": str(stub_log),
        "out_log": str(out_log), "err_log": str(err_log),
    })
    if not ok:
        print(f"WARN: relay on {args.port} not healthy (pid {proc.pid})", file=sys.stderr)
        return 1
    return 0


def cmd_relay_stop(args) -> int:
    if args.pid:
        taskkill(int(args.pid))
    free = False
    for _ in range(20):
        if port_free(int(args.port)):
            free = True
            break
        time.sleep(0.25)
    write_json(Path(args.out), {
        "ts": now_iso(), "pid": int(args.pid), "port": int(args.port), "free": free,
    })
    return 0 if free else 1


def cmd_panel_start(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    scratch.ensure()
    env_dir = scratch.root / args.env_dir
    env_dir.mkdir(parents=True, exist_ok=True)
    panel_py = CLIENT_ROOT / "scripts" / "e2e_window_f2_panel.py"
    adapter_log = scratch.logs / f"adapter-{args.env_dir}-{args.port}.jsonl"
    run = [CLIENT_ROOT / ".venv" / "Scripts" / "python.exe", str(panel_py),
           "--port", str(args.port), "--adapter-log", str(adapter_log)]
    real = read_real_env()
    overrides = {
        "DATA_DIR": str(scratch.data),
        "CLOUD_TRANSCRIBE_URL": f"http://127.0.0.1:{args.relay_url_port}/v1/transcribe",
        "PLATFORM_TOKEN": os.environ.get("F2_PLATFORM_TOKEN", ""),
        "TRANSCRIPTION_BACKEND": "cloud",
    }
    env = base_child_env(real, overrides)
    out_log = scratch.logs / f"panel-{args.env_dir}-{args.port}.out.log"
    err_log = scratch.logs / f"panel-{args.env_dir}-{args.port}.err.log"
    with open(out_log, "w", encoding="utf-8") as o, open(err_log, "w", encoding="utf-8") as e:
        proc = subprocess.Popen(run, cwd=str(env_dir), env=env, stdout=o, stderr=e,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    ok = wait_for_line(out_log, r"面板已启动", timeout=40)
    write_json(Path(args.out), {
        "ts": now_iso(), "pid": proc.pid, "port": args.port,
        "env_dir": str(env_dir), "adapter_log": str(adapter_log),
        "out_log": str(out_log), "err_log": str(err_log), "startup_ok": ok,
    })
    if not ok:
        print(f"WARN: panel on {args.port} startup line missing (pid {proc.pid})", file=sys.stderr)
        return 1
    return 0


def cmd_panel_stop(args) -> int:
    return cmd_relay_stop(args)


def cmd_process_run(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    scratch.ensure()
    real = read_real_env()
    if args.token_file:
        # A client-leg token (created earlier via activate-curl --token-out);
        # the transcription client needs it to authenticate to the relay.
        token = read_code_file(Path(args.token_file))
    else:
        token = os.environ.get("F2_PLATFORM_TOKEN", "")
    env = base_child_env(real, {
        "DATA_DIR": str(scratch.data),
        "CLOUD_TRANSCRIBE_URL": f"http://127.0.0.1:{args.relay_url_port}/v1/transcribe",
        "PLATFORM_TOKEN": token,
        "TRANSCRIPTION_BACKEND": args.backend,
        "DELETE_VIDEO_AFTER_PROCESSING": "false",
    })
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this runner via the client repo's venv python"
        )
    run = [str(exe), "process", "--limit", "1"]
    if args.course:
        run += ["--course", args.course]
    out_log = scratch.logs / f"process-{args.backend}-{args.course}.out.log"
    with open(out_log, "w", encoding="utf-8") as o:
        proc = subprocess.run(run, cwd=str(CLIENT_ROOT), env=env,
                              stdout=o, stderr=subprocess.STDOUT, timeout=180)
    tree = {}
    from pku_sync.pipeline import collect_jobs, recording_stage
    for job in collect_jobs(scratch.data):
        stage, unavailable = recording_stage(job)
        tree[job.course_name] = {
            "course_id": job.recording.course_id,
            "slug": job.recording.slug,
            "title": job.recording.title,
            "stage": stage,
            "unavailable_reason": unavailable,
            "transcript_exists": (job.directory / "transcript.json").exists(),
            "video_exists": job.video.exists(),
        }
    write_json(Path(args.out), {
        "ts": now_iso(), "exit": proc.returncode,
        "backend": args.backend, "course": args.course,
        "out_log": str(out_log), "tree": tree,
    })
    return proc.returncode if proc.returncode in (0, None) else 0


def cmd_tree_snap(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    from pku_sync.pipeline import collect_jobs, recording_stage
    tree = {}
    for job in collect_jobs(scratch.data):
        stage, unavailable = recording_stage(job)
        rec_dir = job.directory
        stage_file = rec_dir / "stage.txt"
        tree[job.course_name] = {
            "course_id": job.recording.course_id,
            "slug": job.recording.slug,
            "stage": stage,
            "unavailable_reason": unavailable,
            "transcript_exists": (rec_dir / "transcript.json").exists(),
            "video_exists": job.video.exists(),
            "stage_file_exists": stage_file.exists(),
            "stage_file": stage_file.read_text("utf-8", errors="replace").strip()
                          if stage_file.exists() else None,
            "keyframes": len(list(rec_dir.glob("keyframe_*.jpg"))) if rec_dir.exists() else 0,
        }
    write_json(Path(args.out), {"ts": now_iso(), "tree": tree})
    return 0


def _post_json(url, payload, token=None, timeout=90):
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(url, json=payload, headers=headers, timeout=timeout)


def cmd_quota_curl(args) -> int:
    import httpx
    token = read_code_file(Path(args.token_file)) if args.token_file else ""
    try:
        resp = httpx.get(f"{args.relay}/v1/quota",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        body = resp.text
        status = resp.status_code
    except Exception as exc:
        status, body = 0, f"<transport-failure: {type(exc).__name__}>"
    write_json(Path(args.out), {"ts": now_iso(), "status": status, "body_head": body[:400]})
    return 0


def cmd_activate_curl(args) -> int:
    import httpx
    code = read_code_file(Path(args.code_file))
    resp = httpx.post(f"{args.relay}/v1/activate", json={"code": code}, timeout=30)
    payload = {}
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except Exception:
            pass
    token = payload.get("platform_token") or payload.get("token") or ""
    if token and args.token_out:
        Path(args.token_out).write_text(token, "utf-8")
    write_json(Path(args.out), {
        "ts": now_iso(), "status": resp.status_code,
        "has_token": bool(token), "token_length": len(token),
        "token_out": args.token_out or "",
        "body_head": str(payload)[:200].replace(token, "<token>") if token else str(payload)[:200],
    })
    return 0


def cmd_redeem_curl(args) -> int:
    import httpx
    token = read_code_file(Path(args.token_file))
    code = read_code_file(Path(args.code_file))
    resp = httpx.post(f"{args.relay}/v1/redeem", json={"code": code},
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
    write_json(Path(args.out), {"ts": now_iso(), "status": resp.status_code,
                                "body_head": resp.text[:300]})
    return 0


def cmd_llm_probe(args) -> int:
    import httpx
    token = read_code_file(Path(args.token_file))
    payload = {
        "operation": args.operation,
        "course_title": "F2 PROBE 课程",
        "lecture_title": "PROBE 讲",
        "exercise_title": "PROBE 练习",
        "page_id": "00000000-0000-4000-8000-000000000001",
    }
    resp = httpx.post(f"{args.relay}/v1/llm", json=payload,
                      headers={"Authorization": f"Bearer {token}"}, timeout=120)
    write_json(Path(args.out), {"ts": now_iso(), "status": resp.status_code,
                                "body_head": resp.text[:400]})
    return 0


def cmd_db_snap(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    relay_py = scratch.relay_root / "scripts" / "e2e_window_f2_relay.py"
    out_file = Path(args.out)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    run = [scratch.relay_root / ".venv" / "Scripts" / "python.exe", str(relay_py),
           "db-snap", "--db", str(scratch.db), "--out", str(out_file)]
    proc = subprocess.run(run, capture_output=True, text=True, timeout=60,
                          cwd=str(scratch.relay_root))
    try:
        snap = json.loads(out_file.read_text("utf-8"))
    except Exception:
        snap = {"parse_error": out_file.read_text("utf-8")[-2000:] if out_file.exists() else "",
                "stderr": proc.stderr[-2000:]}
    snap["ts"] = now_iso()
    write_json(out_file, snap)
    return 0


def cmd_create_code(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    code_file = scratch.root / args.name
    relay_py = scratch.relay_root / "scripts" / "e2e_window_f2_relay.py"
    run = [scratch.relay_root / ".venv" / "Scripts" / "python.exe", str(relay_py),
           "create-code", "--db", str(scratch.db), "--out", str(code_file)]
    if args.seconds is not None:
        run += ["--seconds", str(args.seconds)]
    proc = subprocess.run(run, capture_output=True, text=True, timeout=60,
                          cwd=str(scratch.relay_root))
    code = code_file.read_text("utf-8").strip() if code_file.exists() else ""
    write_json(Path(args.out), {
        "ts": now_iso(), "code_file": str(code_file),
        "code_length": len(code), "ok": bool(code) and not proc.returncode,
        "launcher_stderr": proc.stderr[-500:],
    })
    return 0 if (code and proc.returncode == 0) else 1


def cmd_set_llm_balance(args) -> int:
    scratch = Scratch(Path(args.root), Path(args.relay_root))
    token = read_code_file(Path(args.token_file))
    token_file = scratch.token_dir / "balance-token.txt"
    token_file.write_text(token, "utf-8")
    relay_py = scratch.relay_root / "scripts" / "e2e_window_f2_relay.py"
    run = [scratch.relay_root / ".venv" / "Scripts" / "python.exe", str(relay_py),
           "set-llm-balance", "--db", str(scratch.db),
           "--token-file", str(token_file), "--millipoints", str(args.millipoints)]
    proc = subprocess.run(run, capture_output=True, text=True, timeout=60,
                          cwd=str(scratch.relay_root))
    print(proc.stdout.strip())
    if proc.returncode:
        print(proc.stderr[-1000:], file=sys.stderr)
    return proc.returncode


def cmd_env_snap(args) -> int:
    env_dir = Path(args.env_dir)
    env_file = env_dir / ".env"
    snap = {"ts": now_iso(), "tag": args.tag, "env_file": str(env_file),
            "sha256": sha256_file(env_file)}
    if env_file.exists():
        keys = []
        for line in env_file.read_text("utf-8", errors="replace").splitlines():
            m = DOTENV_RE.match(line)
            if m:
                keys.append(m.group(1))
        snap["keys"] = sorted(keys)
        snap["has_token"] = any(k in ("PLATFORM_TOKEN", "NOTION_INTEGRATION_TOKEN",
                                      "NOTION_TOKEN", "PKU_NOTION_KEY") for k in snap["keys"])
    else:
        snap["has_token"] = False
    write_json(Path(args.out), snap)
    return 0


def cmd_stub_snap(args) -> int:
    log_path = Path(args.log)
    calls = []
    if log_path.exists():
        for line in log_path.read_text("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    calls.append(json.loads(line))
                except json.JSONDecodeError:
                    calls.append({"raw": line[:200]})
    summary = {}
    for call in calls:
        op = call.get("operation") or call.get("vendor") or "unknown"
        summary.setdefault(op, 0)
        summary[op] += 1
    write_json(Path(args.out), {"ts": now_iso(), "count": len(calls), "by_op": summary,
                                "calls": calls})
    return 0


def cmd_adapter_snap(args) -> int:
    log_path = Path(args.log)
    calls = []
    if log_path.exists():
        for line in log_path.read_text("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    calls.append(json.loads(line))
                except json.JSONDecodeError:
                    calls.append({"raw": line[:200]})
    by_adapter: dict[str, dict] = {}
    grading_writes = []
    for call in calls:
        adapter = call.get("adapter", "?")
        method = call.get("method", "?")
        entry = by_adapter.setdefault(adapter, {})
        entry.setdefault(method, 0)
        entry[method] += 1
        if method in ("write_result", "mark_past_due", "set_graded_marker"):
            grading_writes.append(call)
    write_json(Path(args.out), {
        "ts": now_iso(), "count": len(calls),
        "grading_writes": grading_writes,
        "by_adapter": by_adapter,
        "calls": calls,
    })
    return 0


def cmd_oss_check(args) -> int:
    asr_log = Path(args.asr_log)
    access_log = Path(args.relay_access_log) if args.relay_access_log else asr_log
    text = asr_log.read_text("utf-8", errors="replace") if asr_log.exists() else ""
    access = access_log.read_text("utf-8", errors="replace") if access_log.exists() else ""
    upload_keys = re.findall(r"oss upload:\s+([\w./:-]+)", text)
    delete_keys = re.findall(r"oss delete:\s+([\w./:-]+)", text)
    real = read_real_env()
    head_statuses = {}
    for key in upload_keys:
        try:
            import oss2
            auth = oss2.Auth(real["OSS_ACCESS_KEY_ID"], real["OSS_ACCESS_KEY_SECRET"])
            bucket = oss2.Bucket(auth, real.get("OSS_ENDPOINT", ""), real["OSS_BUCKET"])
            head_statuses[key] = bucket.object_exists(key) if getattr(bucket, "object_exists", None) else "n/a"
        except Exception as exc:
            head_statuses[key] = f"error:{type(exc).__name__}"
    write_json(Path(args.out), {
        "ts": now_iso(),
        "upload_keys": upload_keys,
        "delete_keys": delete_keys,
        "uploaded_keys_deleted": sorted(set(upload_keys)) == sorted(set(delete_keys)),
        "head_object_exists": head_statuses,
        "transcribe_requests": len(re.findall(r"POST /v1/transcribe", access)),
        "llm_requests": len(re.findall(r"POST /v1/llm", access)),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=str(Path(os.environ.get("TEMP", ".")) / "pku-window-f2"))
    common.add_argument("--relay-root", default=str(DEFAULT_RELAY_ROOT))
    parser = argparse.ArgumentParser(prog="e2e_window_f2.py", parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stage", parents=[common])
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("relay-start", parents=[common])
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--llm-mode", required=True)
    p.add_argument("--asr-mode", required=True)
    p.add_argument("--stub-llm-millipoints", type=int, default=None)
    p.add_argument("--stub-asr-seconds", type=int, default=None)
    p.add_argument("--llm-hang", type=float, default=None)
    p.add_argument("--asr-hang", type=float, default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_relay_start)

    p = sub.add_parser("relay-stop", parents=[common])
    p.add_argument("--pid", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_relay_stop)

    p = sub.add_parser("panel-start", parents=[common])
    p.add_argument("--env-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--relay-url-port", type=int, default=8800)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_panel_start)

    p = sub.add_parser("panel-stop", parents=[common])
    p.add_argument("--pid", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_panel_stop)

    p = sub.add_parser("process-run", parents=[common])
    p.add_argument("--course", default="")
    p.add_argument("--backend", choices=["cloud", "local"], required=True)
    p.add_argument("--relay-url-port", type=int, default=8800)
    p.add_argument("--token-file", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_process_run)

    p = sub.add_parser("tree-snap", parents=[common])
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_tree_snap)

    p = sub.add_parser("quota-curl", parents=[common])
    p.add_argument("--relay", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_quota_curl)

    p = sub.add_parser("activate-curl", parents=[common])
    p.add_argument("--relay", required=True)
    p.add_argument("--code-file", required=True)
    p.add_argument("--token-out", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_activate_curl)

    p = sub.add_parser("redeem-curl", parents=[common])
    p.add_argument("--relay", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--code-file", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_redeem_curl)

    p = sub.add_parser("llm-probe", parents=[common])
    p.add_argument("--relay", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--operation", default="grade")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_llm_probe)

    p = sub.add_parser("db-snap", parents=[common])
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_db_snap)

    p = sub.add_parser("create-code", parents=[common])
    p.add_argument("--seconds", type=int, default=None)
    p.add_argument("--name", default="code-latest.txt")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_create_code)

    p = sub.add_parser("set-llm-balance", parents=[common])
    p.add_argument("--token-file", required=True)
    p.add_argument("--millipoints", type=int, required=True)
    p.set_defaults(fn=cmd_set_llm_balance)

    p = sub.add_parser("env-snap", parents=[common])
    p.add_argument("--env-dir", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_env_snap)

    p = sub.add_parser("stub-snap", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_stub_snap)

    p = sub.add_parser("adapter-snap", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_adapter_snap)

    p = sub.add_parser("oss-check", parents=[common])
    p.add_argument("--asr-log", required=True)
    p.add_argument("--relay-access-log", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_oss_check)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
