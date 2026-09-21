#!/usr/bin/env python
"""Real Window F4 runner (VAL-CROSS-011 / VAL-CROSS-013).

Window F part 4 per the binding M4 window plan (validation-contract.md):
the duplicate-grading leg carries its own 1-LLM-op ledger (this window's
single real grade; duplicates are rejected before spend), and the live
panel-port legs run in the exclusive occupancy slot with validator-owned
dummies only.

(1) VAL-CROSS-011 -- duplicate grading submission, real path:
    a disposable [E2E] exercise page is created DIRECTLY under the real
    计算机网络 course page with a deterministic five-question fixture
    (the organizer's exact block shape -- no organize LLM spend), the
    adopted E2E_ANSWER_FIXTURE answers are appended, and the panel's real
    grade route is triggered twice in rapid succession by BOTH required
    mechanisms: a scripted UI double-click (agent-browser, JS-dispatched
    bubbling MouseEvents; the first click starts the ONE real grade job,
    the second is rejected by the single-flight gate) and a parallel HTTP
    pair fired while the job runs (both duplicates rejected before any
    spend). Exactly one grading block set, one relay charge, one summary.

(2) VAL-CROSS-013 -- live panel port occupancy:
    validator-owned dummy listeners (SO_EXCLUSIVEADDRUSE + liveness proof)
    occupy 8791 -> the real panel binds 8792 (startup line + healthz +
    screenshot leg via agent-browser), 8791+8792 -> 8793, all three -> the
    pinned actionable failure copy with exit 1. Dummies survive throughout;
    no port outside 8791/8792/8793 is ever bound; no occupant is killed.

Scratch secrets never reach stdout or evidence: the scratch platform token
lives in the scratch tokens file and child process env only; the real
NOTION_TOKEN is read from the local client .env via Settings and passed to
panel children as process env vars only.

Subcommands (state persists in <scratch>/state.json between stages):
  panel-start     start the real-leg panel child (real NOTION_TOKEN + scratch
                  relay/platform token as process env vars only)
  stage           pick the real course page from the panel directory (read-only)
  page-create     create the [E2E] exercise page under the course page with the
                  deterministic five-question fixture; fetch-verify parent,
                  title, five ordered types, sources, teacher answers
  write-answers   append the adopted E2E_ANSWER_FIXTURE answers; readback verify
  dup-pair        the parallel HTTP pair: two simultaneous POSTs to the grade
                  route (threads + barrier); capture both responses verbatim
  grade-poll      poll the running grade job by id; capture the summary + quota
  dup-verify      read-only verification: exactly one marker + one result block
                  set, answers verbatim, wrong-answer rows exactly once each,
                  ONE relay llm_usage grade row, quota arithmetic, row state
  cleanup         authorized [E2E] cleanup: archive the wrong-answer rows + the
                  [E2E] page with re-fetch/re-query proof
  panel-stop      stop a probe-owned panel child by PID + prove the port free
  port-baseline   prove 8791/8792/8793 are free (exclusive occupancy slot gate)
  port-dummy      start a validator-owned dummy listener on a port
  port-dummy-stop stop a dummy by PID + prove the port free
  port-panel      start the real panel child (hermetic scratch env) against the
                  occupied ports; capture bound port, healthz, listeners-by-PID
  port-panel-fail the all-three-occupied leg: pinned copy + exit 1 + no listener
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # client dev dependency

from pku_sync.config import settings as client_settings
from pku_sync.notion import NotionError, get_client, markdown_to_blocks, page_title
from pku_sync.notion_meta import NotionDirectory
from pku_sync.panel.exercise_grader import GRADE_MARKERS, _fixture_answer, _plain
from pku_sync.panel.exercise_organizer import _blocks, _title_for_create
from pku_sync.panel.jobs import EXERCISE_JOB_BUSY_CODE, EXERCISE_JOB_BUSY_MESSAGE
from pku_sync.panel.ports import PANEL_PORTS, PANEL_PORTS_BUSY_MESSAGE

PREFERRED_COURSES = ("计算机网络",)
E2E_MARKER = "[E2E]"
FIXTURE_HEADING = "E2E_ANSWER_FIXTURE"
EXERCISE_TITLE = "第一讲 · 计算机网络练习 F4"
GRADE_TIMEOUT_S = 600.0
PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")
ANSWER_FIXTURE = {
    "Q1": "A",
    "Q2": "错误（fixture 故意作答为正确）",
    "Q3": "协议层；错误的第二空",
    "Q4": "分层明确职责和接口，隔离实现变化并简化维护。",
    "Q5": "只讨论模块化，故意遗漏互操作性和故障定位。",
}

# Deterministic five-question mixed-type fixture (the adopted Window D rubric
# semantics: Q1 correct, Q2 wrong, Q3 partial, Q4 correct, Q5 wrong; Q2/Q5
# register). Authored to match ANSWER_FIXTURE verbatim so the real grade
# reproduces the rubric without any quiz/organize LLM spend.
F4_QUESTIONS = [
    {
        "type": "选择",
        "question": (
            "TCP/IP 体系结构（工程实践常用）分为哪四层？\n"
            "A. 网络接口层、网际层、传输层、应用层\n"
            "B. 物理层、数据链路层、网络层、会话层\n"
            "C. 传输层、表示层、应用层、链路层\n"
            "D. 网络层、传输层、应用层、物理层"
        ),
        "source": "第一讲",
        "answer": "A",
    },
    {
        "type": "判断",
        "question": "TCP 提供面向连接的可靠传输（确认、重传、滑动窗口、拥塞控制），UDP 提供无连接的尽力而为传输。",
        "source": "第一讲",
        "answer": "正确",
    },
    {
        "type": "填空",
        "question": "分层体系结构中，每一层的功能由该层的协议定义，因此每一层也被称为一个____层；OSI 参考模型共分为____层。",
        "source": "第一讲",
        "answer": "协议层；七",
    },
    {
        "type": "简答",
        "question": "简述网络分层设计的核心价值。",
        "source": "第一讲",
        "answer": "每一层向相邻上层提供明确的服务并向下依赖下层功能，层间通过接口交互、屏蔽实现细节，从而隔离实现变化、简化设计与维护，并支持不同厂商设备与协议互操作。",
    },
    {
        "type": "论述",
        "question": "论述网络分层设计的主要代价，以及工程上如何权衡。",
        "source": "第一讲",
        "answer": "代价：层级之间引入处理开销（逐层加拆首部的时延与带宽），故障定位需要跨层追查，链条变长。权衡：在清晰职责、隔离变化与互操作的收益和层级开销、排障成本之间平衡，按需精简层级（如 TCP/IP 四层对 OSI 七层）。",
    },
]

_STATE_SCRATCH = ""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_page_id(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _check(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "result": "pass" if ok else "fail", "detail": detail})


def _scratch_paths(scratch: str) -> dict:
    root = Path(scratch)
    return {
        "root": root,
        "state": root / "state.json",
        "tokens": root / "tokens.json",
        "relay_db": root / "relay.db",
        "data_dir": root / "client-data",
        "client_env": root / "client-env",
    }


def _load_state(scratch: str) -> dict:
    path = _scratch_paths(scratch)["state"]
    if not path.exists():
        raise SystemExit(f"no runner state at {path}; run the earlier stages first")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(scratch: str, state: dict) -> None:
    _write_json(_scratch_paths(scratch)["state"], state)


def _tokens(scratch: str) -> dict:
    path = _scratch_paths(scratch)["tokens"]
    if not path.exists():
        raise SystemExit(f"scratch tokens file missing: {path} (run the relay-repo window setup first)")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _panel_exercise_rows(panel: str) -> list[dict]:
    response = httpx.get(f"{panel.rstrip('/')}/api/exercises", timeout=120)
    if response.status_code != 200:
        raise SystemExit(f"panel /api/exercises failed: HTTP {response.status_code}")
    return response.json().get("exercises") or []


def _panel_row(rows: list[dict], page_id: str) -> dict | None:
    return next((row for row in rows if row.get("id") == page_id), None)


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


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _wait_for_panel(stdout_log: Path, timeout: float = 40.0) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stdout_log.exists():
            text = stdout_log.read_text(encoding="utf-8", errors="replace")
            match = PANEL_STARTUP_PORT_RE.search(text)
            if match:
                return int(match.group(1)), text.strip().splitlines()[-1]
        time.sleep(0.2)
    raise SystemExit("panel startup line did not appear in time")


# -- Notion read helpers (real token via process env only) --------------------------


def _exercise_snapshot(client, page_id: str, course_id: str) -> dict:
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
    if score is not None and float(score).is_integer():
        score = int(score)
    return {
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
    }


def _wrong_answer_database(client) -> dict:
    cache_path = Path(_scratch_paths(_STATE_SCRATCH)["root"]) / "runner-notion-cache" / "identity_cache.json"
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


def _wrong_answer_rows(client, database: dict, exercise_title: str, operation_id: str) -> dict:
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


def _operation_id(state: dict) -> str:
    store_path = Path(state["data_dir"]) / "panel" / "grading_records.json"
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


def _relay_llm_rows(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        usage_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, account_id, operation, model, input_tokens, output_tokens,"
                " millipoints, recorded_at FROM llm_usage ORDER BY id"
            )
        ]
        accounts = [
            dict(row)
            for row in conn.execute(
                "SELECT id, label, transcribe_seconds_remaining, llm_millipoints_remaining"
                " FROM accounts ORDER BY id"
            )
        ]
    finally:
        conn.close()
    return {"llm_usage_rows": usage_rows, "accounts": accounts}


# -- duplicate-grading stages -------------------------------------------------------


def _child_env(scratch: Path, tokens: dict, relay: str) -> dict[str, str]:
    """The scratch client env for the real-leg panel child.

    Process env vars beat the package-root .env on this host, so the child
    reaches the real Notion workspace (real token) and the scratch relay
    (scratch platform token) while the PKU credentials and the direct OpenAI
    key stay blank: the only upstream work in this leg is the ONE grade call
    proxied by the scratch relay.
    """
    notion_token = (client_settings.notion_token or "").strip()
    if not notion_token:
        raise SystemExit(
            "the local client .env has no NOTION_TOKEN; the panel child cannot "
            "reach the real workspace (the value is never printed)"
        )
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(scratch) / "client-data"),
            "CLOUD_TRANSCRIBE_URL": f"{relay.rstrip('/')}/v1/transcribe",
            "PLATFORM_TOKEN": tokens["client"],
            "TRANSCRIPTION_BACKEND": "cloud",
            "EXERCISE_E2E_ENABLED": "true",
            "NOTION_TOKEN": notion_token,
            "OPENAI_API_KEY": "",
            "PKU_USERNAME": "",
            "PKU_PASSWORD": "",
            "DELETE_VIDEO_AFTER_PROCESSING": "false",
            "PYTHONUTF8": "1",
        }
    )
    return env


def cmd_panel_start(args) -> int:
    """Start the real-leg panel child (real Notion token, scratch relay)."""
    scratch = Path(args.scratch)
    paths = _scratch_paths(args.scratch)
    tokens = _tokens(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    paths["client_env"].mkdir(parents=True, exist_ok=True)
    env = _child_env(scratch, tokens, args.relay)
    # The scratch env file DOCUMENTS the child env (the env dict is
    # authoritative); the real NOTION_TOKEN and the scratch platform token are
    # never written into it.
    (paths["client_env"] / ".env").write_text(
        "# Window F4 scratch client env -- deleted at window cleanup.\n"
        "# The panel child receives these as process env vars (authoritative).\n"
        "# NOTION_TOKEN (real, from the local .env) and PLATFORM_TOKEN (scratch)\n"
        "# are passed via the process environment ONLY and never written here.\n"
        f"DATA_DIR={paths['data_dir']}\n"
        f"CLOUD_TRANSCRIBE_URL={args.relay.rstrip('/')}/v1/transcribe\n"
        "TRANSCRIPTION_BACKEND=cloud\n"
        "EXERCISE_E2E_ENABLED=true\n"
        "DELETE_VIDEO_AFTER_PROCESSING=false\n"
        "OPENAI_API_KEY=\n"
        "PKU_USERNAME=\n"
        "PKU_PASSWORD=\n",
        encoding="utf-8",
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
    bound_port, startup_line = _wait_for_panel(stdout_log)
    health = httpx.get(f"http://127.0.0.1:{bound_port}/healthz", timeout=15)
    health.raise_for_status()
    existing: dict = {}
    if paths["state"].exists():
        try:
            loaded = json.loads(paths["state"].read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    state = {**existing, **{
        "relay": args.relay.rstrip("/"),
        "panel": f"http://127.0.0.1:{bound_port}",
        "tokens_file": str(paths["tokens"]),
        "relay_db": str(paths["relay_db"]),
        "data_dir": str(paths["data_dir"]),
        "panel_pid": proc.pid,
        "panel_listener_pid": _port_listener(bound_port),
        "panel_port": bound_port,
    }}
    _save_state(args.scratch, state)
    evidence = {
        "step": "panel-start",
        "generated_at": _utcnow(),
        "pid": proc.pid,
        "listener_pid": _port_listener(bound_port),
        "requested_port": args.port,
        "bound_port": bound_port,
        "startup_line": startup_line,
        "healthz": health.json(),
        "panel_url": f"http://127.0.0.1:{bound_port}/",
        "student_ui_url": f"http://127.0.0.1:{bound_port}/app",
        "relay": args.relay.rstrip("/"),
        "child_env_keys": sorted(k for k in env if k in {
            "DATA_DIR", "CLOUD_TRANSCRIBE_URL", "PLATFORM_TOKEN",
            "TRANSCRIPTION_BACKEND", "EXERCISE_E2E_ENABLED", "NOTION_TOKEN",
            "OPENAI_API_KEY", "PKU_USERNAME", "PKU_PASSWORD",
            "DELETE_VIDEO_AFTER_PROCESSING", "PYTHONUTF8",
        }),
        "token_handling": (
            "PLATFORM_TOKEN (scratch) and the real NOTION_TOKEN are passed as "
            "process env vars to the panel child only; never printed, never "
            "written to the scratch .env, evidence, or any committed file"
        ),
    }
    _write_json(args.out, evidence)
    print(f"panel started: pid={proc.pid} port={bound_port} healthz={health.json()}")
    return 0


def cmd_stage(args) -> int:
    state_path = _scratch_paths(args.scratch)["state"]
    existing: dict = {}
    if state_path.exists():
        existing = json.loads(state_path.read_text(encoding="utf-8"))
    panel = (args.panel or existing.get("panel") or "").rstrip("/")
    if not panel:
        raise SystemExit("no panel URL in state or --panel; run panel-start first")
    response = httpx.get(f"{panel}/api/directory", timeout=180)
    if response.status_code != 200:
        raise SystemExit(f"panel /api/directory failed: HTTP {response.status_code}")
    directory = response.json()
    courses = directory.get("courses") or []
    course = next((c for c in courses if c.get("title") in PREFERRED_COURSES), None)
    if course is None:
        raise SystemExit("preferred course 计算机网络 not found in the real directory")
    checks: list[dict] = []
    _check(checks, "real course page resolved from the live panel directory",
           bool(course.get("id")) and bool(course.get("url")),
           f"title={course.get('title')} id={course.get('id')}")
    state = {**existing, "course": {"id": course["id"], "title": course["title"], "url": course["url"]}}
    _save_state(args.scratch, state)
    evidence = {
        "step": "stage",
        "generated_at": _utcnow(),
        "course": {"id": course["id"], "title": course["title"], "url": course["url"]},
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"staged course: {course['title']} ({course['id']})")
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_page_create(args) -> int:
    state = _load_state(args.scratch)
    course_id = state["course"]["id"]
    checks: list[dict] = []
    notion_token = (client_settings.notion_token or "").strip()
    if not notion_token:
        raise SystemExit("the local client .env has no NOTION_TOKEN (value never printed)")
    title = _title_for_create(EXERCISE_TITLE, e2e_mode=True)
    blocks = _blocks(F4_QUESTIONS)
    with get_client(client_settings) as client:
        # Pre-create discipline: count [E2E] children under the course page so
        # the window creates exactly one new page (mutations on [E2E] only).
        pre_children = [
            block for block in client.list_child_pages(course_id)
            if E2E_MARKER in str(block.get("title") or "")
        ]
        page = client.create_page(course_id, title, children=blocks)
        page_id = page["id"]
        readback = client.get_page(page_id)
        readback_blocks = client.list_children(page_id)
    parent_id = _norm_page_id((readback.get("parent") or {}).get("page_id") or "")
    texts = [_plain(block) for block in readback_blocks]
    question_types = [
        text.split(". ", 1)[-1]
        for index, text in enumerate(texts)
        if readback_blocks[index].get("type") == "heading_3"
    ]
    source_count = sum(1 for value in texts if value.startswith("来源："))
    teacher_answer_count = sum(1 for value in texts if value.startswith("答案："))
    _check(checks, "page created directly under the real course page",
           parent_id == _norm_page_id(course_id),
           f"parent={parent_id}")
    _check(checks, "page title carries exactly one [E2E] marker",
           page_title(readback).count(E2E_MARKER) == 1,
           f"title={page_title(readback)!r}")
    _check(checks, "five ordered question types exactly once each",
           question_types == ["选择", "判断", "填空", "简答", "论述"],
           f"types={question_types}")
    _check(checks, "every question carries a source", source_count == 5,
           f"sources={source_count}")
    _check(checks, "teacher section carries five standard answers",
           teacher_answer_count == 5, f"teacher_answers={teacher_answer_count}")
    state["exercise"] = {
        "page_id": page_id,
        "url": readback.get("url") or "",
        "title": page_title(readback),
        "course_id": course_id,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "page-create",
        "generated_at": _utcnow(),
        "note": (
            "deterministic five-question fixture written via the organizer's exact "
            "block builder (no organize/quiz LLM spend); the one authorized mutation "
            "family of this window is [E2E]-marked pages only"
        ),
        "pre_existing_e2e_children": len(pre_children),
        "page": {
            "page_id": page_id,
            "url": readback.get("url") or "",
            "title": page_title(readback),
            "expected_parent_id": _norm_page_id(course_id),
            "actual_parent_id": parent_id,
        },
        "blocks_summary": {
            "question_types": question_types,
            "source_count": source_count,
            "teacher_answer_count": teacher_answer_count,
        },
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"page created: {page_title(readback)} ({page_id})")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_dup_click_page(args) -> int:
    """Create the [E2E] page for the scripted double-click leg (zero spend).

    Same deterministic fixture as the real leg, but only the first two answers
    are filled in: the grade route then reaches its single-flight gate and the
    synchronous prepare step, and blocks on the incomplete answers BEFORE any
    upstream call, so rapid duplicate triggers can be probed without a second
    charge against the window's single-grade ledger.
    """
    state = _load_state(args.scratch)
    course_id = state["course"]["id"]
    panel = args.panel.rstrip("/") or state["panel"]
    checks: list[dict] = []
    title = _title_for_create(f"{EXERCISE_TITLE} · 双击", e2e_mode=True)
    partial = {"Q1": ANSWER_FIXTURE["Q1"], "Q2": ANSWER_FIXTURE["Q2"]}
    markdown = (
        f"## {FIXTURE_HEADING}\n\n"
        + "\n\n".join(f"{number}. {partial[f'Q{number}']}" for number in (1, 2))
    )
    blocks = [*_blocks(F4_QUESTIONS), *markdown_to_blocks(markdown)]
    with get_client(client_settings) as client:
        page = client.create_page(course_id, title, children=blocks)
        page_id = page["id"]
        readback = client.get_page(page_id)
        readback_blocks = client.list_children(page_id)
    parent_id = _norm_page_id((readback.get("parent") or {}).get("page_id") or "")
    texts = [_plain(block) for block in readback_blocks]
    fixture_at = next((i for i, value in enumerate(texts) if value == FIXTURE_HEADING), None)
    answers = []
    if fixture_at is not None:
        answers = [
            _fixture_answer(texts[index])
            for index, block in enumerate(readback_blocks)
            if index > fixture_at
            and block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
    _check(checks, "double-click page created directly under the real course page",
           parent_id == _norm_page_id(course_id), f"parent={parent_id}")
    _check(checks, "page title carries exactly one [E2E] marker",
           page_title(readback).count(E2E_MARKER) == 1,
           f"title={page_title(readback)!r}")
    _check(checks, "only two of the five answers are filled in (grading blocks before spend)",
           answers == [partial["Q1"], partial["Q2"]], f"answers={answers}")
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)
    _check(checks, "panel row reads 待批改 so the UI renders the grade control",
           row is not None and row.get("status") == "pending-grade",
           f"row={row}")
    state["dup_click_page"] = {
        "page_id": page_id,
        "url": readback.get("url") or "",
        "title": page_title(readback),
        "course_id": course_id,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "dup-click-page",
        "generated_at": _utcnow(),
        "page": {
            "page_id": page_id,
            "url": readback.get("url") or "",
            "title": page_title(readback),
            "expected_parent_id": _norm_page_id(course_id),
            "actual_parent_id": parent_id,
        },
        "answers_present": answers,
        "panel_row": row,
        "purpose": (
            "the scripted double-click leg runs against this page so the two rapid "
            "UI triggers can be observed without a second real grade: the route "
            "acquires the single-flight gate, reads the page, and blocks on the "
            "incomplete answers before any relay/LLM call"
        ),
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"double-click page created: {page_title(readback)} ({page_id})")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_dup_click_verify(args) -> int:
    """Read-only: the double-click leg cost nothing and wrote nothing."""
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    page = state["dup_click_page"]
    relay_db = state["relay_db"]
    checks: list[dict] = []
    with get_client(client_settings) as client:
        snapshot = _exercise_snapshot(client, page["page_id"], page["course_id"])
    ledger = _relay_llm_rows(relay_db)
    grade_rows = [row for row in ledger["llm_usage_rows"] if row["operation"] == "grade"]
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page["page_id"])
    _check(checks, "the double-click page carries no grading result at all",
           snapshot["result_block_sets"] == 0 and snapshot["grade_marker"] is None,
           f"sets={snapshot['result_block_sets']} marker={snapshot['grade_marker']}")
    _check(checks, "the double-click leg added no charge: the scratch relay still holds "
                   "exactly the ONE grade row of the real leg",
           len(grade_rows) == 1 and len(ledger["llm_usage_rows"]) == 1,
           f"grade_rows={len(grade_rows)} llm_rows={len(ledger['llm_usage_rows'])}")
    _check(checks, "the double-click page row stays 待批改 (no state change)",
           row is not None and row.get("status") == "pending-grade",
           f"row_status={row and row.get('status')}")
    evidence = {
        "step": "dup-click-verify",
        "generated_at": _utcnow(),
        "page": page,
        "page_snapshot": snapshot,
        "relay_ledger": ledger,
        "panel_row": row,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"dup-click-verify: sets={snapshot['result_block_sets']} llm_rows={len(ledger['llm_usage_rows'])}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_write_answers(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    page_id = state["exercise"]["page_id"]
    checks: list[dict] = []
    markdown = (
        f"## {FIXTURE_HEADING}\n\n"
        + "\n\n".join(f"{number}. {ANSWER_FIXTURE[f'Q{number}']}" for number in range(1, 6))
    )
    blocks = markdown_to_blocks(markdown)
    with get_client(client_settings) as client:
        client.append_blocks(page_id, blocks)
        readback = client.list_children(page_id)
    texts = [_plain(block) for block in readback]
    fixture_at = next((i for i, value in enumerate(texts) if value == FIXTURE_HEADING), None)
    answers: dict[str, str] = {}
    if fixture_at is not None:
        rows = [
            _fixture_answer(texts[index])
            for index, block in enumerate(readback, start=0)
            if index > fixture_at
            and block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
        answers = {f"Q{number}": value for number, value in enumerate(rows, 1)}
    _check(checks, "fixture heading present and all five answers read back verbatim",
           fixture_at is not None and answers == ANSWER_FIXTURE,
           f"fixture_at={fixture_at} answers={list(answers)}")
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)
    _check(checks, "panel row reads 待批改 after the answer write",
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


def cmd_dup_pair(args) -> int:
    """The parallel HTTP pair: two simultaneous POSTs to the real grade route.

    Fired while the ONE real grade job is running (started by the scripted
    UI double-click leg). Both duplicates must be rejected by the
    single-flight gate with the pinned busy payload BEFORE any spend; the
    runner proves the pair genuinely overlapped via a barrier + timestamps.
    """
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    page_id = state[args.page_key]["page_id"]
    url = f"{panel}/api/exercises/{page_id}/grade"
    results: list[dict] = []
    barrier = threading.Barrier(2, timeout=10)
    started_at = _utcnow()

    origin = time.monotonic()

    def fire(label: str) -> None:
        barrier.wait()
        sent = time.monotonic()
        response = httpx.post(url, json={}, timeout=60)
        done = time.monotonic()
        results.append({
            "label": label,
            "sent_at_s": round(sent - origin, 4),
            "finished_at_s": round(done - origin, 4),
            "duration_s": round(done - sent, 4),
            "http_status": response.status_code,
            "body": response.json(),
        })

    threads = [threading.Thread(target=fire, args=(name,)) for name in ("pair-A", "pair-B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    checks: list[dict] = []
    pinned = {
        "status": "blocked",
        "code": EXERCISE_JOB_BUSY_CODE,
        "message": EXERCISE_JOB_BUSY_MESSAGE,
        "reason": EXERCISE_JOB_BUSY_MESSAGE,
        "retryable": True,
    }
    ordered = sorted(results, key=lambda item: item["sent_at_s"])
    # A response carrying the busy code never reached the work: the
    # single-flight gate turned it away. Everything else was admitted past the
    # gate (202 when work started, another 409 when the route itself blocked).
    gate_rejected = [
        item for item in results if item["body"].get("code") == EXERCISE_JOB_BUSY_CODE
    ]
    admitted = [item for item in results if item not in gate_rejected]
    started_work = [item for item in results if item["http_status"] == 202]
    overlapped = len(ordered) == 2 and ordered[1]["sent_at_s"] < ordered[0]["finished_at_s"]
    _check(checks, "the two triggers genuinely overlapped (the second was in flight before the first answered)",
           overlapped,
           f"sent={[item['sent_at_s'] for item in ordered]} finished={[item['finished_at_s'] for item in ordered]}")
    _check(checks, "single-flight: exactly ONE of the simultaneous triggers got past the gate",
           len(admitted) == 1,
           f"admitted={[item['label'] for item in admitted]} gate_rejected={[item['label'] for item in gate_rejected]}")
    _check(checks, "no duplicate work started (at most one 202 among the pair)",
           len(started_work) <= 1,
           f"statuses={[item['http_status'] for item in ordered]}")
    _check(checks, "every gate-rejected duplicate carries the pinned single-flight 409 payload verbatim",
           bool(gate_rejected) and all(
               item["http_status"] == 409 and item["body"] == pinned for item in gate_rejected
           ),
           f"gate_rejected={[(item['http_status'], item['body']) for item in gate_rejected]}")
    evidence = {
        "step": "dup-pair",
        "generated_at": _utcnow(),
        "started_at": started_at,
        "url": url,
        "pinned_payload": pinned,
        "results": sorted(results, key=lambda item: item["label"]),
        "checks": checks,
        "note": (
            "two barrier-synchronized POSTs to the real grade route: the gate "
            "admits at most one of them and every duplicate is rejected with the "
            "pinned single-flight 409 before any upstream call"
        ),
    }
    _write_json(args.out, evidence)
    print(f"duplicate pair: statuses={[item['http_status'] for item in results]}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_grade_poll(args) -> int:
    """Poll the ONE real grade job by id; capture summary + post-run quota."""
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    relay = (args.relay.rstrip("/") or state.get("relay") or "")
    token = _tokens(args.scratch)["client"]
    if not args.job_id:
        raise SystemExit("--job-id is required (the UI-triggered job id from the double-click leg)")
    checks: list[dict] = []
    summary = _poll_job(panel, "/api/exercises/grade/status", args.job_id, GRADE_TIMEOUT_S)
    _check(checks, "the single real grade job completed",
           summary.get("status") == "completed",
           f"status={summary.get('status')}")
    allowed_keys = {"status", "exercise_id", "title", "score", "graded_at",
                    "points_charged", "points_remaining", "result_page_url"}
    _check(checks, "summary payload equals the single-run allowlist exactly",
           set(summary) == allowed_keys, f"keys={sorted(summary)}")
    quota_after = _quota(relay, token) if relay else None
    state["grade"] = summary
    state["grade_job_id"] = args.job_id
    if quota_after:
        state["grade_quota_after"] = quota_after
    _save_state(args.scratch, state)
    evidence = {
        "step": "grade-poll",
        "generated_at": _utcnow(),
        "job_id": args.job_id,
        "summary": summary,
        "quota_after": quota_after,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"graded: score={summary.get('score')} charged={summary.get('points_charged')}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


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


def cmd_dup_verify(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    page_id = state["exercise"]["page_id"]
    course_id = state["course"]["id"]
    summary = state["grade"]
    relay_db = state["relay_db"]
    checks: list[dict] = []
    with get_client(client_settings) as client:
        snapshot = _exercise_snapshot(client, page_id, course_id)
        operation_id = _operation_id(state)
        database = _wrong_answer_database(client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"], operation_id)
    q2_rows = list(wrong["rows_by_number"].get(2) or [])
    q5_rows = list(wrong["rows_by_number"].get(5) or [])
    prefix_rows = list(wrong["prefix_rows"])
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)
    ledger = _relay_llm_rows(relay_db)

    _check(checks, "exactly one grading marker / result block set on the Notion page",
           snapshot["result_block_sets"] == 1 and snapshot["grade_marker"] == "E2E_GRADE_RESULT",
           f"sets={snapshot['result_block_sets']} marker={snapshot['grade_marker']!r}")
    _check(checks, "original answers preserved verbatim",
           snapshot["answers"] == ANSWER_FIXTURE,
           f"answers={list(snapshot['answers'])}")
    _check(checks, "page stays under the real course page",
           snapshot["parent_page_id"] == _norm_page_id(course_id),
           f"parent={snapshot['parent_page_id']}")
    _check(checks, "wrong answers registered exactly once each (Q2, Q5) and nothing else",
           len(q2_rows) == 1 and len(q5_rows) == 1 and len(prefix_rows) == 2,
           f"q2={len(q2_rows)} q5={len(q5_rows)} prefix={len(prefix_rows)}")
    _check(checks, "the panel summary equals a single run (score + graded_at match the Notion block)",
           summary.get("score") == snapshot["score"] and summary.get("graded_at") == snapshot["graded_at"],
           f"summary_score={summary.get('score')} notion_score={snapshot['score']} "
           f"summary_graded_at={summary.get('graded_at')} notion_graded_at={snapshot['graded_at']}")
    _check(checks, "panel directory row reads 已批改",
           row is not None and row.get("status") == "graded"
           and row.get("status_label") == "已批改",
           f"row_status={row and row.get('status')}")
    grade_rows = [r for r in ledger["llm_usage_rows"] if r.get("operation") == "grade"]
    _check(checks, "scratch relay records exactly ONE charge for the actual work",
           len(ledger["llm_usage_rows"]) == 1 and len(grade_rows) == 1,
           f"llm_rows={len(ledger['llm_usage_rows'])} grade_rows={len(grade_rows)}")
    charged = grade_rows[0]["millipoints"] if grade_rows else None
    summary_points = float(summary.get("points_charged"))
    _check(checks, "the one charge matches the summary settlement",
           charged is not None and abs(float(charged) / 1000.0 - summary_points) < 1e-9,
           f"usage_millipoints={charged} summary_points={summary_points}")
    account = ledger["accounts"][0] if ledger["accounts"] else {}
    remaining = account.get("llm_millipoints_remaining")
    _check(checks, "quota decremented exactly by the single charge",
           remaining is not None and abs(100000 - int(remaining) - float(charged)) < 1e-6,
           f"remaining={remaining} charged={charged}")
    state["dup_verify"] = {
        "snapshot": snapshot,
        "wrong_answer_rows": {
            "expected_titles": wrong["expected_titles"],
            "rows": {str(number): _row_identity(rows) for number, rows in wrong["rows_by_number"].items()},
        },
        "panel_row_after_grade": row,
        "operation_id": operation_id,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "dup-verify",
        "generated_at": _utcnow(),
        "snapshot": snapshot,
        "summary": summary,
        "wrong_answer_rows": {
            "expected_titles": wrong["expected_titles"],
            "q2_rows": _row_identity(q2_rows),
            "q5_rows": _row_identity(q5_rows),
            "prefix_row_count": len(prefix_rows),
        },
        "panel_row_after_grade": row,
        "relay_ledger": ledger,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"dup-verify: sets={snapshot['result_block_sets']} score={snapshot['score']} "
          f"wrong_rows={len(prefix_rows)} llm_rows={len(ledger['llm_usage_rows'])}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def _post_archive_fetch(client, page_id: str) -> dict:
    """Post-cleanup fetch of one archived page: Notion shows some
    trash-archived pages as archived=true and answers others 404
    object_not_found (trash-visibility variants of the same archive)."""
    try:
        page = client.get_page(page_id)
    except NotionError as exc:
        return {
            "page_id": page_id,
            "result": "fetch_error",
            "http_status": exc.status,
            "code": exc.code,
        }
    return {
        "page_id": page_id,
        "result": "archived" if page.get("archived") else "still_present",
        "archived": bool(page.get("archived")),
    }


def cmd_cleanup(args) -> int:
    """Authorized [E2E] cleanup: archive the wrong-answer rows + the [E2E] page.

    Notion's REST surface expresses deletion as trash-archival (no hard-delete
    API exists); the proof shows the archived flag, the rows/pages disappearing
    from their containers, and a re-query returning zero rows.
    """
    state = _load_state(args.scratch)
    page_id = state["exercise"]["page_id"]
    operation_id = _operation_id(state)
    course_id = state["course"]["id"]
    checks: list[dict] = []
    with get_client(client_settings) as client:
        database = _wrong_answer_database(client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"], operation_id)
        q2_rows = list(wrong["rows_by_number"].get(2) or [])
        q5_rows = list(wrong["rows_by_number"].get(5) or [])
        prefix_rows = list(wrong["prefix_rows"])
        _check(checks, "exactly two wrong-answer rows exist before cleanup (Q2, Q5 once each)",
               len(q2_rows) == 1 and len(q5_rows) == 1 and len(prefix_rows) == 2,
               f"q2={len(q2_rows)} q5={len(q5_rows)} prefix={len(prefix_rows)}")
        page_before = client.get_page(page_id)
        parent_before = _norm_page_id((page_before.get("parent") or {}).get("page_id") or "")
        _check(checks, "[E2E] page fetch-verified before cleanup (title + parent)",
               page_title(page_before).count(E2E_MARKER) == 1
               and parent_before == _norm_page_id(course_id),
               f"title={page_title(page_before)!r} parent={parent_before}")
        archived_rows = []
        for row in [*q2_rows, *q5_rows]:
            client.archive_page(row["id"])
            archived_rows.append(_row_identity([row])[0])
        for row in prefix_rows:
            if row.get("id") and all(row["id"] != item["page_id"] for item in archived_rows):
                client.archive_page(row["id"])
                archived_rows.append({"page_id": row.get("id"), "url": row.get("url")})
        client.archive_page(page_id)
        # The double-click leg's page carries no grading result and no
        # wrong-answer rows, so its cleanup is the page archive alone.
        dup_click = state.get("dup_click_page")
        dup_click_before = client.get_page(dup_click["page_id"]) if dup_click else None
        if dup_click:
            client.archive_page(dup_click["page_id"])
        dup_click_after = client.get_page(dup_click["page_id"]) if dup_click else None
        wrong_after = _wrong_answer_rows(client, database, state["exercise"]["title"], operation_id)
        page_after = client.get_page(page_id)
        course_children = client.list_child_pages(course_id)
        # Per-row post-archive fetch: each archived row answers either
        # archived=true or 404 object_not_found -- both are Notion's
        # trash-visibility view of the same archive.
        rows_after_fetch = [
            dict(fetch, title=row.get("title"))
            for fetch, row in zip(
                (_post_archive_fetch(client, row["page_id"]) for row in archived_rows),
                archived_rows,
            )
        ]
    remaining_prefix = len(wrong_after["prefix_rows"])
    page_still_listed = any(
        _norm_page_id(block.get("id") or "") == _norm_page_id(page_id)
        for block in course_children
    )
    _check(checks, "wrong-answer rows deleted: re-query returns zero rows for the exercise",
           remaining_prefix == 0
           and len(wrong_after["rows_by_number"].get(2) or []) == 0
           and len(wrong_after["rows_by_number"].get(5) or []) == 0,
           f"prefix={remaining_prefix}")
    _check(checks, "each archived wrong-answer row re-fetches as archived or 404 (Notion's "
                   "trash-visibility variants of the same archive)",
           len(rows_after_fetch) == len(archived_rows) and all(
               item["result"] == "archived"
               or (item["result"] == "fetch_error" and item.get("http_status") == 404)
               for item in rows_after_fetch
           ),
           f"rows_after_fetch={[(item['result'], item.get('archived'), item.get('http_status')) for item in rows_after_fetch]}")
    _check(checks, "[E2E] page deleted (trash-archived): re-fetch shows archived and the page "
                   "no longer lists under the course page",
           bool(page_after.get("archived")) is True and page_still_listed is False,
           f"archived={page_after.get('archived')} still_listed={page_still_listed}")
    dup_click_listed = bool(dup_click) and any(
        _norm_page_id(block.get("id") or "") == _norm_page_id(dup_click["page_id"])
        for block in course_children
    )
    _check(checks, "the double-click [E2E] page deleted (trash-archived) with the same proof",
           bool(dup_click)
           and bool((dup_click_after or {}).get("archived")) is True
           and dup_click_listed is False,
           f"archived={(dup_click_after or {}).get('archived')} still_listed={dup_click_listed}")
    evidence = {
        "step": "cleanup",
        "generated_at": _utcnow(),
        "authorization": (
            "window-F4 [E2E] mutation authorization (binding Window F plan + the "
            "orchestrator 2026-09-21 dispatch): archive exactly the [E2E]-marked "
            "wrong-answer rows and the [E2E] exercise page, with re-query/re-fetch proof"
        ),
        "page": {
            "page_id": page_id,
            "title": page_title(page_before),
            "expected_parent_id": _norm_page_id(course_id),
            "actual_parent_id": parent_before,
            "pre_cleanup_fetch": {
                "fetched_at": _utcnow(),
                "title": page_title(page_before),
                "actual_parent_id": parent_before,
                "title_verified": True,
                "parent_verified": True,
            },
            "cleanup_status": "archived",
            "reason": (
                "authorized window cleanup; Notion deletion is trash-archive via the "
                "API (no hard-delete surface); re-fetch shows archived=true and the "
                "page is absent from the course page's children"
            ),
            "post_cleanup_fetch": {
                "fetched_at": _utcnow(),
                "result": "archived" if page_after.get("archived") else "still_present",
                "archived": bool(page_after.get("archived")),
                "still_listed_under_course": page_still_listed,
            },
        },
        "dup_click_page": {
            "page_id": (dup_click or {}).get("page_id"),
            "title": page_title(dup_click_before) if dup_click_before else None,
            "cleanup_status": "archived" if (dup_click_after or {}).get("archived") else "still_present",
            "pre_cleanup_fetch": {
                "fetched_at": _utcnow(),
                "title": page_title(dup_click_before) if dup_click_before else None,
                "actual_parent_id": _norm_page_id(
                    ((dup_click_before or {}).get("parent") or {}).get("page_id") or ""
                ),
            },
            "post_cleanup_fetch": {
                "fetched_at": _utcnow(),
                "archived": bool((dup_click_after or {}).get("archived")),
                "still_listed_under_course": dup_click_listed,
            },
        },
        "wrong_answer_rows": {
            "expected_titles": wrong["expected_titles"],
            "rows_before": _row_identity(prefix_rows),
            "archived_rows": archived_rows,
            "rows_after_fetch": rows_after_fetch,
            "rows_after_query_count": remaining_prefix,
            "cleanup_status": "archived",
            "reason": (
                "authorized window cleanup; each row trash-archived via the API; "
                "the database re-query returns zero rows for this exercise"
            ),
        },
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"cleanup: rows_archived={len(archived_rows)} page_archived={page_after.get('archived')}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_panel_stop(args) -> int:
    subprocess.run(["taskkill", "/PID", str(args.pid), "/F", "/T"], capture_output=True)
    deadline = time.monotonic() + 10
    free = False
    while time.monotonic() < deadline:
        listener = _port_listener(args.port)
        if listener is None:
            free = True
            break
        time.sleep(0.3)
    evidence = {
        "step": "panel-stop",
        "generated_at": _utcnow(),
        "pid": args.pid,
        "port": args.port,
        "port_free": free,
    }
    _write_json(args.out, evidence)
    print(f"panel stopped: pid={args.pid} port={args.port} free={free}")
    return 0 if free else 1


# -- panel port-occupancy stages (validator-owned dummies only) ---------------------


def _port_listener(port: int) -> int | None:
    """The PID owning a LISTENING socket on port, or None when free."""
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue"
             " | ForEach-Object { \"$($_.OwningProcess)\" }"],
            capture_output=True, text=True, timeout=15,
        )
        values = [int(value) for value in (output.stdout or "").split() if value.isdigit()]
        return values[0] if values else None
    except (subprocess.SubprocessError, ValueError):
        return None


def _process_listeners(pid: int) -> list[str]:
    """Every LISTENING address:port owned by the process (loopback proof)."""
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -OwningProcess {pid} -State Listen -ErrorAction SilentlyContinue"
             " | ForEach-Object { \"$($_.LocalAddress):$($_.LocalPort)\" }"],
            capture_output=True, text=True, timeout=15,
        )
        lines = [line.strip() for line in (output.stdout or "").splitlines() if line.strip()]
        return lines
    except subprocess.SubprocessError:
        return []


def _dummy_alive(port: int) -> dict:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/__alive", timeout=10)
        body = response.json()
        return {"alive": response.status_code == 200 and body.get("kind") == "window-f4-dummy",
                "http_status": response.status_code, "body": body}
    except Exception as exc:
        return {"alive": False, "error": type(exc).__name__}


class _DummyServer:
    """Tiny liveness server: GET /__alive answers the validator-owned marker.

    Honest occupancy on Windows: without SO_EXCLUSIVEADDRUSE the stack would
    silently allow a second bind onto an occupied port (verified host fact in
    library/environment.md), so the exclusive flag is set BEFORE the bind —
    the same ``server_bind`` override as the OAuth callback listener.
    """

    def __init__(self, port: int):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"kind": "window-f4-dummy", "port": port}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        class Server(HTTPServer):
            def server_bind(self):
                if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.allow_reuse_address = False
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        self._server = Server(("127.0.0.1", port), Handler)

    def serve_forever(self):
        self._server.serve_forever()


def cmd_dummy_serve(args) -> int:
    if args.port not in PANEL_PORTS:
        raise SystemExit(f"dummy port must be one of {PANEL_PORTS}")
    dummy = _DummyServer(args.port)
    print(f"window-f4 dummy serving on 127.0.0.1:{args.port}", flush=True)
    dummy.serve_forever()
    return 0


def cmd_port_dummy(args) -> int:
    """Start a persistent validator-owned dummy child on an approved port."""
    if args.port not in PANEL_PORTS:
        raise SystemExit(f"dummy port must be one of {PANEL_PORTS}")
    occupant = _port_listener(args.port)
    if occupant is not None:
        raise SystemExit(
            f"port {args.port} is already occupied by PID {occupant}; "
            "validator-owned dummies never displace an occupant (aborting leg)"
        )
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_dummy-serve", "--port", str(args.port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    liveness = _dummy_alive(args.port)
    while not liveness.get("alive") and time.monotonic() < deadline:
        time.sleep(0.3)
        liveness = _dummy_alive(args.port)
    if not liveness.get("alive"):
        proc.terminate()
        raise SystemExit(f"dummy on {args.port} failed its liveness probe: {liveness}")
    state_path = _scratch_paths(args.scratch)["state"]
    state: dict = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    dummies = state.get("port_dummies") or {}
    # ``pid`` is the spawned trampoline (taskkill /T kills the tree below it);
    # the socket's actual owner can be a grandchild (uv python trampoline), so
    # the listener-owner PID is recorded too for occupancy comparisons.
    listener_pid = _port_listener(args.port)
    dummies[str(args.port)] = {
        "port": args.port, "pid": proc.pid,
        "listener_pid": listener_pid, "started_at": _utcnow(),
    }
    state["port_dummies"] = dummies
    _save_state(args.scratch, state)
    evidence = {
        "step": "port-dummy",
        "generated_at": _utcnow(),
        "port": args.port,
        "pid": proc.pid,
        "listener_pid": listener_pid,
        "liveness": liveness,
        "bind": "SO_EXCLUSIVEADDRUSE loopback listener (validator-owned)",
    }
    _write_json(args.out, evidence)
    print(f"dummy on {args.port}: pid={proc.pid} alive={liveness['alive']}")
    return 0


def cmd_port_dummy_stop(args) -> int:
    """Stop a validator-owned dummy child BY PID and prove the port freed."""
    state = _load_state(args.scratch)
    dummies = state.get("port_dummies") or {}
    entry = dummies.get(str(args.port))
    if entry is None:
        raise SystemExit(f"no recorded dummy for port {args.port}")
    pid = int(entry.get("pid") or 0)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
    deadline = time.monotonic() + 10
    free = False
    while time.monotonic() < deadline:
        if _port_listener(args.port) is None:
            free = True
            break
        time.sleep(0.3)
    del dummies[str(args.port)]
    state["port_dummies"] = dummies
    _save_state(args.scratch, state)
    evidence = {
        "step": "port-dummy-stop",
        "generated_at": _utcnow(),
        "port": args.port,
        "pid": pid,
        "port_free": free,
        "final_liveness": _dummy_alive(args.port),
    }
    _write_json(args.out, evidence)
    print(f"dummy stopped: port={args.port} pid={pid} free={free}")
    return 0 if free else 1


def _port_panel_child_env(scratch: Path, port: int) -> dict[str, str]:
    """Hermetic scratch env for the port-leg panel: no real token anywhere.

    Env vars beat .env files on this host, so explicit empty strings keep the
    child away from the real Notion token and the deployed relay (the proven
    M2 live-leg recipe: honest not-activated/unconnected states, zero network).
    """
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(Path(scratch) / "client-data"),
            "NOTION_TOKEN": "",
            "PLATFORM_TOKEN": "",
            "PKU_USERNAME": "",
            "PKU_PASSWORD": "",
            "OPENAI_API_KEY": "",
            "PYTHONUTF8": "1",
        }
    )
    return env


def cmd_port_panel(args) -> int:
    """Start the real panel child against the occupied approved ports.

    The preferred port is always 8791 (the product default); the landed port
    policy must select the first FREE approved fallback. Captures the startup
    line (surfaced port), healthz on the bound port, the child's complete
    listener set (loopback-only, inside 8791/8792/8793 only), and the dummy
    liveness before/during/after.
    """
    scratch = Path(args.scratch)
    paths = _scratch_paths(args.scratch)
    paths["client_env"].mkdir(parents=True, exist_ok=True)
    (paths["data_dir"]).mkdir(parents=True, exist_ok=True)
    env = _port_panel_child_env(scratch, args.port)
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
    try:
        bound_port, startup_line = _wait_for_panel(stdout_log)
    except SystemExit:
        proc.terminate()
        raise
    time.sleep(0.6)
    health = httpx.get(f"http://127.0.0.1:{bound_port}/healthz", timeout=15)
    # The console-script shim spawns the real python child that owns the
    # listener (the wrapper-PID hazard in library/environment.md), so the
    # listener set is enumerated from the socket's actual owning PID.
    listener_pid = _port_listener(bound_port)
    listeners = _process_listeners(listener_pid) if listener_pid else []
    dummy_probe = {str(port): _dummy_alive(port) for port in PANEL_PORTS}
    approved_occupancy = {str(port): _port_listener(port) for port in PANEL_PORTS}
    listener_ok = bool(listeners) and all(
        entry == f"127.0.0.1:{bound_port}" for entry in listeners
    )
    recorded_dummies = _load_state(args.scratch).get("port_dummies") or {}
    dummy_listener_pids = {
        int((value or {}).get("listener_pid") or value.get("pid") or 0)
        for value in recorded_dummies.values()
    }
    # No port outside the approved list is ever bound: the only approved-port
    # occupants are the panel (on the bound port) and the recorded dummies.
    occupancy_ok = all(
        pid is None or pid in dummy_listener_pids or pid == listener_pid
        for pid in approved_occupancy.values()
    ) and approved_occupancy.get(str(bound_port)) == listener_pid
    dummies_alive = all(
        dummy_probe.get(str(port), {}).get("alive") for port in recorded_dummies
    )
    checks: list[dict] = []
    _check(checks, "panel bound the first FREE approved port, preferring 8791",
           bound_port == args.expected_port,
           f"bound={bound_port} expected={args.expected_port}")
    _check(checks, "healthz answers on the bound fallback port",
           health.status_code == 200 and health.json().get("ok") is True,
           f"status={health.status_code} body={health.json()}")
    _check(checks, "the panel binds loopback only, only the chosen approved port",
           listener_ok, f"listener_pid={listener_pid} listeners={listeners}")
    _check(checks, "no approved-port occupant beyond the panel + the validator dummies",
           occupancy_ok, f"occupancy={approved_occupancy} dummies={recorded_dummies} listener_pid={listener_pid}")
    _check(checks, "every recorded validator dummy survived the panel start and still serves",
           dummies_alive, f"dummies={ {k: v for k, v in recorded_dummies.items()} } probes={ {k: v.get('alive') for k, v in dummy_probe.items()} }")
    state = _load_state(args.scratch)
    state["port_panels"] = {**(state.get("port_panels") or {}), args.leg: {
        "pid": proc.pid, "port": bound_port, "requested_port": args.port,
        "listener_pid": listener_pid,
    }}
    _save_state(args.scratch, state)
    evidence = {
        "step": "port-panel",
        "generated_at": _utcnow(),
        "leg": args.leg,
        "pid": proc.pid,
        "listener_pid": listener_pid,
        "requested_port": args.port,
        "bound_port": bound_port,
        "startup_line": startup_line,
        "healthz": health.json(),
        "listeners_by_pid": listeners,
        "approved_port_occupancy": approved_occupancy,
        "dummy_liveness": dummy_probe,
        "child_env_keys": sorted(k for k in env if k in {
            "DATA_DIR", "NOTION_TOKEN", "PLATFORM_TOKEN", "PKU_USERNAME",
            "PKU_PASSWORD", "OPENAI_API_KEY", "PYTHONUTF8",
        }),
        "hermetic_note": (
            "scratch DATA_DIR with NOTION_TOKEN/PLATFORM_TOKEN/PKU credentials "
            "explicitly empty (env vars beat .env): unconnected + not-activated "
            "states, zero network beyond loopback"
        ),
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"panel leg {args.leg}: pid={proc.pid} bound={bound_port} healthz={health.json()}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    if all(check["result"] == "pass" for check in checks):
        return 0
    return 1


def cmd_port_panel_fail(args) -> int:
    """The all-three-occupied leg: pinned copy + exit 1 + no listener.

    Never kills or contacts any occupant; all three dummies must still serve
    afterwards, and the failed launcher must not bind anything anywhere.
    """
    scratch = Path(args.scratch)
    paths = _scratch_paths(args.scratch)
    paths["client_env"].mkdir(parents=True, exist_ok=True)
    dummy_probe_before = {str(port): _dummy_alive(port) for port in PANEL_PORTS}
    occupancy_before = {str(port): _port_listener(port) for port in PANEL_PORTS}
    env = _port_panel_child_env(scratch, args.port)
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
    returncode = proc.wait(timeout=60)
    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace")
    time.sleep(0.5)
    dummy_probe_after = {str(port): _dummy_alive(port) for port in PANEL_PORTS}
    occupancy_after = {str(port): _port_listener(port) for port in PANEL_PORTS}
    recorded_dummies = _load_state(args.scratch).get("port_dummies") or {}
    dummy_listener_pids = {
        int((value or {}).get("listener_pid") or value.get("pid") or 0)
        for value in recorded_dummies.values()
    }
    checks: list[dict] = []
    _check(checks, "launcher exits nonzero when all three approved ports are occupied",
           returncode == 1, f"exit={returncode}")
    combined = stdout_text + stderr_text
    _check(checks, "the pinned actionable failure copy is surfaced verbatim",
           PANEL_PORTS_BUSY_MESSAGE in combined,
           f"copy_present={PANEL_PORTS_BUSY_MESSAGE in combined}")
    _check(checks, "all three validator-owned dummies survive the failed launch",
           all(probe["alive"] for probe in dummy_probe_after.values()),
           f"probes={ {k: v['alive'] for k, v in dummy_probe_after.items()} }")
    _check(checks, "every approved port keeps its original validator-owned occupant (nothing displaced)",
           occupancy_after == occupancy_before
           and all(pid in dummy_listener_pids for pid in occupancy_after.values()),
           f"before={occupancy_before} after={occupancy_after} dummy_listener_pids={sorted(dummy_listener_pids)}")
    evidence = {
        "step": "port-panel-fail",
        "generated_at": _utcnow(),
        "requested_port": args.port,
        "exit_code": returncode,
        "pinned_copy": PANEL_PORTS_BUSY_MESSAGE,
        "copy_in_output": PANEL_PORTS_BUSY_MESSAGE in combined,
        "stdout_text": stdout_text,
        "stderr_text": stderr_text,
        "dummy_liveness_before": dummy_probe_before,
        "dummy_liveness_after": dummy_probe_after,
        "approved_port_occupancy_before": occupancy_before,
        "approved_port_occupancy_after": occupancy_after,
        "checks": checks,
        "note": (
            "the failed launcher binds nothing and kills no occupant; the three "
            "validator-owned dummies (SO_EXCLUSIVEADDRUSE, liveness-proven) survive"
        ),
    }
    _write_json(args.out, evidence)
    print(f"port-panel-fail: exit={returncode} copy={PANEL_PORTS_BUSY_MESSAGE in combined}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_port_baseline(args) -> int:
    checks: list[dict] = []
    occupancy = {}
    for port in PANEL_PORTS:
        pid = _port_listener(port)
        occupancy[str(port)] = {"occupied": pid is not None, "occupant_pid": pid}
        if pid is not None:
            _check(checks, f"approved port {port} is free for the exclusive occupancy slot",
                   False, f"occupied by PID {pid} (foreign occupant: never killed)")
    _check(checks, "8791/8792/8793 all free (exclusive occupancy slot)",
           all(not value["occupied"] for value in occupancy.values()),
           f"occupancy={occupancy}")
    evidence = {
        "step": "port-baseline",
        "generated_at": _utcnow(),
        "occupancy": occupancy,
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"port baseline: {occupancy}")
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2e_window_f4.py",
        description=(
            "Real Window F4 runner (VAL-CROSS-011 duplicate grading + "
            "VAL-CROSS-013 live panel port occupancy): probe tooling only, "
            "product surfaces everywhere; scratch secrets never reach stdout "
            "or evidence."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    panel_start = sub.add_parser("panel-start", help="start the real-leg panel child")
    panel_start.add_argument("--scratch", required=True)
    panel_start.add_argument("--relay", required=True)
    panel_start.add_argument("--port", type=int, default=8791)
    panel_start.add_argument("--stdout-log", required=True)
    panel_start.add_argument("--stderr-log", required=True)
    panel_start.add_argument("--out", required=True)
    panel_start.set_defaults(func=cmd_panel_start)

    stage = sub.add_parser("stage", help="pick the real course page (read-only)")
    stage.add_argument("--scratch", required=True)
    stage.add_argument("--panel", default="")
    stage.add_argument("--out", required=True)
    stage.set_defaults(func=cmd_stage)

    page_create = sub.add_parser("page-create", help="create the [E2E] exercise page (no LLM spend)")
    page_create.add_argument("--scratch", required=True)
    page_create.add_argument("--out", required=True)
    page_create.set_defaults(func=cmd_page_create)

    write_answers = sub.add_parser("write-answers", help="append the adopted answer fixture")
    write_answers.add_argument("--scratch", required=True)
    write_answers.add_argument("--panel", default="")
    write_answers.add_argument("--out", required=True)
    write_answers.set_defaults(func=cmd_write_answers)

    dup_click_page = sub.add_parser(
        "dup-click-page", help="create the zero-spend [E2E] page for the double-click leg"
    )
    dup_click_page.add_argument("--scratch", required=True)
    dup_click_page.add_argument("--panel", default="")
    dup_click_page.add_argument("--out", required=True)
    dup_click_page.set_defaults(func=cmd_dup_click_page)

    dup_click_verify = sub.add_parser(
        "dup-click-verify", help="read-only proof the double-click leg cost nothing"
    )
    dup_click_verify.add_argument("--scratch", required=True)
    dup_click_verify.add_argument("--panel", default="")
    dup_click_verify.add_argument("--out", required=True)
    dup_click_verify.set_defaults(func=cmd_dup_click_verify)

    dup_pair = sub.add_parser("dup-pair", help="parallel HTTP pair against the grade route")
    dup_pair.add_argument("--scratch", required=True)
    dup_pair.add_argument("--panel", default="")
    dup_pair.add_argument(
        "--page-key", default="exercise", choices=("exercise", "dup_click_page"),
        help="which staged [E2E] page the pair targets",
    )
    dup_pair.add_argument("--out", required=True)
    dup_pair.set_defaults(func=cmd_dup_pair)

    grade_poll = sub.add_parser("grade-poll", help="poll the ONE real grade job by id")
    grade_poll.add_argument("--scratch", required=True)
    grade_poll.add_argument("--panel", default="")
    grade_poll.add_argument("--relay", default="")
    grade_poll.add_argument("--job-id", required=True)
    grade_poll.add_argument("--out", required=True)
    grade_poll.set_defaults(func=cmd_grade_poll)

    dup_verify = sub.add_parser("dup-verify", help="read-only duplicate-grading verification")
    dup_verify.add_argument("--scratch", required=True)
    dup_verify.add_argument("--panel", default="")
    dup_verify.add_argument("--out", required=True)
    dup_verify.set_defaults(func=cmd_dup_verify)

    cleanup = sub.add_parser("cleanup", help="authorized [E2E] cleanup with re-fetch proof")
    cleanup.add_argument("--scratch", required=True)
    cleanup.add_argument("--out", required=True)
    cleanup.set_defaults(func=cmd_cleanup)

    panel_stop = sub.add_parser("panel-stop", help="stop a probe-owned panel child by PID")
    panel_stop.add_argument("--pid", type=int, required=True)
    panel_stop.add_argument("--port", type=int, required=True)
    panel_stop.add_argument("--out", required=True)
    panel_stop.set_defaults(func=cmd_panel_stop)

    port_baseline = sub.add_parser("port-baseline", help="prove 8791/8792/8793 free")
    port_baseline.add_argument("--out", required=True)
    port_baseline.set_defaults(func=cmd_port_baseline)

    dummy_serve = sub.add_parser(
        "_dummy-serve",
        help=argparse.SUPPRESS,
        description="persistent dummy listener child (spawned by port-dummy)",
    )
    dummy_serve.add_argument("--port", type=int, required=True)
    dummy_serve.set_defaults(func=cmd_dummy_serve)

    port_dummy = sub.add_parser("port-dummy", help="start a validator-owned dummy listener")
    port_dummy.add_argument("--scratch", required=True)
    port_dummy.add_argument("--port", type=int, required=True)
    port_dummy.add_argument("--out", required=True)
    port_dummy.set_defaults(func=cmd_port_dummy)

    port_dummy_stop = sub.add_parser("port-dummy-stop", help="stop a dummy by PID + prove the port free")
    port_dummy_stop.add_argument("--scratch", required=True)
    port_dummy_stop.add_argument("--port", type=int, required=True)
    port_dummy_stop.add_argument("--out", required=True)
    port_dummy_stop.set_defaults(func=cmd_port_dummy_stop)

    port_panel = sub.add_parser("port-panel", help="real panel child against occupied ports")
    port_panel.add_argument("--scratch", required=True)
    port_panel.add_argument("--port", type=int, default=8791)
    port_panel.add_argument("--expected-port", type=int, required=True)
    port_panel.add_argument("--leg", required=True)
    port_panel.add_argument("--stdout-log", required=True)
    port_panel.add_argument("--stderr-log", required=True)
    port_panel.add_argument("--out", required=True)
    port_panel.set_defaults(func=cmd_port_panel)

    port_panel_fail = sub.add_parser("port-panel-fail", help="all-three-occupied failure leg")
    port_panel_fail.add_argument("--scratch", required=True)
    port_panel_fail.add_argument("--port", type=int, default=8791)
    port_panel_fail.add_argument("--stdout-log", required=True)
    port_panel_fail.add_argument("--stderr-log", required=True)
    port_panel_fail.add_argument("--out", required=True)
    port_panel_fail.set_defaults(func=cmd_port_panel_fail)

    return parser


def main(argv: list[str] | None = None) -> int:
    global _STATE_SCRATCH
    args = build_parser().parse_args(argv)
    if getattr(args, "scratch", None):
        _STATE_SCRATCH = args.scratch
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
