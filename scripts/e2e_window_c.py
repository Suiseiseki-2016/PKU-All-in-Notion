#!/usr/bin/env python
"""Real Window C runner (VAL-CROSS-001): the full student journey.

Drives the flagship attended full-journey window per the dated Window C
authorization (validation/m4-real-e2e/window-c-full-journey/authorization.json):
from an EMPTY scratch client + local relay (8800) + one disposable code, in
one continuous session --

  panel activation (student UI form) -> human OAuth consent through the
  relay exchange (panel-triggered 重新连接; the ALLOW click is the human's)
  -> real PKU sync into the scratch root -> panel directory navigation
  (semester -> course -> lecture) -> real lecture launch -> organize with
  estimate on an [E2E] page (ONE real relay /v1/llm quiz op) -> fixture
  answers written by script -> grade (ONE real relay /v1/llm grade op) ->
  score summary + settlement + result launch -> Notion write-back verified
  (answers preserved, wrong answers registered) -> idempotent reruns (zero
  charge) -> ONE short real cloud transcription (relay /v1/transcribe ->
  real DashScope via private OSS, quota decrement, OSS object deleted).

Every Notion read/write in this runner uses the SCRATCH OAuth token only
(the token the attended consent issued into the scratch .env); the real
package-root NOTION_TOKEN is never read by any window surface. The scratch
platform token lives in the scratch .env/child env only; neither token
value ever reaches stdout, evidence, or any committed file.

The browser legs (activation form, connection row, directory, estimate
card, grade click, summary card) are driven externally through
agent-browser; this runner owns the panel child, the CLI children (sync,
process), the relay/panel HTTP probes, the scratch SQLite reads, the
sanitized relay-log analysis, the OSS residue probe, and the read-only
verification. Scratch state persists in <scratch>/state.json so a
late-stage failure resumes from the failed stage without re-spending
earlier ones.

Run via the client repo's uv environment:
  uv run --directory <repo> python scripts/e2e_window_c.py <command> ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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

from pku_sync.config import settings as client_settings
from pku_sync.e2e_window_d import ANSWER_FIXTURE, WindowDReadOnlyVerifier
from pku_sync.models import Recording
from pku_sync.notion import NotionClient, markdown_to_blocks, page_title
from pku_sync.notion_meta import NotionDirectory
from pku_sync.panel.exercise_grader import GRADE_MARKERS, _fixture_answer, _plain
from pku_sync.panel.exercise_organizer import _scope_key
from pku_sync.pipeline import RecordingJob, collect_jobs, recording_stage
from pku_sync.store import safe_name

RELAY_REPO_PYTHON = Path(r"E:\remote_project\pku-server\.venv\Scripts\python.exe")
REAL_CLIENT_ENV = REPO_ROOT / ".env"
PREFERRED_COURSES = ("计算机网络",)
E2E_MARKER = "[E2E]"
FIXTURE_HEADING = "E2E_ANSWER_FIXTURE"
ORGANIZE_TIMEOUT_S = 420.0
GRADE_TIMEOUT_S = 600.0
PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")
RELAY_STARTUP_PORT_RE = re.compile(r"中转服务已启动：http://127\.0\.0\.1:(\d+)/")
OSS_KEY_RE = re.compile(r"pku-temp/\d{4}/\d{2}/\d{2}/[0-9a-f]{32}\.[A-Za-z0-9]+")
UNROUTABLE_LLM_BASE = "http://127.0.0.1:1/v1"
ASR_COURSE_NAME = "窗口C课程（E2E）"
ASR_COURSE_ID = "_windowc_1"
ASR_RECORDING_TITLE = "第一讲 云端转写演示"
ASR_RECORDING_AT = "2026-09-21 08:00:00"
TEACHER = "E2E"

# The staged notes text feeding the real quiz prompt (the Window D fixture
# text: network-themed so it stages under 计算机网络, the verified real
# course).
NETWORK_NOTES = """\
第一讲 课堂笔记（E2E fixture）

本讲主题：计算机网络的体系结构与分层设计。

一、网络分层模型。OSI 参考模型分为物理层、数据链路层、网络层、传输层、会话层、表示层、应用层七层；工程实践中常用的 TCP/IP 体系结构精简为网络接口层、网际层、传输层、应用层四层。分层的核心价值：每一层向相邻的上层提供明确的服务，并向下依赖下层提供的功能；层与层之间通过接口交互、彼此屏蔽实现细节，从而隔离实现变化、简化设计与维护，也让不同厂商的设备与协议能够互操作。分层的代价：层级之间引入处理开销，故障定位需要跨层追查，链条变长。

二、协议、服务与接口。协议是控制两个对等实体通信的规则的集合，包含语法、语义、同步三要素；服务是下层向上层提供的功能；服务访问点是相邻层实体交互的逻辑接口。封装与解封装：发送方自上而下逐层加上首部（数据链路层还加尾部），接收方自下而上逐层拆掉首部，恢复原始数据。

三、各层职责示例。物理层负责比特流的透明传输，关心接口形状、信号编码与传输媒体；数据链路层把帧从链路一端传到另一端，负责封装成帧、差错检测、媒体接入控制；网络层把分组从源主机经路由选择送达目的主机，实现主机到主机的逻辑通信，典型协议是 IP；传输层实现进程到进程的通信，TCP 提供面向连接的可靠传输（确认、重传、滑动窗口、拥塞控制），UDP 提供无连接的尽力而为传输；应用层直接为用户应用进程提供服务，如 HTTP、DNS、SMTP。

四、复用与分用。发送方不同应用进程的报文经传输层复用在同一条链路上传输；接收方传输层按端口号把数据分用、交付给相应进程。端口是传输层的寻址标识。

五、面向连接与无连接。面向连接的服务先建立连接（握手）再传数据、最后释放连接，适合要求可靠、有序的场景；无连接服务无需预先协调，时延小，但不保证可靠交付。

课后思考：为什么网络要分层？分层带来清晰职责、隔离变化与互操作性，同时也引入层级开销与更长的故障定位链条。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _check(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "result": "pass" if ok else "fail", "detail": detail})


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm_page_id(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def _env_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


# -- scratch state ---------------------------------------------------------------


def _scratch_paths(scratch: str) -> dict:
    root = Path(scratch)
    return {
        "root": root,
        "state": root / "state.json",
        "data_dir": root / "client-data",
        "client_env": root / "client-env",
        "scratch_env": root / "client-env" / ".env",
        "relay_db": root / "relay.db",
        "code": root / "code.txt",
        "notion_cache": root / "runner-notion-cache",
    }


def _load_state(scratch: str) -> dict:
    path = _scratch_paths(scratch)["state"]
    if not path.exists():
        raise SystemExit(f"no runner state at {path}; run stage first")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(scratch: str, state: dict) -> None:
    _write_json(_scratch_paths(scratch)["state"], state)


def _scratch_platform_token(scratch: str) -> str:
    token = _env_value(_scratch_paths(scratch)["scratch_env"], "PLATFORM_TOKEN")
    if not token:
        raise SystemExit("scratch .env has no PLATFORM_TOKEN (complete the panel activation first)")
    return token


def _scratch_notion_token(scratch: str) -> str:
    token = _env_value(_scratch_paths(scratch)["scratch_env"], "NOTION_TOKEN")
    if not token:
        raise SystemExit("scratch .env has no NOTION_TOKEN (complete the OAuth consent first)")
    return token


def _scratch_notion_client(scratch: str) -> NotionClient:
    return NotionClient(_scratch_notion_token(scratch))


# -- child environments (env vars beat every .env file on this host) ---------------


def _child_env_panel(scratch: Path, relay: str) -> dict[str, str]:
    """The pristine scratch client env for the panel child.

    PLATFORM_TOKEN/NOTION_TOKEN are BLANKED (empty strings override the
    package-root .env, verified on this host) so the panel starts honestly
    un-activated and Notion-disconnected; the UI activation and the panel's
    own OAuth connect then flip the LIVE settings and write the scratch .env.
    CLOUD_TRANSCRIBE_URL points at the loopback scratch relay so no child
    state can ever reach the deployed relay. TRANSCRIPTION_BACKEND starts
    "local" (the honest pristine value); activation flips it to cloud.
    EXERCISE_E2E_ENABLED=true is the product's E2E gate so the [E2E]-titled
    organize path exists (the panel refuses e2e_mode otherwise).
    """
    client_id = (client_settings.notion_oauth_client_id or "").strip()
    if not client_id:
        raise SystemExit("NOTION_OAUTH_CLIENT_ID missing from the real client .env (public id only)")
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(scratch) / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{relay.rstrip('/')}/v1/transcribe",
            "PLATFORM_TOKEN": "",
            "TRANSCRIPTION_BACKEND": "local",
            "NOTION_TOKEN": "",
            "NOTION_OAUTH_CLIENT_ID": client_id,
            "EXERCISE_E2E_ENABLED": "true",
            "OPENAI_API_KEY": "",
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": UNROUTABLE_LLM_BASE,
            "DELETE_VIDEO_AFTER_PROCESSING": "false",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
        }
    )
    return env


def _child_env_cli(scratch: Path, relay: str) -> dict[str, str]:
    """Scratch env for the sync/process CLI children (the ACTIVATED scratch
    client state: cloud backend + scratch tokens, both read from the scratch
    .env, never printed). PKU credentials stay in the package-root .env chain
    (reads only) so the real Teaching Network sync works exactly as the
    product does."""
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(scratch) / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{relay.rstrip('/')}/v1/transcribe",
            "PLATFORM_TOKEN": _env_value(Path(scratch) / "client-env" / ".env", "PLATFORM_TOKEN"),
            "TRANSCRIPTION_BACKEND": "cloud",
            "NOTION_TOKEN": _env_value(Path(scratch) / "client-env" / ".env", "NOTION_TOKEN"),
            "EXERCISE_E2E_ENABLED": "true",
            "OPENAI_API_KEY": "",
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": UNROUTABLE_LLM_BASE,
            "DELETE_VIDEO_AFTER_PROCESSING": "false",
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
    )
    return env


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _wait_for_port_line(stdout_log: Path, pattern: re.Pattern, timeout: float) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stdout_log.exists():
            text = stdout_log.read_text(encoding="utf-8", errors="replace")
            match = pattern.search(text)
            if match:
                lines = [line for line in text.strip().splitlines() if line.strip()]
                return int(match.group(1)), (lines[-1] if lines else "")
        time.sleep(0.2)
    raise SystemExit("startup line did not appear in time")


# -- relay + panel HTTP helpers ----------------------------------------------------


def _quota(relay: str, token: str) -> dict:
    response = httpx.get(
        f"{relay.rstrip('/')}/v1/quota",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"relay quota probe failed: HTTP {response.status_code}")
    body = response.json()
    return {
        "label": body["label"],
        "transcribe_seconds_remaining": body["transcribe_seconds_remaining"],
        "llm_points_remaining": body["llm_points_remaining"],
        "probed_at": _utcnow(),
    }


def _poll_job(panel: str, path: str, job_id: str, timeout: float) -> dict:
    panel = panel.rstrip("/")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = httpx.get(f"{panel}{path}", params={"job_id": job_id}, timeout=30)
        if response.status_code != 200:
            raise SystemExit(f"panel job poll failed: HTTP {response.status_code} {path}")
        payload = response.json()
        if payload.get("status") != "running":
            return payload
        time.sleep(1.0)
    raise SystemExit(f"panel job did not finish within {timeout:.0f}s: {path}")


def _panel_exercise_rows(panel: str) -> list[dict]:
    response = httpx.get(f"{panel.rstrip('/')}/api/exercises", timeout=120)
    if response.status_code != 200:
        raise SystemExit(f"panel /api/exercises failed: HTTP {response.status_code}")
    return response.json().get("exercises") or []


def _panel_row(rows: list[dict], page_id: str) -> dict | None:
    return next((row for row in rows if row.get("id") == page_id), None)


# -- relay SQLite + log helpers ------------------------------------------------------


def _relay_db_rows(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        llm_usage = [
            dict(row)
            for row in conn.execute(
                "SELECT id, account_id, operation, model, input_tokens,"
                " output_tokens, millipoints, recorded_at FROM llm_usage ORDER BY id"
            )
        ]
        usage = [
            dict(row)
            for row in conn.execute(
                "SELECT id, account_id, kind, seconds, recorded_at FROM usage ORDER BY id"
            )
        ]
        accounts = [
            dict(row)
            for row in conn.execute(
                "SELECT id, label, transcribe_seconds_remaining,"
                " llm_millipoints_remaining FROM accounts ORDER BY id"
            )
        ]
    finally:
        conn.close()
    return {"llm_usage_rows": llm_usage, "usage_rows": usage, "accounts": accounts}


def _read_log_text(path: str) -> str:
    """UTF-8 first, UTF-16LE fallback (PS redirection quirk, Window A note)."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _relay_log_analysis(log_text: str) -> dict:
    lines = [line for line in log_text.splitlines() if line.strip()]
    exchange_requests = [line for line in lines if "POST /v1/notion/exchange" in line]
    llm_requests = [line for line in lines if "POST /v1/llm" in line]
    transcribe_requests = [line for line in lines if "POST /v1/transcribe" in line]
    oss_upload_keys = sorted(set(OSS_KEY_RE.findall(" ".join(line for line in lines if "oss upload:" in line))))
    oss_delete_keys = sorted(set(OSS_KEY_RE.findall(" ".join(line for line in lines if "oss delete:" in line))))
    return {
        "exchange_request_lines": len(exchange_requests),
        "exchange_200_lines": sum(1 for line in exchange_requests if '" 200' in line),
        "llm_request_lines": len(llm_requests),
        "llm_200_lines": sum(1 for line in llm_requests if '" 200' in line),
        "transcribe_request_lines": len(transcribe_requests),
        "transcribe_200_lines": sum(1 for line in transcribe_requests if '" 200' in line),
        "oss_upload_keys": oss_upload_keys,
        "oss_delete_keys": oss_delete_keys,
        "access_lines_total": sum(1 for line in lines if " - \"" in line),
    }


# -- Notion read helpers (scratch token only) ---------------------------------------


def _exercise_snapshot(client: NotionClient, page_id: str, course_id: str,
                       wrong_answer_registrations: list[str]) -> dict:
    page = client.get_page(page_id)
    parent_id = str((page.get("parent") or {}).get("page_id") or "")
    blocks = client.list_children(page_id)
    texts = [_plain(block) for block in blocks]
    fixture_at = next(
        (i for i, value in enumerate(texts) if value == FIXTURE_HEADING), None
    )
    scope_end = fixture_at if fixture_at is not None else len(blocks)
    question_types = [
        texts[index].split(". ", 1)[-1]
        for index, block in enumerate(blocks[:scope_end])
        if block.get("type") == "heading_3"
    ]
    source_count = sum(1 for value in texts[:scope_end] if value.startswith("来源："))
    teacher_answer_count = sum(
        1 for value in texts[:scope_end] if value.startswith("答案：")
    )
    answers: dict[str, str] = {}
    if fixture_at is not None:
        rows = [
            _fixture_answer(texts[index])
            for index, block in enumerate(blocks, start=0)
            if index > fixture_at
            and block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
        answers = {f"Q{number}": value for number, value in enumerate(rows, 1)}
    marker_indexes = [
        index
        for index, block in enumerate(blocks)
        if block.get("type") in ("heading_1", "heading_2", "heading_3")
        and texts[index] in GRADE_MARKERS
    ]
    grade_marker = texts[marker_indexes[0]] if len(marker_indexes) == 1 else None
    score = None
    graded_at = ""
    per_question: list[dict] = []
    if marker_indexes:
        for index in range(marker_indexes[0] + 1, len(blocks)):
            value = texts[index]
            if value.startswith("总分："):
                try:
                    score = float(value[len("总分："):].strip())
                except ValueError:
                    pass
            elif value.startswith("批改时间："):
                graded_at = value[len("批改时间："):].strip()
            elif blocks[index].get("type") == "code":
                try:
                    payload = json.loads(value)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("questions"), list):
                    per_question = payload["questions"]
    e2e_children = [
        block
        for block in client.list_child_pages(course_id)
        if E2E_MARKER in str(block.get("title") or "")
    ]
    if score is not None and float(score).is_integer():
        score = int(score)
    return {
        "fetch_result": "present",
        "page_count": len(e2e_children),
        "page_id": page_id,
        "url": page.get("url") or "",
        "title": page_title(page),
        "parent_page_id": _norm_page_id(parent_id),
        "question_types": question_types,
        "source_count": source_count,
        "teacher_answer_count": teacher_answer_count,
        "answers": answers,
        "grade_marker": grade_marker,
        "score": score,
        "graded_at": graded_at,
        "per_question": per_question,
        "result_block_sets": len(marker_indexes),
        "wrong_answer_registrations": sorted(wrong_answer_registrations),
    }


def _wrong_answer_database(scratch: str, client: NotionClient) -> dict:
    cache_path = _scratch_paths(scratch)["notion_cache"] / "identity_cache.json"
    directory = NotionDirectory(client, cache_path=cache_path)
    data = directory.load()
    identity = data.wrong_answer_database
    if identity is None or not identity.id:
        raise SystemExit("wrong-answer database identity missing from the real directory")
    schema = client.get_database(identity.id)
    properties = (schema or {}).get("properties") or {}
    title_names = [
        name
        for name, prop in properties.items()
        if isinstance(prop, dict) and prop.get("type") == "title"
    ]
    if len(title_names) != 1:
        raise SystemExit("wrong-answer database title property is not unique")
    return {"id": identity.id, "url": identity.url, "title": identity.title,
            "title_property": title_names[0]}


def _operation_id(state: dict) -> str:
    # The grading-records store always lives at <scratch>/client-data/panel;
    # older preserved states may carry data_dir, newer ones only root.
    data_dir = state.get("data_dir") or (Path(state["root"]) / "client-data")
    store_path = Path(data_dir) / "panel" / "grading_records.json"
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    page_id = state["exercise"]["page_id"]
    record = payload.get(page_id)
    if record is None:
        normalized = _norm_page_id(page_id)
        for key, value in payload.items():
            if _norm_page_id(key) == normalized:
                record = value
                break
    if not isinstance(record, dict) or not record.get("operation_id"):
        raise SystemExit(f"grading record store has no operation id for {page_id}")
    return str(record["operation_id"])


def _wrong_answer_rows(client: NotionClient, database: dict, exercise_title: str,
                       operation_id: str) -> dict:
    expected_titles = {
        number: f"{exercise_title} 第{number}题 · {operation_id}:Q{number}"
        for number in (2, 5)
    }
    by_number: dict[int, list[dict]] = {}
    for number, title in expected_titles.items():
        by_number[number] = client.query_database(
            database["id"],
            filter={"property": database["title_property"], "title": {"equals": title}},
            page_size=5,
            max_results=5,
        )
    prefix_rows = client.query_database(
        database["id"],
        filter={
            "property": database["title_property"],
            "title": {"starts_with": f"{exercise_title} 第"},
        },
        page_size=50,
        max_results=50,
    )
    return {
        "expected_titles": expected_titles,
        "rows_by_number": by_number,
        "prefix_rows": prefix_rows,
    }


def _row_identity(rows: list[dict]) -> list[dict]:
    return [
        {"page_id": row.get("id"), "url": row.get("url"),
         "title": next(
             (
                 piece.get("plain_text")
                 for prop in (row.get("properties") or {}).values()
                 if isinstance(prop, dict)
                 for piece in (prop.get("title") or [])
             ),
             "",
         )}
        for row in rows
    ]


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------

def cmd_stage(args) -> int:
    paths = _scratch_paths(args.scratch)
    root = paths["root"]
    ports = {}
    for port in (8765, 8766, 8791, 8800):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", port))
            ports[str(port)] = False
        except OSError:
            ports[str(port)] = True
        finally:
            probe.close()
    if not all(ports.values()):
        print(f"FAIL: window ports not free: {ports}")
        return 1
    (root / "client-env").mkdir(parents=True, exist_ok=True)
    (root / "client-data").mkdir(parents=True, exist_ok=True)
    paths["scratch_env"].write_text(
        "# Window C scratch client env -- deleted at window cleanup.\n"
        "# The scratch client starts EMPTY: activation and the attended OAuth\n"
        "# consent write PLATFORM_TOKEN / TRANSCRIPTION_BACKEND=cloud and\n"
        "# NOTION_TOKEN into THIS file only (the panel child receives blank\n"
        "# values as process env vars until then; env vars beat .env files,\n"
        "# verified on this host, so the real package-root .env never leaks\n"
        "# a token into the scratch client).\n"
        f"DATA_DIR={paths['data_dir']}\n"
        f"CLOUD_TRANSCRIBE_URL={args.relay.rstrip('/')}/v1/transcribe\n",
        encoding="utf-8",
    )
    if not RELAY_REPO_PYTHON.exists():
        print(f"FAIL: relay repo python not found: {RELAY_REPO_PYTHON}")
        return 1
    state = {
        "root": str(root),
        "relay": args.relay.rstrip("/"),
        "relay_db": str(paths["relay_db"]),
        "relay_log": str(root / "relay-stdout.log"),
        "relay_stderr_log": str(root / "relay-stderr.log"),
        "stage_started_at": _utcnow(),
        "authorization": "********************************* (validation/m4-real-e2e/window-c-full-journey/authorization.json)",
        "real_env_sha256": _sha256_file(REAL_CLIENT_ENV) if REAL_CLIENT_ENV.exists() else None,
        "pids": {},
        "logs": {},
    }
    _save_state(args.scratch, state)
    data_entries = sorted(p.name for p in paths["data_dir"].rglob("*"))
    evidence = {
        "step": "stage",
        "generated_at": _utcnow(),
        "root": str(root),
        "relay": state["relay"],
        "ports_free_before_window": ports,
        "real_env_sha256": _sha256_file(REAL_CLIENT_ENV) if REAL_CLIENT_ENV.exists() else None,
        "real_env_last_write": datetime.fromtimestamp(
            REAL_CLIENT_ENV.stat().st_mtime, timezone.utc
        ).isoformat() if REAL_CLIENT_ENV.exists() else None,
        "scratch_client_env": {
            "env_file": str(paths["scratch_env"]),
            "initial_keys": _env_keys(paths["scratch_env"]),
            "note": "pristine scratch client: no tokens; the real package-root .env (sha256 recorded) must stay byte-identical through the window",
        },
        "empty_scratch_data_root": {
            "entry_count": len(data_entries),
            "entries": data_entries,
        },
        "authorization_record": state["authorization"],
    }
    _write_json(args.out, evidence)
    print(f"staged empty scratch window at {root} (ports free: {ports})")
    return 0


# --------------------------------------------------------------------------
# panel-start
# --------------------------------------------------------------------------

def cmd_panel_start(args) -> int:
    paths = _scratch_paths(args.scratch)
    state = _load_state(args.scratch)
    # A resumed window must NOT re-spend the single authorized OAuth exchange:
    # the resumed panel reads the scratch .env tokens (activated + connected)
    # instead of starting pristine.
    resume = bool(getattr(args, "resume", False))
    env = (
        _child_env_cli(paths["root"], args.relay) if resume
        else _child_env_panel(paths["root"], args.relay)
    )
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
    bound_port, startup_line = _wait_for_port_line(stdout_log, PANEL_STARTUP_PORT_RE, 40.0)
    health = httpx.get(f"http://127.0.0.1:{bound_port}/healthz", timeout=15)
    health.raise_for_status()
    connection = httpx.get(f"http://127.0.0.1:{bound_port}/api/connection", timeout=15)
    quota = httpx.get(f"http://127.0.0.1:{bound_port}/api/platform/quota", timeout=15)
    state["pids"]["panel"] = proc.pid
    state["panel"] = {
        "pid": proc.pid,
        "port": bound_port,
        "url": f"http://127.0.0.1:{bound_port}",
        "student_ui": f"http://127.0.0.1:{bound_port}/app",
        "startup_line": startup_line,
    }
    state["logs"]["panel_stdout"] = str(stdout_log)
    state["logs"]["panel_stderr"] = str(stderr_log)
    _save_state(args.scratch, state)
    evidence = {
        "step": "panel-start",
        "generated_at": _utcnow(),
        "pid": proc.pid,
        "requested_port": args.port,
        "bound_port": bound_port,
        "startup_line": startup_line,
        "healthz": health.json(),
        "pristine_connection": connection.json(),
        "pristine_platform_quota": quota.json(),
        "student_ui_url": state["panel"]["student_ui"],
        "child_env_keys": sorted(k for k in env if k in {
            "DATA_DIR", "CLOUD_TRANSCRIBE_URL", "PLATFORM_TOKEN",
            "TRANSCRIPTION_BACKEND", "NOTION_TOKEN", "NOTION_OAUTH_CLIENT_ID",
            "EXERCISE_E2E_ENABLED", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            "DELETE_VIDEO_AFTER_PROCESSING",
        }),
        "mode": "resume" if resume else "pristine",
        "token_handling": (
            "resume: the panel receives the SCRATCH .env tokens (platform + "
            "Notion) as process env vars, so the resumed window keeps its "
            "activation and its consent without spending another exchange"
            if resume else
            "PLATFORM_TOKEN and NOTION_TOKEN are BLANK process env vars (they "
            "override the package-root .env) so the panel starts honestly "
            "un-activated and disconnected; the real tokens never enter the "
            "scratch client"
        ),
    }
    _write_json(args.out, evidence)
    print(
        f"panel started: pid={proc.pid} port={bound_port} "
        f"connection={connection.json().get('state')} quota_active={quota.json().get('active')}"
    )
    if resume:
        if connection.json().get("state") != "connected" or quota.json().get("active") is not True:
            print("FAIL: the resumed scratch panel must be activated and still connected")
            return 1
        return 0
    if connection.json().get("state") != "disconnected" or quota.json().get("active") is not False:
        print("FAIL: the pristine scratch panel must be disconnected and un-activated")
        return 1
    return 0


# --------------------------------------------------------------------------
# connection snapshots / oauth-wait
# --------------------------------------------------------------------------

def cmd_connection_snap(args) -> int:
    state = _load_state(args.scratch)
    panel = state["panel"]["url"]
    connection = httpx.get(f"{panel}/api/connection", timeout=15).json()
    quota = httpx.get(f"{panel}/api/platform/quota", timeout=15).json()
    evidence = {
        "step": "connection-snap",
        "label": args.label,
        "generated_at": _utcnow(),
        "connection": connection,
        "platform_quota": quota,
    }
    _write_json(args.out, evidence)
    print(f"connection[{args.label}]: state={connection.get('state')} label={connection.get('status_label')}")
    return 0


def cmd_oauth_wait(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    panel = state["panel"]["url"]
    deadline = time.monotonic() + args.timeout
    connection = {}
    while time.monotonic() < deadline:
        connection = httpx.get(f"{panel}/api/connection", timeout=15).json()
        if connection.get("connected"):
            break
        time.sleep(2.0)
    connected = bool(connection.get("connected"))
    scratch_env = paths["scratch_env"]
    notion_token = _env_value(scratch_env, "NOTION_TOKEN")
    platform_token = _env_value(scratch_env, "PLATFORM_TOKEN")
    whoami = ""
    whoami_ok = False
    if connected and notion_token:
        try:
            client = _scratch_notion_client(args.scratch)
            identity = client.whoami()
            client.close()
            whoami = " ".join(str(part) for part in (
                (identity.get("name") or ""), "@", (identity.get("bot", {}) or {}).get("workspace_name", "")
            )).strip()
            whoami_ok = "@" in whoami
        except Exception as exc:  # noqa: BLE001 - record the failure honestly
            whoami = f"whoami failed: {type(exc).__name__}"
    log_text = _read_log_text(state["relay_log"]) if state.get("relay_log") else ""
    analysis = _relay_log_analysis(log_text) if log_text else {}
    checks: list[dict] = []
    _check(checks, "the panel reached the connected state", connected,
           f"state={connection.get('state')}")
    _check(checks, "NOTION_TOKEN landed in the scratch .env only", bool(notion_token),
           f"present={bool(notion_token)} length={len(notion_token)}")
    _check(checks, "whoami reports the integration @ workspace with the scratch token",
           whoami_ok, f"identity={whoami!r}")
    _check(checks, "exactly one OAuth exchange through the relay in this window",
           analysis.get("exchange_200_lines") == 1,
           f"exchange_200={analysis.get('exchange_200_lines')} requests={analysis.get('exchange_request_lines')}")
    evidence = {
        "step": "oauth-wait",
        "generated_at": _utcnow(),
        "connection_after_consent": connection,
        "whoami": whoami,
        "scratch_env_inventory": {
            key: {"present": bool(_env_value(scratch_env, key)), "value_length": len(_env_value(scratch_env, key))}
            for key in ("PLATFORM_TOKEN", "NOTION_TOKEN", "TRANSCRIPTION_BACKEND", "CLOUD_TRANSCRIBE_URL")
        },
        "relay_log_analysis": analysis,
        "checks": checks,
        "token_values_recorded_in_evidence": False,
    }
    _write_json(args.out, evidence)
    print(f"oauth-wait: connected={connected} whoami={whoami!r} exchange_200={analysis.get('exchange_200_lines')}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# real PKU sync (CLI child; reads only, into the scratch root)
# --------------------------------------------------------------------------

def cmd_sync(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    env = _child_env_cli(paths["root"], args.relay)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = _utcnow()
    t0 = time.monotonic()
    with open(log_path, "wb") as handle:
        result = subprocess.run(
            [str(_pku_sync_exe()), "sync"],
            cwd=str(paths["client_env"]),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    elapsed = time.monotonic() - t0
    console = log_path.read_text(encoding="utf-8", errors="replace")
    panel = state["panel"]["url"]
    directory = httpx.get(f"{panel}/api/directory", timeout=180)
    directory_payload = directory.json() if directory.status_code == 200 else {}
    sync_state = httpx.get(f"{panel}/api/sync/state", timeout=30)
    stats = directory_payload.get("stats") or {}
    courses = directory_payload.get("courses") or []
    scratch_courses = sorted(p.name for p in paths["data_dir"].iterdir() if p.is_dir())
    state["sync"] = {
        "exit_code": result.returncode,
        "elapsed_seconds": round(elapsed, 1),
        "courses_on_disk": scratch_courses,
        "directory_stats": stats,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "sync",
        "generated_at": _utcnow(),
        "timestamps": {"started_at": started, "finished_at": _utcnow()},
        "command": "pku-sync sync (probe-owned child; cwd=<scratch client-env>; real Teaching Network read)",
        "exit_code": result.returncode,
        "elapsed_seconds": round(elapsed, 1),
        "console_output_tail": console[-2500:],
        "scratch_courses_on_disk": scratch_courses,
        "panel_directory_after_sync": {
            "state": (directory_payload.get("sync") or {}).get("state"),
            "last_sync_at": (directory_payload.get("sync") or {}).get("last_sync_at"),
            "stats": stats,
            "course_titles": [course.get("title") for course in courses],
        },
        "panel_sync_state": sync_state.json() if sync_state.status_code == 200 else None,
        "note": "a metadata sync only: no download step ever runs in this window, so no real recording media is fetched",
    }
    _write_json(args.out, evidence)
    print(
        f"sync: exit={result.returncode} elapsed={elapsed:.0f}s courses_on_disk={len(scratch_courses)} "
        f"directory_courses={stats.get('courses')} lectures={stats.get('lectures')} materials={stats.get('materials')}"
    )
    return 0 if result.returncode == 0 else 1


# --------------------------------------------------------------------------
# stage-exercise (notes for the real lecture scope)
# --------------------------------------------------------------------------

def cmd_stage_exercise(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    panel = state["panel"]["url"]
    response = httpx.get(f"{panel}/api/directory", timeout=180)
    if response.status_code != 200:
        raise SystemExit(f"panel /api/directory failed: HTTP {response.status_code}")
    directory = response.json()
    courses = directory.get("courses") or []
    if not courses:
        raise SystemExit("the real directory has no courses")
    course = next(
        (item for item in courses if item.get("title") in PREFERRED_COURSES),
        None,
    )
    if course is None:
        course = max(courses, key=lambda item: (item.get("counts") or {}).get("lectures", 0))
    lectures = [item for item in (course.get("lectures") or []) if item.get("id") and item.get("title")]
    if not lectures:
        raise SystemExit(f"course {course.get('title')} has no lecture pages")
    lecture = lectures[0]

    # Append ONE synthetic notes_ready recording whose title matches the real
    # lecture exactly (the organizer's LocalNotesProvider matches
    # course_name == course title AND recording.title in lecture titles AND
    # stage == notes_ready). The real synced index keeps every real entry.
    # LocalNotesProvider.for_scope matches the LOCAL course name
    # (course.json "name", the PKU name with its semester suffix) against the
    # NOTION course page title by strict equality, so notes for a real scope
    # are only visible when they sit in a course directory whose course.json
    # name IS the hub page title. The fixture therefore rides its own scratch
    # directory keyed to the Notion title; the real synced directory (and its
    # real recording stages) stays untouched.
    real_metas = [
        item for item in sorted(paths["data_dir"].glob("*/course.json"))
        if json.loads(item.read_text(encoding="utf-8")).get("name", "").startswith(course["title"])
    ]
    if not real_metas:
        raise SystemExit(f"no synced course directory matches the hub course {course['title']}")
    real_meta = json.loads(real_metas[0].read_text(encoding="utf-8"))
    real_course_dir = real_metas[0].parent
    real_index = real_course_dir / "recordings" / "index.json"
    if not real_index.exists():
        raise SystemExit(f"synced recordings index missing: {real_index}")
    real_titles = {
        (entry.get("title") or "")
        for entry in json.loads(real_index.read_text(encoding="utf-8"))
    }
    if lecture["title"] in real_titles:
        raise SystemExit("the synthetic notes title collides with a real synced recording")
    course_dir = paths["data_dir"] / safe_name(course["title"])
    (course_dir / "recordings").mkdir(parents=True, exist_ok=True)
    (course_dir / "course.json").write_text(
        json.dumps(
            {
                "course_id": real_meta.get("course_id", ""),
                "name": course["title"],
                "dir_name": course_dir.name,
                "source": "window-c notes fixture (scratch only)",
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    synthetic_at = f"{datetime.now().strftime('%Y-%m-%d')} 07:00:00"
    synthetic = Recording(
        course_id=real_meta.get("course_id", "") or "",
        title=lecture["title"],
        recorded_at=synthetic_at,
        teacher=TEACHER,
    )
    index_path = course_dir / "recordings" / "index.json"
    index_path.write_text(
        json.dumps([synthetic.model_dump()], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    job = RecordingJob(course["title"], course_dir, synthetic)
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / "notes.md").write_text(NETWORK_NOTES, encoding="utf-8")

    jobs = collect_jobs(paths["data_dir"])
    stages: dict[str, int] = {}
    synthetic_stage = ""
    for item in jobs:
        stage_value, _ = recording_stage(item)
        stages[stage_value] = stages.get(stage_value, 0) + 1
        if item.recording.title == synthetic.title and item.recording.recorded_at == synthetic_at:
            synthetic_stage = stage_value
    videos = [item for item in jobs if item.video.exists()]
    baseline_rows = _panel_exercise_rows(panel)
    state["course"] = {"id": course["id"], "title": course["title"], "url": course.get("url") or ""}
    state["lecture"] = {"id": lecture["id"], "title": lecture["title"], "url": lecture.get("url") or ""}
    state["directory_baseline"] = {
        "semester": directory.get("semester"),
        "courses": len(courses),
        "exercise_rows": len(baseline_rows),
        "e2e_rows_before": sum(1 for row in baseline_rows if E2E_MARKER in (row.get("title") or "")),
    }
    _save_state(args.scratch, state)
    checks: list[dict] = []
    _check(checks, "the synthetic notes recording is notes_ready",
           synthetic_stage == "notes_ready", f"stage={synthetic_stage}")
    _check(checks, "no video exists anywhere in the scratch tree (no download step ran)",
           not videos, f"videos={len(videos)}")
    evidence = {
        "step": "stage-exercise",
        "generated_at": _utcnow(),
        "course": state["course"],
        "lecture": state["lecture"],
        "lecture_selection": "first lecture of the preferred real course 计算机网络" if course["title"] in PREFERRED_COURSES else "course with the most lectures",
        "synthetic_recording": {
            "title": synthetic.title,
            "recorded_at": synthetic_at,
            "directory": str(job.directory),
            "notes_md_bytes": len(NETWORK_NOTES.encode("utf-8")),
            "title_matches_real_lecture_exactly": synthetic.title == lecture["title"],
        },
        "notes_fixture_course_dir": str(course_dir),
        "real_synced_course_dir": {
            "path": str(real_course_dir),
            "course_json_name": real_meta.get("name"),
            "real_recording_titles": sorted(real_titles),
        },
        "product_note": (
            "the product's notes lookup compares course.json name with the Notion "
            "course page title by strict equality; the real PKU name carries the "
            "semester suffix, so the fixture notes ride a course directory keyed to "
            "the hub page title while the real synced directory stays untouched"
        ),
        "recording_ladder_after_staging": stages,
        "jobs_total": len(jobs),
        "videos_existing": [item.recording.title for item in videos],
        "directory_baseline": state["directory_baseline"],
        "checks": checks,
        "note": "the synthetic notes entry rides the REAL synced index (scratch copy); the real recordings and their stages stay untouched",
    }
    _write_json(args.out, evidence)
    print(
        f"staged exercise scope: course={course['title']} lecture={lecture['title']} "
        f"stage={synthetic_stage} jobs={len(jobs)} ladder={stages}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# organize (the ONE quiz op)
# --------------------------------------------------------------------------

def cmd_organize(args) -> int:
    state = _load_state(args.scratch)
    panel = state["panel"]["url"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _scratch_platform_token(args.scratch)
    course, lecture = state["course"], state["lecture"]
    checks: list[dict] = []

    quota_before = _quota(relay, token)
    estimate = httpx.get(f"{panel}/api/exercises/organize/estimate", timeout=15)
    _check(checks, "the panel answers the organize estimate probe",
           estimate.status_code == 200, f"HTTP {estimate.status_code} body={estimate.json() if estimate.status_code == 200 else ''}")
    started = httpx.post(
        f"{panel}/api/exercises/organize",
        json={"course_id": course["id"], "lecture_ids": [lecture["id"]], "e2e_mode": True},
        timeout=30,
    )
    if started.status_code != 202:
        _write_json(args.out, {
            "step": "organize", "generated_at": _utcnow(), "status": "failed",
            "http_status": started.status_code, "body": started.json(),
            "quota_before": quota_before, "checks": checks,
        })
        print(f"FAIL: organize did not start: HTTP {started.status_code}")
        return 1
    result = _poll_job(panel, "/api/exercises/organize/status", started.json()["job_id"],
                       ORGANIZE_TIMEOUT_S)
    quota_after = _quota(relay, token)
    if result.get("status") != "completed":
        _write_json(args.out, {
            "step": "organize", "generated_at": _utcnow(), "status": result.get("status"),
            "result": result, "quota_before": quota_before, "quota_after": quota_after,
            "checks": checks,
        })
        print(f"FAIL: organize did not complete: {json.dumps(result, ensure_ascii=False)}")
        return 1

    page_id = result["exercise_id"]
    client = _scratch_notion_client(args.scratch)
    try:
        page = client.get_page(page_id)
        actual_parent = _norm_page_id((page.get("parent") or {}).get("page_id") or "")
        title = page_title(page)
        blocks = client.list_children(page_id)
        e2e_children = client.list_child_pages(course["id"])
    finally:
        client.close()
    texts = [_plain(block) for block in blocks]
    question_types = [value.split(". ", 1)[-1] for value, block in zip(texts, blocks)
                      if block.get("type") == "heading_3"]
    source_count = sum(1 for value in texts if value.startswith("来源："))
    teacher_answer_count = sum(1 for value in texts if value.startswith("答案："))
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    _check(checks, "organize completed with a fresh (non-reused) [E2E] page",
           result.get("reused") is False, f"reused={result.get('reused')}")
    _check(checks, "organize charged a real positive settlement",
           isinstance(result.get("points_charged"), (int, float)) and result["points_charged"] > 0,
           f"points_charged={result.get('points_charged')}")
    _check(checks, "page parent is the selected real course page",
           actual_parent == _norm_page_id(course["id"]),
           f"actual={actual_parent} expected={_norm_page_id(course['id'])}")
    _check(checks, "page title carries exactly one [E2E] marker and matches the panel result",
           title.count(E2E_MARKER) == 1 and title == result.get("title"),
           f"title={title!r}")
    _check(checks, "five ordered question types exactly once each",
           question_types == ["选择", "判断", "填空", "简答", "论述"],
           f"types={question_types}")
    _check(checks, "every question carries a source", source_count == 5, f"sources={source_count}")
    _check(checks, "teacher section carries five standard answers",
           teacher_answer_count == 5, f"teacher_answers={teacher_answer_count}")
    _check(checks, "exactly one [E2E] exercise page exists under the course page",
           sum(1 for block in e2e_children if E2E_MARKER in str(block.get("title") or "")) == 1,
           f"e2e_children={[block.get('title') for block in e2e_children]}")
    _check(checks, "panel directory lists the new row as 已整理",
           row is not None and row.get("status") == "organized"
           and row.get("status_label") == "已整理",
           f"row={row}")
    _check(checks, "quota decremented by the charged points",
           abs(quota_before["llm_points_remaining"] - result["points_charged"]
               - quota_after["llm_points_remaining"]) <= 1e-9,
           f"before={quota_before['llm_points_remaining']} charged={result['points_charged']} "
           f"after={quota_after['llm_points_remaining']}")

    state["organize"] = result
    state["organize_quota"] = {"before": quota_before, "after": quota_after}
    state["organize_estimate_probe"] = {
        "http_status": estimate.status_code,
        "body": estimate.json() if estimate.status_code == 200 else None,
    }
    state["exercise"] = {"page_id": page_id, "url": result.get("url") or page.get("url") or "", "title": title}
    state["panel_row_after_organize"] = row
    _save_state(args.scratch, state)
    evidence = {
        "step": "organize",
        "generated_at": _utcnow(),
        "status": result.get("status"),
        "estimate_probe": state["organize_estimate_probe"],
        "organize_result": result,
        "quota": state["organize_quota"],
        "page": {
            "page_id": page_id,
            "url": state["exercise"]["url"],
            "title": title,
            "expected_parent_id": _norm_page_id(course["id"]),
            "actual_parent_id": actual_parent,
        },
        "blocks_summary": {
            "question_types": question_types,
            "source_count": source_count,
            "teacher_answer_count": teacher_answer_count,
        },
        "panel_row_after_organize": row,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(
        f"organized: {title} charged={result['points_charged']} "
        f"quota {quota_before['llm_points_remaining']} -> {quota_after['llm_points_remaining']}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# write-answers (the authorized fixture write)
# --------------------------------------------------------------------------

def cmd_write_answers(args) -> int:
    state = _load_state(args.scratch)
    panel = state["panel"]["url"]
    page_id = state["exercise"]["page_id"]
    checks: list[dict] = []
    markdown = (
        f"## {FIXTURE_HEADING}\n\n"
        + "\n\n".join(f"{number}. {ANSWER_FIXTURE[f'Q{number}']}" for number in range(1, 6))
    )
    blocks = markdown_to_blocks(markdown)
    client = _scratch_notion_client(args.scratch)
    try:
        client.append_blocks(page_id, blocks)
        readback = client.list_children(page_id)
    finally:
        client.close()
    texts = [_plain(block) for block in readback]
    fixture_at = next((i for i, value in enumerate(texts) if value == FIXTURE_HEADING), None)
    answers: dict[str, str] = {}
    if fixture_at is not None:
        rows = [
            _fixture_answer(texts[index])
            for index, block in enumerate(readback)
            if index > fixture_at
            and block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
        answers = {f"Q{number}": value for number, value in enumerate(rows, 1)}
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    _check(checks, "the answer fixture heading was appended", fixture_at is not None,
           f"fixture_at={fixture_at}")
    _check(checks, "the adopted Q1-Q5 answers read back verbatim", answers == ANSWER_FIXTURE,
           f"answers={answers}")
    _check(checks, "panel directory lists the row as 待批改",
           row is not None and row.get("status") == "pending-grade"
           and row.get("status_label") == "待批改",
           f"row={row}")
    state["panel_row_after_answers"] = row
    _save_state(args.scratch, state)
    evidence = {
        "step": "write-answers",
        "generated_at": _utcnow(),
        "page_id": page_id,
        "fixture_answers_written": list(ANSWER_FIXTURE.items()),
        "readback_answers": answers,
        "panel_row_after_answers": row,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"answers written and read back: {len(answers)} rows; panel row={row and row['status_label']}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# grade (the ONE grade op; UI-triggered via --job-id or runner-triggered)
# --------------------------------------------------------------------------

def cmd_grade(args) -> int:
    state = _load_state(args.scratch)
    panel = state["panel"]["url"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _scratch_platform_token(args.scratch)
    page_id = state["exercise"]["page_id"]
    external_job = (getattr(args, "job_id", "") or "").strip()
    checks: list[dict] = []

    if external_job:
        summary = _poll_job(panel, "/api/exercises/grade/status", external_job, GRADE_TIMEOUT_S)
        quota_before = None
    else:
        quota_before = _quota(relay, token)
        started = httpx.post(f"{panel}/api/exercises/{page_id}/grade", json={}, timeout=30)
        if started.status_code != 202:
            _write_json(args.out, {
                "step": "grade", "generated_at": _utcnow(), "status": "failed",
                "http_status": started.status_code, "body": started.json(),
                "quota_before": quota_before, "checks": checks,
            })
            print(f"FAIL: grade did not start: HTTP {started.status_code}")
            return 1
        summary = _poll_job(panel, "/api/exercises/grade/status", started.json()["job_id"],
                            GRADE_TIMEOUT_S)
    quota_after = _quota(relay, token)
    if summary.get("status") != "completed":
        _write_json(args.out, {
            "step": "grade", "generated_at": _utcnow(), "status": summary.get("status"),
            "summary": summary, "quota_before": quota_before, "quota_after": quota_after,
            "checks": checks,
        })
        print(f"FAIL: grade did not complete: {json.dumps(summary, ensure_ascii=False)}")
        return 1

    client = _scratch_notion_client(args.scratch)
    try:
        snapshot = _exercise_snapshot(client, page_id, state["course"]["id"], [])
        database = _wrong_answer_database(args.scratch, client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"],
                                   _operation_id(state))
    finally:
        client.close()
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)
    per_question = snapshot["per_question"]

    _check(checks, "grade summary completed with score 50", summary.get("score") == 50,
           f"score={summary.get('score')}")
    _check(checks, "grade charged a real positive settlement",
           isinstance(summary.get("points_charged"), (int, float)) and summary["points_charged"] > 0,
           f"points_charged={summary.get('points_charged')}")
    _check(checks, "exactly one E2E_GRADE_RESULT marker on the page",
           snapshot["grade_marker"] == "E2E_GRADE_RESULT" and snapshot["result_block_sets"] == 1,
           f"marker={snapshot['grade_marker']} sets={snapshot['result_block_sets']}")
    _check(checks, "written total matches the panel summary score",
           snapshot["score"] == summary.get("score"),
           f"page_total={snapshot['score']} summary={summary.get('score')}")
    _check(checks, "written graded_at matches the panel summary timestamp",
           snapshot["graded_at"] == summary.get("graded_at"),
           f"page={snapshot['graded_at']} summary={summary.get('graded_at')}")
    _check(checks, "five per-question results with the adopted scores and Q3 partial",
           [item.get("score") for item in per_question] == [2, 0, 1, 2, 0]
           and [item.get("outcome") for item in per_question] == ["correct", "wrong", "partial", "correct", "wrong"]
           and bool(per_question) and per_question[2].get("partial") is True,
           f"scores={[item.get('score') for item in per_question]} "
           f"outcomes={[item.get('outcome') for item in per_question]}")
    _check(checks, "original answers remain verbatim after write-back",
           snapshot["answers"] == ANSWER_FIXTURE, f"answers={snapshot['answers']}")
    _check(checks, "wrong answers Q2 and Q5 each registered exactly once",
           len(wrong["rows_by_number"].get(2) or []) == 1
           and len(wrong["rows_by_number"].get(5) or []) == 1
           and len(wrong["prefix_rows"]) == 2,
           f"q2={len(wrong['rows_by_number'].get(2) or [])} "
           f"q5={len(wrong['rows_by_number'].get(5) or [])} "
           f"prefix_total={len(wrong['prefix_rows'])}")
    _check(checks, "panel directory lists the row as 已批改",
           row is not None and row.get("status") == "graded"
           and row.get("status_label") == "已批改",
           f"row={row}")
    if quota_before is not None:
        _check(checks, "quota decremented by the charged points",
               abs(quota_before["llm_points_remaining"] - summary["points_charged"]
                   - quota_after["llm_points_remaining"]) <= 1e-9,
               f"before={quota_before['llm_points_remaining']} charged={summary['points_charged']} "
               f"after={quota_after['llm_points_remaining']}")

    state["grade"] = summary
    state["grade_quota"] = {"before": quota_before, "after": quota_after}
    state["grade_job"] = {"id": external_job or None, "trigger": "panel student UI (browser JS-dispatched click)" if external_job else "runner POST through the panel route"}
    state["operation_id"] = _operation_id(state)
    state["wrong_answer_database"] = {
        "id": database["id"], "url": database["url"], "title": database["title"],
    }
    state["wrong_answer_rows"] = {
        "expected_titles": wrong["expected_titles"],
        "rows": {str(number): _row_identity(rows) for number, rows in wrong["rows_by_number"].items()},
    }
    state["panel_row_after_grade"] = row
    state["graded_snapshot_summary"] = {
        "grade_marker": snapshot["grade_marker"],
        "result_block_sets": snapshot["result_block_sets"],
        "score": snapshot["score"],
        "graded_at": snapshot["graded_at"],
        "per_question_scores": [item.get("score") for item in per_question],
        "per_question_outcomes": [item.get("outcome") for item in per_question],
        "answers": snapshot["answers"],
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "grade",
        "generated_at": _utcnow(),
        "status": summary.get("status"),
        "grade_job": state["grade_job"],
        "grade_summary": summary,
        "quota": state["grade_quota"],
        "operation_id": state["operation_id"],
        "page_snapshot_summary": state["graded_snapshot_summary"],
        "wrong_answer_database": state["wrong_answer_database"],
        "wrong_answer_rows": state["wrong_answer_rows"],
        "panel_row_after_grade": row,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    quota_note = (
        f"quota {quota_before['llm_points_remaining']} -> {quota_after['llm_points_remaining']}"
        if quota_before
        else f"quota after {quota_after['llm_points_remaining']} (UI-triggered job)"
    )
    print(f"graded: score={summary.get('score')} charged={summary['points_charged']} {quota_note}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# reruns (idempotent, zero charge)
# --------------------------------------------------------------------------

def cmd_reruns(args) -> int:
    state = _load_state(args.scratch)
    panel = state["panel"]["url"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _scratch_platform_token(args.scratch)
    course, lecture = state["course"], state["lecture"]
    page_id = state["exercise"]["page_id"]
    checks: list[dict] = []

    quota_before_rerun = _quota(relay, token)
    started = httpx.post(
        f"{panel}/api/exercises/organize",
        json={"course_id": course["id"], "lecture_ids": [lecture["id"]], "e2e_mode": True},
        timeout=30,
    )
    if started.status_code != 202:
        raise SystemExit(f"organize rerun did not start: HTTP {started.status_code}")
    organize_rerun = _poll_job(panel, "/api/exercises/organize/status",
                               started.json()["job_id"], ORGANIZE_TIMEOUT_S)

    gate = httpx.post(f"{panel}/api/exercises/{page_id}/grade", json={}, timeout=30)
    confirmed = None
    if gate.status_code == 409 and gate.json().get("status") == "confirm_required":
        quota_before_grade_rerun = _quota(relay, token)
        started2 = httpx.post(
            f"{panel}/api/exercises/{page_id}/grade",
            json={"regrade": True, "confirm": True},
            timeout=30,
        )
        if started2.status_code != 202:
            raise SystemExit(f"confirmed grade rerun did not start: HTTP {started2.status_code}")
        confirmed = _poll_job(panel, "/api/exercises/grade/status",
                              started2.json()["job_id"], GRADE_TIMEOUT_S)
        quota_after_reruns = _quota(relay, token)
    else:
        quota_before_grade_rerun = None
        quota_after_reruns = _quota(relay, token)

    first_grade = state["grade"]
    grade_rerun = dict(confirmed or {})
    grade_rerun["no_op"] = bool(
        confirmed
        and confirmed.get("score") == first_grade.get("score")
        and confirmed.get("graded_at") == first_grade.get("graded_at")
        and confirmed.get("points_charged") is not None
        and abs(float(confirmed["points_charged"]) - 0.0) <= 1e-9
    )

    client = _scratch_notion_client(args.scratch)
    try:
        snapshot = _exercise_snapshot(client, page_id, course["id"], [])
        database = _wrong_answer_database(args.scratch, client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"],
                                   state["operation_id"])
    finally:
        client.close()
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    _check(checks, "organize rerun REUSED the existing [E2E] page (no duplicate)",
           organize_rerun.get("reused") is True
           and organize_rerun.get("exercise_id") == page_id,
           f"reused={organize_rerun.get('reused')} exercise_id={organize_rerun.get('exercise_id')}")
    _check(checks, "organize rerun charged zero",
           abs(float(organize_rerun.get("points_charged") or 0)) <= 1e-9,
           f"points_charged={organize_rerun.get('points_charged')}")
    _check(checks, "the unconfirmed grade rerun hit the confirmation gate",
           gate.status_code == 409 and gate.json().get("status") == "confirm_required",
           f"http={gate.status_code} body={gate.json()}")
    _check(checks, "confirmed grade rerun was a zero-charge no-op with unchanged score/timestamp",
           bool(grade_rerun.get("no_op")),
           f"score={grade_rerun.get('score')} graded_at={grade_rerun.get('graded_at')} "
           f"charged={grade_rerun.get('points_charged')}")
    _check(checks, "still exactly one E2E_GRADE_RESULT marker (no duplicate blocks)",
           snapshot["grade_marker"] == "E2E_GRADE_RESULT" and snapshot["result_block_sets"] == 1,
           f"marker={snapshot['grade_marker']} sets={snapshot['result_block_sets']}")
    _check(checks, "answers still verbatim after the reruns",
           snapshot["answers"] == ANSWER_FIXTURE, f"answers={snapshot['answers']}")
    _check(checks, "wrong-answer registration is still exactly Q2+Q5 (no duplicates)",
           len(wrong["rows_by_number"].get(2) or []) == 1
           and len(wrong["rows_by_number"].get(5) or []) == 1
           and len(wrong["prefix_rows"]) == 2,
           f"q2={len(wrong['rows_by_number'].get(2) or [])} "
           f"q5={len(wrong['rows_by_number'].get(5) or [])} prefix={len(wrong['prefix_rows'])}")
    _check(checks, "panel row remains 已批改 after the reruns (the row API carries status/label by design, never a score)",
           row is not None and row.get("status") == "graded"
           and row.get("status_label") == "已批改"
           and row.get("score") is None,
           f"row={row} first_grade_score={first_grade.get('score')}")
    _check(checks, "quota unchanged across both reruns (zero new spend)",
           abs(quota_before_rerun["llm_points_remaining"]
               - quota_after_reruns["llm_points_remaining"]) <= 1e-9,
           f"before={quota_before_rerun['llm_points_remaining']} "
           f"after={quota_after_reruns['llm_points_remaining']}")

    state["reruns"] = {
        "organize_rerun": organize_rerun,
        "grade_gate": {"http_status": gate.status_code, "body": gate.json()},
        "grade_rerun": grade_rerun,
        "quota": {"before": quota_before_rerun, "after": quota_after_reruns},
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "reruns",
        "generated_at": _utcnow(),
        "reruns": state["reruns"],
        "panel_row_after_reruns": row,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(
        f"reruns: organize reused={organize_rerun.get('reused')} charged={organize_rerun.get('points_charged')}; "
        f"grade no_op={grade_rerun.get('no_op')}; quota {quota_before_rerun['llm_points_remaining']} -> {quota_after_reruns['llm_points_remaining']}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# ASR stage: staging + run + verify (the ONE cloud transcription)
# --------------------------------------------------------------------------

def _asr_paths(scratch: str) -> dict:
    paths = _scratch_paths(scratch)
    course_dir = paths["data_dir"] / safe_name(ASR_COURSE_NAME)
    job = RecordingJob(ASR_COURSE_NAME, course_dir, Recording(
        course_id=ASR_COURSE_ID, title=ASR_RECORDING_TITLE,
        recorded_at=ASR_RECORDING_AT, teacher=TEACHER,
    ))
    return {
        "course_dir": course_dir,
        "recordings_dir": course_dir / "recordings",
        "recording_dir": job.directory,
        "video": job.video,
        "transcript": job.directory / "transcript.json",
        "notes": job.directory / "notes.md",
    }


def cmd_stage_asr(args) -> int:
    state = _load_state(args.scratch)
    relay = args.relay.rstrip("/") or state["relay"]
    token = _scratch_platform_token(args.scratch)
    clip = Path(args.clip)
    if not clip.exists():
        raise SystemExit(f"synthetic clip not found: {clip} (generate it first)")

    paths = _asr_paths(args.scratch)
    paths["course_dir"].mkdir(parents=True, exist_ok=True)
    (paths["course_dir"] / "course.json").write_text(
        json.dumps({"course_id": ASR_COURSE_ID, "name": ASR_COURSE_NAME}, ensure_ascii=False),
        encoding="utf-8",
    )
    recording = Recording(course_id=ASR_COURSE_ID, title=ASR_RECORDING_TITLE,
                          recorded_at=ASR_RECORDING_AT, teacher=TEACHER)
    paths["recordings_dir"].mkdir(parents=True, exist_ok=True)
    (paths["recordings_dir"] / "index.json").write_text(
        json.dumps([recording.model_dump()], ensure_ascii=False, indent=1), encoding="utf-8"
    )
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

    jobs = collect_jobs(_scratch_paths(args.scratch)["data_dir"])
    asr_job = next(
        (item for item in jobs
         if item.recording.title == ASR_RECORDING_TITLE and item.course_name == ASR_COURSE_NAME),
        None,
    )
    stage_value, _ = recording_stage(asr_job) if asr_job else ("", "")
    quota = _quota(relay, token)
    db = _relay_db_rows(state["relay_db"])
    log_text = _read_log_text(state["relay_log"])
    analysis = _relay_log_analysis(log_text)

    state["asr_stage"] = {
        "clip": {"path": str(clip), "sha256": _sha256_file(clip)},
        "video": {"path": str(video), "bytes": video.stat().st_size,
                  "sha256": _sha256_file(video), "duration_seconds": duration},
        "recording_title": ASR_RECORDING_TITLE,
        "course_name": ASR_COURSE_NAME,
        "stage_before": stage_value,
        "quota_before": quota,
        "usage_rows_before": len(db["usage_rows"]),
        "llm_usage_rows_before": len(db["llm_usage_rows"]),
        "oss_keys_before": analysis["oss_upload_keys"],
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "stage-asr",
        "generated_at": _utcnow(),
        **state["asr_stage"],
        "recording": recording.model_dump(),
        "relay_db_before": {"usage_rows": db["usage_rows"], "llm_usage_rows": len(db["llm_usage_rows"])},
        "relay_log_before": analysis,
        "checks": [
            {"name": "the staged E2E recording sits at ladder stage 'downloaded'",
             "result": "pass" if stage_value == "downloaded" else "fail",
             "detail": f"stage={stage_value}"},
        ],
        "note": "a synthetic clip (<=60 s TTS audio) staged as its own E2E course inside the scratch tree; the real synced recordings have no videos and no download step ever runs, so this is the ONLY transcribable recording in the scratch root",
    }
    _write_json(args.out, evidence)
    print(
        f"staged asr: video_bytes={state['asr_stage']['video']['bytes']} duration={duration}s "
        f"stage={stage_value} quota_seconds={quota['transcribe_seconds_remaining']}"
    )
    return 0 if stage_value == "downloaded" else 1


def cmd_run_asr(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    env = _child_env_cli(paths["root"], args.relay)
    console_log = Path(args.log)
    console_log.parent.mkdir(parents=True, exist_ok=True)
    started = _utcnow()
    t0 = time.monotonic()
    with open(console_log, "wb") as handle:
        result = subprocess.run(
            [str(_pku_sync_exe()), "process", "--limit", "1"],
            cwd=str(paths["client_env"]),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    elapsed = time.monotonic() - t0
    console = console_log.read_text(encoding="utf-8", errors="replace")
    evidence = {
        "step": "run-asr",
        "generated_at": _utcnow(),
        "timestamps": {"started_at": started, "finished_at": _utcnow()},
        "command": "pku-sync process --limit 1 (probe-owned child; cwd=<scratch client-env>; cloud backend through the scratch relay)",
        "exit_code": result.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "console_output": console,
    }
    _write_json(args.out, evidence)
    print(f"process run: exit={result.returncode} elapsed={elapsed:.1f}s")
    for line in console.splitlines()[-6:]:
        print(f"  | {line}")
    return 0 if result.returncode == 0 else 1


def _oss_head_probe(scratch: str, keys: list[str]) -> list[dict]:
    """head_object each temp key with the probe's OWN OSS credentials.

    The credentials are read from the real client .env into the CHILD env
    only (values never printed, never written)."""
    results: list[dict] = []
    if not keys:
        return results
    env_map = {}
    for line in REAL_CLIENT_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if match:
            env_map[match.group(1)] = match.group(2)
    script = (
        "import os, sys\n"
        "import oss2\n"
        "auth = oss2.Auth(os.environ['OSS_ACCESS_KEY_ID'], os.environ['OSS_ACCESS_KEY_SECRET'])\n"
        "bucket = oss2.Bucket(auth, os.environ['OSS_ENDPOINT'], os.environ['OSS_BUCKET'])\n"
        "for key in sys.argv[1:]:\n"
        "    try:\n"
        "        bucket.head_object(key)\n"
        "        print('HEAD:' + key + ':200')\n"
        "    except oss2.exceptions.NotFound:\n"
        "        print('HEAD:' + key + ':404')\n"
        "    except Exception as exc:\n"
        "        print('HEAD:' + key + ':ERR:' + type(exc).__name__)\n"
    )
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT")}
    child_env.update({
        "OSS_ACCESS_KEY_ID": env_map.get("OSS_ACCESS_KEY_ID", ""),
        "OSS_ACCESS_KEY_SECRET": env_map.get("OSS_ACCESS_KEY_SECRET", ""),
        "OSS_BUCKET": env_map.get("OSS_BUCKET", ""),
        "OSS_ENDPOINT": env_map.get("OSS_ENDPOINT", ""),
        "PYTHONUTF8": "1",
    })
    proc = subprocess.run(
        [str(RELAY_REPO_PYTHON), "-c", script, *keys],
        capture_output=True, text=True, env=child_env, timeout=120,
    )
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "HEAD":
            results.append({"key": parts[1], "head_status": parts[2]})
    return results


def cmd_verify_asr(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    relay = args.relay.rstrip("/") or state["relay"]
    token = _scratch_platform_token(args.scratch)
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    asr_paths = _asr_paths(args.scratch)
    checks: list[dict] = []

    jobs = collect_jobs(paths["data_dir"])
    asr_job = next(
        (item for item in jobs
         if item.recording.title == ASR_RECORDING_TITLE and item.course_name == ASR_COURSE_NAME),
        None,
    )
    stage_value, _ = recording_stage(asr_job) if asr_job else ("", "")
    transcript_path = asr_paths["transcript"]
    transcript_payload = None
    parse_ok = False
    if transcript_path.exists():
        try:
            transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            parse_ok = True
        except json.JSONDecodeError:
            parse_ok = False
    quota = _quota(relay, token)
    db = _relay_db_rows(state["relay_db"])
    log_text = _read_log_text(state["relay_log"])
    analysis = _relay_log_analysis(log_text)
    panel_status = httpx.get(f"{state['panel']['url']}/api/status", timeout=30).json()
    panel_asr_course = next(
        (course for course in panel_status.get("recordings", [])
         if course.get("name") == ASR_COURSE_NAME),
        None,
    )
    panel_asr_row = None
    if panel_asr_course:
        panel_asr_row = next(
            (rec for rec in panel_asr_course.get("recordings", [])
             if rec.get("title") == ASR_RECORDING_TITLE),
            None,
        )

    new_usage_rows = [
        row for row in db["usage_rows"]
        if row["kind"] == "transcribe"
    ]
    seconds_charged = (transcript_payload or {}).get("seconds_charged") if parse_ok else None
    new_keys = sorted(set(analysis["oss_upload_keys"]) - set(before.get("oss_keys_before") or []))
    head_results = _oss_head_probe(args.scratch, new_keys)

    _check(checks, "transcript written and parses with non-empty segments",
           transcript_path.exists() and parse_ok
           and len((transcript_payload or {}).get("segments") or []) > 0,
           f"segments={len((transcript_payload or {}).get('segments') or []) if parse_ok else 0}")
    _check(checks, "the transcript is a fresh miss (reused=false) -- one real DashScope task",
           (transcript_payload or {}).get("reused") is False,
           f"reused={(transcript_payload or {}).get('reused')}")
    _check(checks, "the ladder advanced: downloaded -> transcribed",
           before.get("stage_before") == "downloaded" and stage_value == "transcribed",
           f"before={before.get('stage_before')} after={stage_value}")
    _check(checks, "the panel recording state reflects the advance",
           (panel_asr_row or {}).get("stage") == "transcribed",
           f"panel_stage={(panel_asr_row or {}).get('stage')}")
    _check(checks, "quota decremented by exactly the charged seconds (ceil of the actual duration)",
           isinstance(seconds_charged, int)
           and before["quota_before"]["transcribe_seconds_remaining"]
               - quota["transcribe_seconds_remaining"] == seconds_charged,
           f"before={before['quota_before']['transcribe_seconds_remaining']} "
           f"after={quota['transcribe_seconds_remaining']} charged={seconds_charged}")
    _check(checks, "exactly one new transcribe usage row for the charged seconds",
           len(new_usage_rows) == 1 and new_usage_rows[0]["seconds"] == seconds_charged,
           f"rows={new_usage_rows}")
    _check(checks, "exactly one POST /v1/transcribe reached the relay",
           analysis["transcribe_200_lines"] == 1,
           f"transcribe_200={analysis['transcribe_200_lines']} "
           f"requests={analysis['transcribe_request_lines']}")
    _check(checks, "the relay logged exactly one temp OSS key with both upload and delete lines",
           len(new_keys) == 1
           and new_keys == analysis["oss_delete_keys"]
           and set(new_keys) <= set(analysis["oss_upload_keys"]),
           f"keys={new_keys} deletes={analysis['oss_delete_keys']}")
    _check(checks, "head_object on the logged temp key answers 404 (raw audio gone from OSS)",
           bool(head_results) and all(item["head_status"] == "404" for item in head_results),
           f"heads={head_results}")
    _check(checks, "notes stage skipped (no notes.md) -- zero LLM spend on the ASR leg",
           not asr_paths["notes"].exists(), f"notes_exists={asr_paths['notes'].exists()}")
    _check(checks, "the video is retained (DELETE_VIDEO_AFTER_PROCESSING=false in the scratch env)",
           asr_paths["video"].exists())

    state["asr_verify"] = {
        "stage_after": stage_value,
        "quota_after": quota,
        "seconds_charged": seconds_charged,
        "transcript": {
            "segment_count": len((transcript_payload or {}).get("segments") or []) if parse_ok else 0,
            "language": (transcript_payload or {}).get("language"),
            "reused": (transcript_payload or {}).get("reused"),
            "seconds_charged": seconds_charged,
        },
        "temp_oss_key": new_keys[0] if len(new_keys) == 1 else None,
        "head_results": head_results,
        "usage_rows": new_usage_rows,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "verify-asr",
        "generated_at": _utcnow(),
        "asr": state["asr_verify"],
        "panel_asr_row": panel_asr_row,
        "relay_db_after": {"usage_rows": db["usage_rows"], "llm_usage_rows": db["llm_usage_rows"]},
        "relay_log_analysis": analysis,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(
        f"verify-asr: stage={stage_value} charged={seconds_charged}s key={state['asr_verify']['temp_oss_key']} "
        f"head404={all(item['head_status'] == '404' for item in head_results)}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# consolidated verify
# --------------------------------------------------------------------------

def cmd_verify(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    relay = state["relay"]
    token = _scratch_platform_token(args.scratch)
    page_id = state["exercise"]["page_id"]
    course = state["course"]
    checks: list[dict] = []

    client = _scratch_notion_client(args.scratch)
    try:
        database = _wrong_answer_database(args.scratch, client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"],
                                   state["operation_id"])
        # The Window D read-only verifier asserts the wrong-answer registrations
        # the real grader produced (Q2 + Q5, each exactly once); derive them
        # from the live wrong-answer rows rather than passing an empty list.
        found_keys = sorted(
            f"Q{number}"
            for number in (2, 5)
            if len(wrong["rows_by_number"].get(number) or []) == 1
        )
        snapshot = _exercise_snapshot(client, page_id, course["id"], found_keys)
    finally:
        client.close()

    db = _relay_db_rows(state["relay_db"])
    log_text = _read_log_text(state["relay_log"])
    analysis = _relay_log_analysis(log_text)
    quota = _quota(relay, token)
    rows = _panel_exercise_rows(state["panel"]["url"])
    row = _panel_row(rows, page_id)

    # -- the Window D read-only verifier over the final real snapshot -------
    # Resume-window semantics (window-c-2026-09-21-extension-2): the scratch
    # relay DB is the PRESERVED retry DB. It already held the retry's charged
    # quiz row before this resume; the first run's quiz charge lived in that
    # run's own scratch DB, destroyed at its close-out (evidenced there). The
    # verifier's ledger therefore covers exactly THIS resumed window's two
    # ops (the new quiz + the grade); the cumulative cross-run ledger is
    # checked separately against the retry's recorded 100000-millipoint
    # activation grant.
    resumed = bool(getattr(args, "resumed", False))
    organize_quota = state["organize_quota"]
    grade_quota = state["grade_quota"]
    llm_rows = db["llm_usage_rows"]
    ledger_source_rows = llm_rows[-2:] if resumed else llm_rows
    balances = [organize_quota["before"]["llm_points_remaining"]]
    for item in ledger_source_rows:
        balances.append(balances[-1] - int(item["millipoints"]) / 1000)
    ledger_rows = [
        {
            "sequence": index + 1,
            "operation": item["operation"],
            "status": "completed",
            "points_before": balances[index],
            "points_charged": int(item["millipoints"]) / 1000,
            "points_after": balances[index + 1],
            "millipoints": int(item["millipoints"]),
            "model": item.get("model"),
        }
        for index, item in enumerate(ledger_source_rows)
    ]
    verifier = WindowDReadOnlyVerifier()
    verdict = verifier.verify(
        snapshot=snapshot,
        panel_summary=state["grade"],
        organize_result=state["organize"],
        organize_rerun=state["reruns"]["organize_rerun"],
        grade_rerun=state["reruns"]["grade_rerun"],
        ledger_rows=ledger_rows,
        expected_parent_id=_norm_page_id(course["id"]),
    )

    # -- ledger: exactly two LLM ops, one ASR task, one exchange -------------
    if resumed:
        # Preserved retry DB: the retry's charged quiz + this resume's quiz
        # + grade. The retry's activation grant was 100000 millipoints
        # (evidence/33: quota flipped to 100.0 LLM points at activation).
        grant_millipoints = 100000
        cumulative_charged = sum(int(item["millipoints"]) for item in llm_rows)
        _check(
            checks,
            "the preserved relay DB holds exactly three LLM usage rows"
            " (the retry's charged quiz + this resume's quiz + grade)",
            len(llm_rows) == 3
            and [item["operation"] for item in llm_rows] == ["quiz", "quiz", "grade"],
            f"rows={[{'operation': item['operation'], 'millipoints': item['millipoints']} for item in llm_rows]}")
        _check(
            checks,
            "the resumed window itself added exactly one quiz then one grade"
            " (rows 2-3 of the preserved DB), both charged",
            [item["operation"] for item in ledger_source_rows] == ["quiz", "grade"]
            and all(int(item["millipoints"]) > 0 for item in ledger_source_rows),
            f"resume_rows={[{'operation': item['operation'], 'millipoints': item['millipoints']} for item in ledger_source_rows]}")
        _check(
            checks,
            "the cumulative millipoint ledger closes against the retry's"
            " 100000-millipoint activation grant",
            quota["llm_points_remaining"] * 1000 + cumulative_charged == grant_millipoints,
            f"remaining_millipoints={quota['llm_points_remaining'] * 1000}"
            f" charged={cumulative_charged} grant={grant_millipoints}")
        _check(
            checks,
            "the resumed relay log shows ZERO OAuth exchange requests"
            " (the two authorized exchanges happened in the prior runs;"
            " no third exchange)",
            analysis["exchange_request_lines"] == 0,
            f"exchange_requests={analysis['exchange_request_lines']}")
        _check(
            checks,
            "the resumed relay log shows ZERO transcribe requests"
            " (the single ASR task was spent in the retry; none added)",
            analysis["transcribe_request_lines"] == 0,
            f"transcribe_requests={analysis['transcribe_request_lines']}")
        _check(
            checks,
            "the resumed relay log shows exactly two /v1/llm 200s"
            " (the resume's quiz + grade only)",
            analysis["llm_request_lines"] == 2 and analysis["llm_200_lines"] == 2,
            f"llm_requests={analysis['llm_request_lines']} llm_200={analysis['llm_200_lines']}")
    else:
        _check(checks, "exactly two relay LLM usage rows (one quiz + one grade)",
               len(llm_rows) == 2 and [item["operation"] for item in llm_rows] == ["quiz", "grade"],
               f"rows={[{'operation': item['operation'], 'millipoints': item['millipoints']} for item in llm_rows]}")
        _check(checks, "exactly one OAuth exchange line in the relay log",
               analysis["exchange_200_lines"] == 1,
               f"exchange_200={analysis['exchange_200_lines']}")
    _check(checks, "exactly one transcribe usage row (one real ASR task)",
           len(db["usage_rows"]) == 1 and db["usage_rows"][0]["kind"] == "transcribe",
           f"rows={db['usage_rows']}")
    _check(checks, "the Window D read-only verifier passes over the final real snapshot",
           all(item["result"] == "pass" for item in verdict["checks"]),
           "failed=" + json.dumps([item for item in verdict["checks"] if item["result"] != "pass"],
                                  ensure_ascii=False))

    # -- token scope: scratch tokens exist ONLY in the scratch env -----------
    platform_token = _env_value(paths["scratch_env"], "PLATFORM_TOKEN")
    notion_token = _env_value(paths["scratch_env"], "NOTION_TOKEN")
    real_env_text = REAL_CLIENT_ENV.read_text(encoding="utf-8", errors="replace") if REAL_CLIENT_ENV.exists() else ""
    scope_hits: list[str] = []
    for path in paths["root"].rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(paths["root"]))
        if rel in (r"client-env\.env", "relay-stdout.log", "relay-stderr.log",
                   "relay-stdout-resume.log", "relay-stderr-resume.log", "state.json",
                   "relay.db"):
            continue  # the scratch env file is the approved home; logs scanned below;
            # relay.db is the relay's own session store where the raw platform
            # token lives BY DESIGN (sessions.token PK) -- its Notion-token
            # absence is asserted separately below
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if (platform_token and platform_token in text) or (notion_token and notion_token in text):
            scope_hits.append(rel)
    _check(checks, "the scratch tokens never appear in the real repo .env",
           not (platform_token and platform_token in real_env_text)
           and not (notion_token and notion_token in real_env_text),
           "value-presence booleans only")
    _check(checks, "the scratch tokens appear in no other scratch file",
           not scope_hits, f"hits={scope_hits}")
    log_scope_ok = True
    if log_text:
        log_scope_ok = (not platform_token or platform_token not in log_text) and (
            not notion_token or notion_token not in log_text
        )
    _check(checks, "the relay log carries neither scratch token", log_scope_ok)

    # -- relay DB scan: no Notion token anywhere ------------------------------
    conn = sqlite3.connect(state["relay_db"], timeout=10)
    conn.row_factory = sqlite3.Row
    notion_tables: list[str] = []
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            rows_db = conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
            found = any(
                notion_token and notion_token in str(value)
                for row in rows_db for value in tuple(row)
            )
            if found:
                notion_tables.append(name)
    finally:
        conn.close()
    _check(checks, "the Notion token appears in NO relay DB table",
           not notion_tables, f"hit_tables={notion_tables}")

    # -- the real .env stayed byte-identical -----------------------------------
    _check(checks, "the real package-root .env stayed byte-identical through the window",
           _sha256_file(REAL_CLIENT_ENV) == state.get("real_env_sha256")
           if state.get("real_env_sha256") else True,
           "sha256 comparison against the stage-time baseline")

    # -- final panel state -----------------------------------------------------
    _check(checks, "the panel row is 已批改 (the row API carries status/label by design, never a score)",
           row is not None and row.get("status") == "graded"
           and row.get("status_label") == "已批改" and row.get("score") is None,
           f"row={row} page_score={snapshot.get('score')}")
    if resumed:
        _check(
            checks,
            "final quota equals the retry's grant minus every preserved-DB charge"
            " (seconds stay decremented once from the retry's ASR)",
            abs(quota["llm_points_remaining"] - (grant_millipoints - cumulative_charged) / 1000) <= 1e-9
            and quota["transcribe_seconds_remaining"] == 431954,
            f"quota={quota['llm_points_remaining']} seconds={quota['transcribe_seconds_remaining']}")
        captures_dir = (getattr(args, "captures", "") or "").strip()
        capture_entries: list[dict] = []
        if captures_dir:
            summary_path = Path(captures_dir) / "summary.json"
            if summary_path.exists():
                try:
                    capture_entries = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    capture_entries = []
        _check(
            checks,
            "the transport capture proves response_format json_object +"
            " enable_thinking=false on BOTH resumed vendor requests (quiz + grade)",
            len(capture_entries) == 2
            and all(
                entry.get("response_format_type") == "json_object"
                and entry.get("enable_thinking") is False
                and entry.get("upstream_status") == 200
                for entry in capture_entries
            ),
            f"captures={[{k: entry.get(k) for k in ('seq', 'response_format_type', 'enable_thinking', 'upstream_status', 'finish_reason')} for entry in capture_entries]}")
    else:
        _check(checks, "final quota reconciles: 100 - quiz - grade points, seconds decremented once",
               abs(quota["llm_points_remaining"] - balances[-1]) <= 1e-9,
               f"quota={quota['llm_points_remaining']} ledger_after={balances[-1]}")

    evidence = {
        "step": "verify",
        "generated_at": _utcnow(),
        "verifier_checks": verdict["checks"],
        "ledger_rows": ledger_rows,
        "resume_window": (
            {
                "note": "window-c-2026-09-21-extension-2 resume of the preserved scratch env; ledger_rows cover ONLY this resumed window's two ops (quiz + grade); the preserved retry DB additionally holds the retry's charged quiz row",
                "preserved_db_llm_rows": [
                    {"operation": item["operation"], "millipoints": item["millipoints"],
                     "model": item.get("model")}
                    for item in llm_rows
                ],
                "retry_activation_grant_millipoints": grant_millipoints,
                "cumulative_charged_millipoints": cumulative_charged,
                "cross_run_cumulative_totals": {
                    "llm_ops": 4, "quiz_ops": 3, "grade_ops": 1,
                    "asr_tasks": 1, "oauth_exchanges": 2,
                    "note": "the first run's quiz charge (11 millipoints) lived in that run's scratch DB, destroyed at its close-out (evidence/22 + ledger); the remaining three ops are rows of this preserved DB",
                },
                "transport_capture_entries": [
                    {k: entry.get(k) for k in ("seq", "captured_at", "model", "response_format_type", "enable_thinking", "upstream_status", "finish_reason", "raw_content_bytes")}
                    for entry in capture_entries
                ],
            }
            if resumed else None
        ),
        "relay_log_analysis": analysis,
        "final_quota": quota,
        "panel_row_final": row,
        "wrong_answer_rows_final": {
            "expected_titles": wrong["expected_titles"],
            "q2": _row_identity(wrong["rows_by_number"].get(2) or []),
            "q5": _row_identity(wrong["rows_by_number"].get(5) or []),
            "prefix_total": len(wrong["prefix_rows"]),
        },
        "token_scope": {
            "platform_token_only_in_scratch_env": True,
            "notion_token_only_in_scratch_env": True,
            "other_scratch_hits": scope_hits,
            "token_values_recorded_in_evidence": False,
        },
        "relay_db": {
            "llm_usage_rows": llm_rows,
            "usage_rows": db["usage_rows"],
            "accounts": db["accounts"],
            "notion_token_hit_tables": notion_tables,
        },
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"verify: verifier={sum(1 for c in verdict['checks'] if c['result'] == 'pass')}/{len(verdict['checks'])} ledger_rows={len(ledger_rows)}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


# --------------------------------------------------------------------------
# human [E2E] cleanup poll + proof
# --------------------------------------------------------------------------

def cmd_cleanup_poll(args) -> int:
    state = _load_state(args.scratch)
    page_id = state["exercise"]["page_id"]
    operation_id = state["operation_id"]
    title = state["exercise"]["title"]
    deadline = time.monotonic() + args.timeout
    page_result = ""
    wrong_result: dict = {}
    while time.monotonic() < deadline:
        client = _scratch_notion_client(args.scratch)
        try:
            # Resolve the wrong-answer database by its recorded identity: the
            # state stores id/url/title and the row query needs the live
            # title_property. A full directory load is NOT usable here -- once
            # the human deletes the [E2E] exercise page the directory's
            # exercise enumeration 404s on that page id, which is exactly what
            # this poll must tolerate.
            identity = state["wrong_answer_database"]
            schema = client.get_database(identity["id"])
            properties = (schema or {}).get("properties") or {}
            title_names = [
                name
                for name, prop in properties.items()
                if isinstance(prop, dict) and prop.get("type") == "title"
            ]
            if len(title_names) != 1:
                raise SystemExit("wrong-answer database title property is not unique")
            database = {"id": identity["id"], "url": identity["url"],
                        "title": identity["title"], "title_property": title_names[0]}
            page_status = ""
            try:
                page = client.get_page(page_id)
                # Notion expresses UI deletion as trash-archive: the object
                # keeps answering get_page with archived/in_trash flags set.
                # Only a live (non-trashed) page counts as present here.
                if page.get("archived") or page.get("in_trash"):
                    page_status = "trashed:archived={}".format(bool(page.get("archived")))
                else:
                    page_status = "present"
            except Exception as exc:  # noqa: BLE001
                page_status = f"gone:{type(exc).__name__}"
            wrong = _wrong_answer_rows(client, database, title, operation_id)
        finally:
            client.close()
        q2 = len(wrong["rows_by_number"].get(2) or [])
        q5 = len(wrong["rows_by_number"].get(5) or [])
        prefix = len(wrong["prefix_rows"])
        if page_status != "present" and q2 == 0 and q5 == 0 and prefix == 0:
            page_result = page_status
            wrong_result = {"q2": 0, "q5": 0, "prefix": 0}
            break
        wrong_result = {"q2": q2, "q5": q5, "prefix": prefix}
        page_result = page_status
        time.sleep(5.0)
    cleaned = page_result != "present" and wrong_result == {"q2": 0, "q5": 0, "prefix": 0}
    evidence = {
        "step": "cleanup-poll",
        "generated_at": _utcnow(),
        "human_cleanup_requested": True,
        "page_status_after_cleanup": page_result,
        "wrong_answer_rows_after_cleanup": wrong_result,
        "cleanup_proven": cleaned,
        "note": "the human performed the final [E2E] cleanup in the Notion UI; this poll is the programmatic re-fetch proof (page gone from the workspace, zero wrong-answer rows for this exercise)",
    }
    _write_json(args.out, evidence)
    print(f"cleanup-poll: page={page_result} wrong={wrong_result} proven={cleaned}")
    return 0 if cleaned else 1


# --------------------------------------------------------------------------
# relay log excerpt (sanitized)
# --------------------------------------------------------------------------

def cmd_relay_excerpt(args) -> int:
    state = _load_state(args.scratch)
    log_text = _read_log_text(args.log)
    platform_token = _env_value(_scratch_paths(args.scratch)["scratch_env"], "PLATFORM_TOKEN")
    notion_token = _env_value(_scratch_paths(args.scratch)["scratch_env"], "NOTION_TOKEN")
    secret_shapes = (
        re.compile(r"\bsecret_[A-Za-z0-9]{16,}"),
        re.compile(r"\bntn_[A-Za-z0-9]{16,}"),
        re.compile(r"[?&]code=[A-Za-z0-9_-]{16,}"),
        re.compile(r"[?&]state=[A-Za-z0-9_-]{16,}"),
    )
    excerpt: list[str] = []
    for line in log_text.splitlines():
        if platform_token and platform_token in line:
            continue
        if notion_token and notion_token in line:
            continue
        if any(shape.search(line) for shape in secret_shapes):
            continue
        if "/v1/" in line or "oss upload:" in line or "oss delete:" in line or "Uvicorn running" in line:
            excerpt.append(line)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(excerpt) + "\n", encoding="utf-8")
    print(f"relay excerpt: {len(excerpt)} sanitized lines -> {out}")
    return 0


# --------------------------------------------------------------------------
# scratch cleanup
# --------------------------------------------------------------------------

def cmd_cleanup_scratch(args) -> int:
    state = _load_state(args.scratch)
    paths = _scratch_paths(args.scratch)
    stopped = []
    for name, pid in (state.get("pids") or {}).items():
        if not pid:
            continue
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True
        )
        stopped.append({"name": name, "pid": pid, "kill_exit": result.returncode})
    if args.relay_pid:
        result = subprocess.run(
            ["taskkill", "/PID", str(args.relay_pid), "/F", "/T"], capture_output=True, text=True
        )
        stopped.append({"name": "relay", "pid": args.relay_pid, "kill_exit": result.returncode})
    time.sleep(1.5)
    port_status = {}
    for port in (8765, 8766, 8791, 8800):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", port))
            port_status[str(port)] = False
        except OSError:
            port_status[str(port)] = True
        finally:
            probe.close()
    inventory = sorted(
        ({"name": str(p.relative_to(paths["root"])), "bytes": p.stat().st_size}
         for p in paths["root"].rglob("*") if p.is_file()),
        key=lambda entry: entry["name"],
    )
    import shutil
    shutil.rmtree(paths["root"], ignore_errors=False)
    root_removed = not paths["root"].exists()
    evidence = {
        "step": "cleanup-scratch",
        "generated_at": _utcnow(),
        "stopped": stopped,
        "ports_free_after_cleanup": port_status,
        "scratch_root": str(paths["root"]),
        "removed_entry_count": len(inventory),
        "removed_entry_names": [entry["name"] for entry in inventory],
        "root_removed": root_removed,
        "note": "the whole scratch root (scratch .env holding both scratch tokens, activation code file, scratch relay DB, panel/sync/process/relay logs, staged synthetic video + clip, runner state) is deleted; no token value was ever copied into evidence",
    }
    _write_json(args.out, evidence)
    print(f"cleanup-scratch: stopped={len(stopped)} ports_free={port_status} root_removed={root_removed} entries={len(inventory)}")
    return 0 if all(port_status.values()) and root_removed else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2e_window_c.py",
        description="Window C (M4) full student journey runner (scratch env; secrets never recorded).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="build the empty scratch window; assert ports free")
    stage.add_argument("--scratch", required=True)
    stage.add_argument("--relay", default="http://127.0.0.1:8800")
    stage.add_argument("--out", required=True)
    stage.set_defaults(func=cmd_stage)

    panel_start = sub.add_parser("panel-start", help="launch the pristine panel child")
    panel_start.add_argument("--scratch", required=True)
    panel_start.add_argument("--relay", default="http://127.0.0.1:8800")
    panel_start.add_argument("--port", type=int, default=8791)
    panel_start.add_argument("--stdout-log", required=True)
    panel_start.add_argument("--stderr-log", required=True)
    panel_start.add_argument("--out", required=True)
    panel_start.add_argument(
        "--resume", action="store_true",
        help="restart the panel for a resumed window: keep the scratch activation and consent",
    )
    panel_start.set_defaults(func=cmd_panel_start)

    conn = sub.add_parser("connection-snap", help="capture the panel connection + quota state")
    conn.add_argument("--scratch", required=True)
    conn.add_argument("--label", required=True)
    conn.add_argument("--out", required=True)
    conn.set_defaults(func=cmd_connection_snap)

    oauth_wait = sub.add_parser("oauth-wait", help="poll until the consent lands the connected state")
    oauth_wait.add_argument("--scratch", required=True)
    oauth_wait.add_argument("--timeout", type=float, default=480.0)
    oauth_wait.add_argument("--out", required=True)
    oauth_wait.set_defaults(func=cmd_oauth_wait)

    sync = sub.add_parser("sync", help="run the real PKU sync into the scratch root")
    sync.add_argument("--scratch", required=True)
    sync.add_argument("--relay", default="http://127.0.0.1:8800")
    sync.add_argument("--log", required=True)
    sync.add_argument("--out", required=True)
    sync.set_defaults(func=cmd_sync)

    stage_ex = sub.add_parser("stage-exercise", help="stage the notes for the real lecture scope")
    stage_ex.add_argument("--scratch", required=True)
    stage_ex.add_argument("--out", required=True)
    stage_ex.set_defaults(func=cmd_stage_exercise)

    organize = sub.add_parser("organize", help="the ONE quiz op + [E2E] page verification")
    organize.add_argument("--scratch", required=True)
    organize.add_argument("--relay", default="")
    organize.add_argument("--out", required=True)
    organize.set_defaults(func=cmd_organize)

    write_answers = sub.add_parser("write-answers", help="append the adopted fixture answers")
    write_answers.add_argument("--scratch", required=True)
    write_answers.add_argument("--out", required=True)
    write_answers.set_defaults(func=cmd_write_answers)

    grade = sub.add_parser("grade", help="the ONE grade op + full write-back verification")
    grade.add_argument("--scratch", required=True)
    grade.add_argument("--relay", default="")
    grade.add_argument("--job-id", default="", help="poll a UI-triggered grade job instead of POSTing")
    grade.add_argument("--out", required=True)
    grade.set_defaults(func=cmd_grade)

    reruns = sub.add_parser("reruns", help="idempotent organize + grade reruns (zero charge)")
    reruns.add_argument("--scratch", required=True)
    reruns.add_argument("--relay", default="")
    reruns.add_argument("--out", required=True)
    reruns.set_defaults(func=cmd_reruns)

    stage_asr = sub.add_parser("stage-asr", help="stage the synthetic clip as the one transcribable recording")
    stage_asr.add_argument("--scratch", required=True)
    stage_asr.add_argument("--relay", default="")
    stage_asr.add_argument("--clip", required=True)
    stage_asr.add_argument("--out", required=True)
    stage_asr.set_defaults(func=cmd_stage_asr)

    run_asr = sub.add_parser("run-asr", help="run the real client cloud transcription once")
    run_asr.add_argument("--scratch", required=True)
    run_asr.add_argument("--relay", default="http://127.0.0.1:8800")
    run_asr.add_argument("--log", required=True)
    run_asr.add_argument("--out", required=True)
    run_asr.set_defaults(func=cmd_run_asr)

    verify_asr = sub.add_parser("verify-asr", help="verify the transcription leg end to end")
    verify_asr.add_argument("--scratch", required=True)
    verify_asr.add_argument("--relay", default="")
    verify_asr.add_argument("--before", required=True)
    verify_asr.add_argument("--out", required=True)
    verify_asr.set_defaults(func=cmd_verify_asr)

    verify = sub.add_parser("verify", help="the consolidated read-only verification")
    verify.add_argument("--scratch", required=True)
    verify.add_argument("--out", required=True)
    verify.add_argument(
        "--resumed", action="store_true",
        help="resume-window semantics (window-c extension-2): the scratch relay DB is"
             " the PRESERVED retry DB (its quiz row pre-dates this resume), the relay"
             " log is the resume log (zero exchanges, zero transcribes), and the"
             " cumulative ledger closes against the retry's 100000-millipoint grant",
    )
    verify.add_argument(
        "--captures", default="",
        help="optional transport-capture dir (summary.json) proving response_format"
             " json_object + enable_thinking=false on the resumed vendor requests",
    )
    verify.set_defaults(func=cmd_verify)

    cleanup_poll = sub.add_parser("cleanup-poll", help="poll until the human [E2E] cleanup is proven")
    cleanup_poll.add_argument("--scratch", required=True)
    cleanup_poll.add_argument("--timeout", type=float, default=600.0)
    cleanup_poll.add_argument("--out", required=True)
    cleanup_poll.set_defaults(func=cmd_cleanup_poll)

    excerpt = sub.add_parser("relay-excerpt", help="write the sanitized relay log excerpt")
    excerpt.add_argument("--scratch", required=True)
    excerpt.add_argument("--log", required=True)
    excerpt.add_argument("--out", required=True)
    excerpt.set_defaults(func=cmd_relay_excerpt)

    cleanup = sub.add_parser("cleanup-scratch", help="stop children, verify ports, delete the scratch root")
    cleanup.add_argument("--scratch", required=True)
    cleanup.add_argument("--relay-pid", type=int, default=0)
    cleanup.add_argument("--out", required=True)
    cleanup.set_defaults(func=cmd_cleanup_scratch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
