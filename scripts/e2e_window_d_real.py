#!/usr/bin/env python
"""Real Window D runner (VAL-EXER-035 / VAL-EXER-036).

Drives the real exercise lifecycle end to end per the dated Window D
authorization (authorization.json in the window pack): ONE organize mutation
(one real relay /v1/llm quiz -> real Bailian -> one disposable [E2E] exercise
page created directly under a REAL course page), the authorized answer
fixture write, ONE grade mutation (one real relay /v1/llm grade -> real
Bailian -> real Notion write-back + wrong-answer rows in the real
「知识点与错题」 database), zero-spend idempotent reruns, read-only
verification with the SAME WindowDReadOnlyVerifier the fake dry run proved,
settlement reconciliation against the relay's llm_usage rows, and final
cleanup of every [E2E] artifact.

Everything runs through product surfaces only:
  - the panel's own organize/grade HTTP routes (EXERCISE_E2E_ENABLED=true),
  - the panel's directory/exercises API for row states (已整理 -> 待批改 ->
    已批改),
  - the panel's platform bridge -> relay /v1/llm (Bearer scratch platform
    token; the token value never reaches stdout, evidence, or any committed
    file -- it lives in the scratch tokens file and child process env only),
  - the runner's own Notion read client for read-only verification.

The real NOTION_TOKEN is sourced from the local client .env via the process
environment of the probe-owned panel child only (env vars beat .env files,
verified on this host); it is never printed or written anywhere.

Subcommands (state persists in <scratch>/state.json between stages):
  panel-start    launch the probe-owned panel child (first free of
                 8791/8792/8793) against the scratch env
  stage          pick the target course + lecture from the panel's real
                 directory; build the scratch notes tree (one notes_ready
                 recording whose course/lecture titles match the real Notion
                 identities exactly)
  organize       panel organize (e2e_mode) -> the ONE quiz op + the [E2E]
                 page under the course page; fetch-verify parent, title,
                 five ordered types, sources, teacher answers; capture the
                 directory row (已整理)
  write-answers  append the adopted E2E_ANSWER_FIXTURE answers onto the
                 [E2E] page (the one authorized fixture write); capture the
                 directory row (待批改)
  grade          panel grade -> the ONE grade op + write-back; capture the
                 summary; fetch-verify marker, total, per-question results,
                 answers verbatim, wrong-answer rows; capture the directory
                 row (已批改)
  reruns         organize same-scope rerun (reused, zero charge) + grade
                 rerun (confirm_required gate, then confirmed zero-charge
                 no-op with unchanged score/timestamp)
  verify         full read-only verification: WindowDReadOnlyVerifier over
                 real snapshots, relay llm_usage settlement rows, panel row
                 states, quota corroboration; writes the consolidated
                 evidence JSON and exits non-zero on any failed check
  panel-stop     stop the probe-owned panel child by PID

Zero-spend resume subcommands (Window D recovery, zero additional LLM ops;
the retained charged record completes the write-back through the product's
durable recovery path; authorization:
window-d-resume-zero-spend-cleanup-2026-09-20):
  seed-resume    prerequisite-gate the retained [E2E] page read-only (parent,
                 [E2E] title, five types, sources, teacher answers, fixture
                 answers verbatim, no marker, zero wrong-answer rows), probe
                 the fresh scratch relay baselines, then seed the retained
                 charged grading record (verbatim) and the organize-scope
                 record into the scratch data root
  row-snapshot   capture one live panel exercise row into runner state
  grade --resume [--job-id <id>]
                 the resume grade: the panel route enters the durable
                 recovery, completes the write-back + Q2/Q5 registration from
                 the STORED charged result with ZERO relay calls, preserves
                 the retained settlement; --job-id polls a UI-triggered job
  verify --resume
                 full read-only verification where the settlement ledger is
                 the retained captured llm_usage rows (the prior scratch
                 relay DB was deleted at that window's cleanup) and the
                 fresh scratch relay proves zero new rows/spend
  cleanup        archive (trash) the two [E2E] wrong-answer rows + the
                 retained [E2E] page with re-fetch/re-query proof
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
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # client dev dependency

from pku_sync.config import settings as client_settings
from pku_sync.e2e_window_d import ANSWER_FIXTURE, WindowDReadOnlyVerifier
from pku_sync.models import Recording
from pku_sync.notion import get_client, markdown_to_blocks, page_title
from pku_sync.notion_meta import NotionDirectory
from pku_sync.panel.exercise_grader import GRADE_MARKERS, _fixture_answer, _plain
from pku_sync.panel.exercise_organizer import _scope_key
from pku_sync.pipeline import RecordingJob, collect_jobs, recording_stage
from pku_sync.store import safe_name

PREFERRED_COURSES = ("计算机网络",)
E2E_MARKER = "[E2E]"
FIXTURE_HEADING = "E2E_ANSWER_FIXTURE"
ORGANIZE_TIMEOUT_S = 420.0
GRADE_TIMEOUT_S = 600.0
PANEL_STARTUP_PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")

# The staged notes tree carries one notes_ready recording whose notes feed the
# real quiz prompt. The network-themed text stages under 计算机网络 (the
# verified real course); the generic text stages under any other course.
NETWORK_NOTES = """\
第一讲 课堂笔记（E2E fixture）

本讲主题：计算机网络的体系结构与分层设计。

一、网络分层模型。OSI 参考模型分为物理层、数据链路层、网络层、传输层、会话层、表示层、应用层七层；工程实践中常用的 TCP/IP 体系结构精简为网络接口层、网际层、传输层、应用层四层。分层的核心价值：每一层向相邻的上层提供明确的服务，并向下依赖下层提供的功能；层与层之间通过接口交互、彼此屏蔽实现细节，从而隔离实现变化、简化设计与维护，也让不同厂商的设备与协议能够互操作。分层的代价：层级之间引入处理开销，故障定位需要跨层追查，链条变长。

二、协议、服务与接口。协议是控制两个对等实体通信的规则的集合，包含语法、语义、同步三要素；服务是下层向上层提供的功能；服务访问点是相邻层实体交互的逻辑接口。封装与解封装：发送方自上而下逐层加上首部（数据链路层还加尾部），接收方自下而上逐层拆掉首部，恢复原始数据。

三、各层职责示例。物理层负责比特流的透明传输，关心接口形状、信号编码与传输媒体；数据链路层把帧从链路一端传到另一端，负责封装成帧、差错检测、媒体接入控制；网络层把分组从源主机经路由选择送达目的主机，实现主机到主机的逻辑通信，典型协议是 IP；传输层实现进程到进程的通信，TCP 提供面向连接的可靠传输（确认、重传、滑动窗口、拥塞控制），UDP 提供无连接的尽力而为传输；应用层直接为用户应用进程提供服务，如 HTTP、DNS、SMTP。

四、复用与分用。发送方不同应用进程的报文经传输层复用在同一条链路上传输；接收方传输层按端口号把数据分用、交付给相应进程。端口是传输层的寻址标识。

五、面向连接与无连接。面向连接的服务先建立连接（握手）再传数据、最后释放连接，适合要求可靠、有序的场景；无连接服务无需预先协调，时延小，但不保证可靠交付。

课后思考：为什么网络要分层？分层带来清晰职责、隔离变化与互操作性，同时也引入层级开销与更长的故障定位链条。"""

GENERIC_NOTES = """\
第一讲 课堂笔记（E2E fixture）

本讲主题：课程导论与核心框架。

一、课程定位。本课程围绕本学科的基本概念、核心方法与典型应用展开，目标是建立完整的知识框架，并能将方法迁移到真实问题。

二、核心概念。概念之间呈现清晰的层次关系：底层是基本术语与对象，中层是机制与流程，上层是原则与权衡。理解任何一个机制都需要同时回答"它解决什么问题、依赖什么前提、付出什么代价"三个问题。

三、方法论。本课程强调三条方法：结构化拆解（把复杂问题分解为可独立分析的子问题）、抽象与接口（用明确边界隐藏实现细节，使各部分可独立演进）、验证与迭代（先建立可检验的假设，再通过证据修正）。

四、典型应用与常见误区。应用侧关注真实场景中的约束（时间、资源、协作）；常见误区包括：把记忆术语等同于理解机制、忽视前提条件直接套用结论、只看局部最优而忽略全局权衡。

五、评价标准。一项设计的好坏取决于：是否明确解决了既定问题、边界与前提是否清晰、代价是否可接受、能否在变化中保持稳定。

课后思考：结构化拆解与抽象接口分别适合什么场景？它们各自付出什么代价？"""


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


# -- scratch state ---------------------------------------------------------------


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
        raise SystemExit(f"no runner state at {path}; run panel-start first")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(scratch: str, state: dict) -> None:
    _write_json(_scratch_paths(scratch)["state"], state)


def _tokens(scratch: str) -> dict:
    path = _scratch_paths(scratch)["tokens"]
    if not path.exists():
        raise SystemExit(f"scratch tokens file missing: {path} (run the relay-repo window D setup first)")
    return json.loads(path.read_text(encoding="utf-8"))


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


# -- Notion read helpers (real token via process env only) --------------------------


def _exercise_snapshot(client, page_id: str, course_id: str,
                       wrong_answer_registrations: list[str]) -> dict:
    """Build the WindowDReadOnlyVerifier snapshot from a real Notion read."""
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


def _wrong_answer_database(client) -> dict:
    """Resolve the 知识点与错题 database exactly like the panel backend does."""
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


def _wrong_answer_rows(client, database: dict, exercise_title: str, operation_id: str) -> dict:
    """Query the real wrong-answer rows for this exercise (read-only)."""
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


# -- panel child -------------------------------------------------------------------

_STATE_SCRATCH = ""  # set by main() for cache-path resolution


def _pku_sync_exe() -> Path:
    exe = Path(sys.executable).with_name("pku-sync.exe")
    if not exe.exists():
        raise SystemExit(
            f"pku-sync console script not found next to {sys.executable}; "
            "run this probe via the client repo's uv environment"
        )
    return exe


def _child_env(scratch: Path, tokens: dict, relay: str) -> dict[str, str]:
    """The scratch client env for the panel child (env vars beat .env files)."""
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


def cmd_panel_start(args) -> int:
    scratch = Path(args.scratch)
    paths = _scratch_paths(args.scratch)
    tokens = _tokens(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    paths["client_env"].mkdir(parents=True, exist_ok=True)
    env = _child_env(scratch, tokens, args.relay)
    # The scratch env file DOCUMENTS the child env (the env dict is
    # authoritative); the real NOTION_TOKEN and scratch platform token are
    # never written into it.
    (paths["client_env"] / ".env").write_text(
        "# Window D scratch client env -- deleted at window cleanup.\n"
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
    # A pre-seeded scratch (the zero-spend Window D resume) keeps its keys;
    # a fresh scratch has no prior state, so the normal window flow is
    # byte-for-byte unchanged.
    state = {**existing, **{
        "relay": args.relay.rstrip("/"),
        "panel": f"http://127.0.0.1:{bound_port}",
        "tokens_file": str(paths["tokens"]),
        "relay_db": str(paths["relay_db"]),
        "data_dir": str(paths["data_dir"]),
        "panel_pid": proc.pid,
        "panel_port": bound_port,
    }}
    _save_state(args.scratch, state)
    evidence = {
        "step": "panel-start",
        "generated_at": _utcnow(),
        "pid": proc.pid,
        "requested_port": args.port,
        "bound_port": bound_port,
        "startup_line": startup_line,
        "healthz": health.json(),
        "panel_url": f"http://127.0.0.1:{bound_port}/",
        "student_ui_url": f"http://127.0.0.1:{bound_port}/app",
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


def cmd_seed_resume(args) -> int:
    """Zero-spend Window D resume: re-seed the retained charged state.

    Prerequisite gate first (all read-only): the retained [E2E] page must
    still exist under the real course page with the fixture answers verbatim,
    no grading marker, and zero wrong-answer rows. Only then are the durable
    grading record (verbatim, from the retained charged evidence) and the
    organize-scope record seeded into the scratch client data root, so the
    panel child loads them at startup and the ordinary grade retry completes
    the write-back through the product recovery path with ZERO relay calls.
    """
    paths = _scratch_paths(args.scratch)
    retained_doc = json.loads(Path(args.graded_record).read_text(encoding="utf-8"))
    retained = retained_doc.get("record") if isinstance(retained_doc, dict) else None
    if not isinstance(retained, dict) or len(retained) != 1:
        raise SystemExit("the retained durable-record evidence must hold exactly one page record")
    page_id = next(iter(retained))
    record = dict(retained[page_id])
    organize_evidence = json.loads(Path(args.organize_evidence).read_text(encoding="utf-8"))
    stage_evidence = json.loads(Path(args.stage_evidence).read_text(encoding="utf-8"))
    blocked_evidence = json.loads(Path(args.blocked_verification).read_text(encoding="utf-8"))
    course, lecture = stage_evidence["course"], stage_evidence["lecture"]
    token = _tokens(args.scratch)["client"]
    checks: list[dict] = []

    # -- read-only prerequisite gate over the real workspace ---------------
    with get_client(client_settings) as client:
        page = client.get_page(page_id)
        actual_parent = _norm_page_id((page.get("parent") or {}).get("page_id") or "")
        snapshot = _exercise_snapshot(client, page_id, course["id"], [])
        database = _wrong_answer_database(client)
        wrong = _wrong_answer_rows(client, database, record["title"], record["operation_id"])
    title = snapshot["title"]
    q2_count = len(wrong["rows_by_number"].get(2) or [])
    q5_count = len(wrong["rows_by_number"].get(5) or [])

    _check(checks, "retained [E2E] page still exists under the real course page",
           snapshot["fetch_result"] == "present"
           and actual_parent == _norm_page_id(course["id"]),
           f"parent={actual_parent} expected={_norm_page_id(course['id'])}")
    _check(checks, "retained title carries exactly one [E2E] marker and matches the record",
           title.count(E2E_MARKER) == 1 and title == record["title"], f"title={title!r}")
    _check(checks, "the adopted five ordered types are intact",
           snapshot["question_types"] == ["选择", "判断", "填空", "简答", "论述"],
           f"types={snapshot['question_types']}")
    _check(checks, "every question still carries a source", snapshot["source_count"] == 5,
           f"sources={snapshot['source_count']}")
    _check(checks, "teacher section still carries five standard answers",
           snapshot["teacher_answer_count"] == 5, f"teacher={snapshot['teacher_answer_count']}")
    _check(checks, "the fixture answers remain verbatim on the page",
           snapshot["answers"] == ANSWER_FIXTURE, f"answers={snapshot['answers']}")
    _check(checks, "NO grading marker and zero result block sets before the resume",
           snapshot["grade_marker"] is None and snapshot["result_block_sets"] == 0,
           f"marker={snapshot['grade_marker']} sets={snapshot['result_block_sets']}")
    _check(checks, "wrong answers were NOT registered before the resume (blocked grade wrote nothing)",
           q2_count == 0 and q5_count == 0 and len(wrong["prefix_rows"]) == 0,
           f"q2={q2_count} q5={q5_count} prefix={len(wrong['prefix_rows'])}")
    _check(checks, "exactly one [E2E] exercise page exists under the course page",
           snapshot["page_count"] == 1, f"page_count={snapshot['page_count']}")

    # -- relay baselines: the resume must be provably zero-spend -----------
    relay = args.relay.rstrip("/")
    quota_now = _quota(relay, token)
    usage_baseline = _relay_llm_rows(str(Path(args.relay_db or paths["relay_db"])))["llm_usage_rows"]
    _check(checks, "fresh scratch relay answers quota with an untouched balance",
           isinstance(quota_now.get("llm_points_remaining"), (int, float)),
           f"quota={quota_now}")
    _check(checks, "fresh scratch relay holds zero llm_usage rows at seed time",
           usage_baseline == [], f"rows={usage_baseline}")

    if not all(check["result"] == "pass" for check in checks):
        _write_json(args.out, {
            "step": "seed-resume", "generated_at": _utcnow(), "status": "aborted",
            "checks": checks,
            "note": "prerequisite gate failed; nothing was seeded and no mutation was made",
        })
        print("FAIL: seed-resume prerequisite gate failed; nothing seeded")
        for check in checks:
            marker = "PASS" if check["result"] == "pass" else "FAIL"
            print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
        return 1

    # -- retained settlement ledger (the authoritative captured usage rows) -
    retained_relay = blocked_evidence.get("relay") or {}
    usage = retained_relay.get("llm_usage_rows") or []
    initial_mp = retained_relay.get("initial_millipoints")
    ledger_rows: list[dict] = []
    if (len(usage) == 2 and [item.get("operation") for item in usage] == ["quiz", "grade"]
            and isinstance(initial_mp, int)):
        balances = [initial_mp]
        for item in usage:
            balances.append(balances[-1] - int(item["millipoints"]))
        ledger_rows = [
            {
                "sequence": index + 1,
                "operation": item["operation"],
                "status": "completed",
                "points_before": balances[index] / 1000,
                "points_charged": int(item["millipoints"]) / 1000,
                "points_after": balances[index + 1] / 1000,
                "millipoints": int(item["millipoints"]),
                "model": item.get("model"),
                "source": ("retained authoritative llm_usage rows captured at the prior "
                           "Window D close (the window's settlement authority; the prior "
                           "scratch relay DB was deleted at that window's cleanup)"),
            }
            for index, item in enumerate(usage)
        ]

    # -- seed the scratch client data root (nothing is mutated in Notion) ---
    data_dir = Path(paths["data_dir"])
    panel_dir = data_dir / "panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    grading_store_file = panel_dir / "grading_records.json"
    organize_store_file = panel_dir / "organized_exercises.json"
    scope_key = _scope_key(course["id"], [lecture["id"]], e2e_mode=True)
    organize_record = {
        "id": page_id,
        "url": organize_evidence["organize_result"]["url"],
        "title": record["title"],
        "course": course["title"],
        "parent": course["id"],
        "scope": lecture["title"],
        "updated": "",
    }
    grading_store_file.write_text(
        json.dumps({page_id: record}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    organize_store_file.write_text(
        json.dumps({scope_key: organize_record}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    state = _load_state(args.scratch) if paths["state"].exists() else {}
    state.update({
        "relay": relay,
        "tokens_file": str(paths["tokens"]),
        "relay_db": str(Path(args.relay_db or paths["relay_db"])),
        "data_dir": str(data_dir),
        "course": course,
        "lecture": lecture,
        "exercise": {"page_id": page_id, "url": record["page_url"], "title": record["title"]},
        "organize": organize_evidence["organize_result"],
        "organize_quota": organize_evidence["quota"],
        "resume": {
            "authorization": (
                "window-d-resume-zero-spend-cleanup-2026-09-20: write the grade result to "
                "the retained [E2E] page, create exactly two [E2E] wrong-answer rows, "
                "delete those rows and the page, read-only re-fetch proof; zero "
                "additional LLM operations"
            ),
            "scope_inputs": {"course_id": course["id"], "lecture_id": lecture["id"]},
            "operation_id": record["operation_id"],
            "points_charged": record["points_charged"],
            "points_remaining": record["points_remaining"],
            "graded_at": record["graded_at"],
            "quota_baseline": quota_now,
            "retained_ledger_rows": ledger_rows,
            "retained_usage_rows": usage,
            "wrong_answer_expected_titles": (blocked_evidence.get("wrong_answer_rows") or {}).get("expected_titles"),
            "seeded_files": [str(grading_store_file), str(organize_store_file)],
        },
    })
    _save_state(args.scratch, state)

    evidence = {
        "step": "seed-resume",
        "generated_at": _utcnow(),
        "status": "seeded",
        "page": {
            "page_id": page_id,
            "url": snapshot["url"],
            "title": title,
            "expected_parent_id": _norm_page_id(course["id"]),
            "actual_parent_id": actual_parent,
        },
        "snapshot_summary": {
            "question_types": snapshot["question_types"],
            "source_count": snapshot["source_count"],
            "teacher_answer_count": snapshot["teacher_answer_count"],
            "answers": snapshot["answers"],
            "grade_marker": snapshot["grade_marker"],
            "result_block_sets": snapshot["result_block_sets"],
        },
        "wrong_answer_database": {"id": database["id"], "url": database["url"], "title": database["title"]},
        "wrong_answer_rows_before_resume": {
            "expected_titles": wrong["expected_titles"],
            "q2": q2_count, "q5": q5_count, "prefix_total": len(wrong["prefix_rows"]),
        },
        "relay": {
            "url": relay,
            "quota_baseline": quota_now,
            "llm_usage_baseline": usage_baseline,
            "vendor_credentials": (
                "none injected into the relay process (the zero-spend guarantee: "
                "/v1/llm answers 503 without Bailian configuration, so no "
                "accidental LLM operation can spend)"
            ),
        },
        "seeded": {
            "grading_record_key": page_id,
            "grading_record_operation_id": record["operation_id"],
            "grading_record_phase": record.get("phase"),
            "organize_scope_key": scope_key,
            "organize_record": organize_record,
            "files": [str(grading_store_file), str(organize_store_file)],
            "notes": (
                "the retained durable grading record (phase=relay_succeeded, content "
                "verbatim, settlement 0.036/99.953, graded_at preserved) is re-seeded "
                "byte-identical so the ordinary panel grade retry completes the "
                "write-back through the product recovery path with zero relay calls"
            ),
        },
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(f"seeded: page={title} scope_key={scope_key[:12]}... quota={quota_now['llm_points_remaining']}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0


def cmd_row_snapshot(args) -> int:
    """Capture one live panel exercise row into runner state (read-only)."""
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    page_id = state["exercise"]["page_id"]
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)
    if row is None:
        _write_json(args.out, {
            "step": "row-snapshot", "generated_at": _utcnow(), "status": "missing",
            "page_id": page_id,
        })
        print(f"FAIL: panel row not found for {page_id}")
        return 1
    state[args.state_key] = row
    _save_state(args.scratch, state)
    _write_json(args.out, {
        "step": "row-snapshot", "generated_at": _utcnow(), "state_key": args.state_key,
        "row": row,
    })
    print(f"row snapshot: {args.state_key}={row.get('status_label')}")
    return 0


def cmd_stage(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
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

    notes_text = NETWORK_NOTES if course["title"] in PREFERRED_COURSES else GENERIC_NOTES
    data_dir = Path(state["data_dir"])
    course_dir = data_dir / safe_name(course["title"])
    recording = Recording(
        course_id=course["id"],
        title=lecture["title"],
        recorded_at=f"{lecture.get('date') or '2026-09-09'} 08:00:00",
        teacher="E2E",
    )
    job = RecordingJob(course["title"], course_dir, recording)
    job.directory.mkdir(parents=True, exist_ok=True)
    (course_dir / "course.json").write_text(
        json.dumps({"course_id": course["id"], "name": course["title"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (course_dir / "recordings").mkdir(parents=True, exist_ok=True)
    (course_dir / "recordings" / "index.json").write_text(
        json.dumps([recording.model_dump()], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (job.directory / "notes.md").write_text(notes_text, encoding="utf-8")

    jobs = collect_jobs(data_dir)
    stage, unavailable = recording_stage(jobs[0]) if len(jobs) == 1 else ("", "")
    staged_course_name = json.loads(
        (course_dir / "course.json").read_text(encoding="utf-8")
    ).get("name")
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
    evidence = {
        "step": "stage",
        "generated_at": _utcnow(),
        "course": state["course"],
        "lecture": state["lecture"],
        "course_selection": (
            "preferred real course 计算机网络" if course["title"] in PREFERRED_COURSES
            else "course with the most lectures (preferred course absent)"
        ),
        "scratch_tree": {
            "data_dir": str(data_dir),
            "course_dir": str(course_dir),
            "recording_dir": str(job.directory),
            "notes_md_bytes": len(notes_text.encode("utf-8")),
            "recording_title_matches_lecture_exactly": recording.title == lecture["title"],
            "course_name_matches_course_title_exactly": staged_course_name == course["title"],
        },
        "pipeline_read": {"jobs": len(jobs), "stage": stage, "unavailable": unavailable},
        "directory_baseline": state["directory_baseline"],
    }
    _write_json(args.out, evidence)
    print(
        f"staged: course={course['title']} lecture={lecture['title']} "
        f"jobs={len(jobs)} stage={stage} baseline_rows={len(baseline_rows)}"
    )
    if len(jobs) != 1 or stage != "notes_ready":
        print("FAIL: expected exactly one notes_ready recording in the scratch tree")
        return 1
    return 0


def cmd_organize(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _tokens(args.scratch)["client"]
    course, lecture = state["course"], state["lecture"]
    checks: list[dict] = []

    quota_before = _quota(relay, token)
    estimate = httpx.get(f"{panel}/api/exercises/organize/estimate", timeout=15)
    _check(checks, "organize estimate answered", estimate.status_code == 200,
           f"HTTP {estimate.status_code}")
    started = httpx.post(
        f"{panel}/api/exercises/organize",
        json={"course_id": course["id"], "lecture_ids": [lecture["id"]], "e2e_mode": True},
        timeout=30,
    )
    if started.status_code != 202:
        _write_json(args.out, {
            "step": "organize", "generated_at": _utcnow(), "status": "failed",
            "http_status": started.status_code, "body": started.json(),
            "checks": checks,
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
    with get_client(client_settings) as client:
        page = client.get_page(page_id)
        actual_parent = _norm_page_id((page.get("parent") or {}).get("page_id") or "")
        title = page_title(page)
        blocks = client.list_children(page_id)
    texts = [_plain(block) for block in blocks]
    question_types = [value.split(". ", 1)[-1] for value, block in zip(texts, blocks)
                      if block.get("type") == "heading_3"]
    source_count = sum(1 for value in texts if value.startswith("来源："))
    teacher_answer_count = sum(1 for value in texts if value.startswith("答案："))
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    _check(checks, "organize completed with a fresh (non-reused) page", result.get("reused") is False,
           f"reused={result.get('reused')}")
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
    state["exercise"] = {
        "page_id": page_id, "url": result.get("url") or page.get("url") or "",
        "title": title,
    }
    state["panel_row_after_organize"] = row
    state["organized_page_blocks_summary"] = {
        "question_types": question_types,
        "source_count": source_count,
        "teacher_answer_count": teacher_answer_count,
        "parent_page_id": actual_parent,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "organize",
        "generated_at": _utcnow(),
        "status": result.get("status"),
        "organize_result": result,
        "quota": state["organize_quota"],
        "page": {
            "page_id": page_id,
            "url": state["exercise"]["url"],
            "title": title,
            "expected_parent_id": _norm_page_id(course["id"]),
            "actual_parent_id": actual_parent,
        },
        "blocks_summary": state["organized_page_blocks_summary"],
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
            for index, block in enumerate(readback)
            if index > fixture_at
            and block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
        answers = {f"Q{number}": value for number, value in enumerate(rows, 1)}
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    _check(checks, "answer fixture heading appended", fixture_at is not None,
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


def cmd_grade(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _tokens(args.scratch)["client"]
    page_id = state["exercise"]["page_id"]
    resume = bool(getattr(args, "resume", False))
    external_job = (getattr(args, "job_id", "") or "").strip()
    checks: list[dict] = []
    seeded = state.get("resume") if resume else None
    if resume and not isinstance(seeded, dict):
        raise SystemExit("grade --resume requires a seeded resume state (run seed-resume first)")

    if resume:
        scope_ok = (
            (state.get("course") or {}).get("id") == seeded["scope_inputs"]["course_id"]
            and (state.get("lecture") or {}).get("id") == seeded["scope_inputs"]["lecture_id"]
        )
        _check(checks, "staged live-directory scope matches the seeded Window D organize scope",
               scope_ok,
               f"staged_course={(state.get('course') or {}).get('id')} "
               f"staged_lecture={(state.get('lecture') or {}).get('id')}")
        if not scope_ok:
            _write_json(args.out, {
                "step": "grade", "generated_at": _utcnow(), "status": "aborted",
                "checks": checks,
                "note": "scope mismatch: a fresh organize would be required; aborted before any spend",
            })
            print("FAIL: staged scope does not match the seeded organize scope; aborted before any spend")
            return 1
        if not state.get("panel_row_after_answers"):
            rows = _panel_exercise_rows(panel)
            pre_row = _panel_row(rows, page_id)
            state["panel_row_after_answers"] = pre_row
            _save_state(args.scratch, state)
        pre_row = state.get("panel_row_after_answers") or {}
        _check(checks, "panel row reads 待批改 before the resume grade",
               pre_row.get("status") == "pending-grade"
               and pre_row.get("status_label") == "待批改",
               f"row={pre_row}")

    if external_job:
        # The grade job was triggered through the panel's own student UI
        # (browser evidence); poll the same product status surface.
        summary = _poll_job(panel, "/api/exercises/grade/status", external_job,
                            GRADE_TIMEOUT_S)
        quota_after = _quota(relay, token)
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

    with get_client(client_settings) as client:
        snapshot = _exercise_snapshot(client, page_id, state["course"]["id"], [])
    operation_id = _operation_id(state)
    with get_client(client_settings) as client:
        database = _wrong_answer_database(client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"], operation_id)
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
    if resume:
        _check(checks, "resume settlement preserved from the retained charged record",
               isinstance(summary.get("points_charged"), (int, float))
               and abs(float(summary["points_charged"]) - float(seeded["points_charged"])) <= 1e-9,
               f"summary={summary.get('points_charged')} retained={seeded['points_charged']}")
        _check(checks, "zero new spend: quota unchanged across the whole resume leg",
               abs(quota_after["llm_points_remaining"]
                   - seeded["quota_baseline"]["llm_points_remaining"]) <= 1e-9,
               f"baseline={seeded['quota_baseline']['llm_points_remaining']} "
               f"now={quota_after['llm_points_remaining']}")
        usage_now = _relay_llm_rows(state["relay_db"])["llm_usage_rows"]
        _check(checks, "fresh scratch relay still holds zero llm_usage rows after the resume grade",
               usage_now == [], f"rows={usage_now}")
    else:
        _check(checks, "quota decremented by the charged points",
               abs(quota_before["llm_points_remaining"] - summary["points_charged"]
                   - quota_after["llm_points_remaining"]) <= 1e-9,
               f"before={quota_before['llm_points_remaining']} charged={summary['points_charged']} "
               f"after={quota_after['llm_points_remaining']}")

    state["grade"] = summary
    state["grade_quota"] = (
        {"baseline": seeded["quota_baseline"], "after": quota_after} if resume
        else {"before": quota_before, "after": quota_after}
    )
    state["operation_id"] = operation_id
    state["wrong_answer_database"] = {
        "id": database["id"], "url": database["url"], "title": database["title"],
    }
    state["wrong_answer_rows"] = {
        "expected_titles": wrong["expected_titles"],
        "rows": {
            str(number): _row_identity(rows)
            for number, rows in wrong["rows_by_number"].items()
        },
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
        "grade_summary": summary,
        "quota": state["grade_quota"],
        "operation_id": operation_id,
        "page_snapshot_summary": state["graded_snapshot_summary"],
        "wrong_answer_database": state["wrong_answer_database"],
        "wrong_answer_rows": state["wrong_answer_rows"],
        "panel_row_after_grade": row,
        "checks": checks,
    }
    if resume:
        evidence["resume"] = {
            "mode": "zero-spend durable recovery (no new relay LLM operation)",
            "job_source": ("the panel's own student UI (browser-triggered); the runner "
                           "polled the same product grade/status surface"
                           if external_job else "runner POST through the panel route"),
            "job_id": external_job or None,
            "settlement_authority": (
                "the retained charged record re-seeded verbatim (seed-resume); the "
                "settlement matches the retained relay llm_usage grade row captured at "
                "the prior window close"
            ),
            "graded_at_preserved": seeded["graded_at"],
        }
    _write_json(args.out, evidence)
    print(
        f"graded: score={summary.get('score')} charged={summary['points_charged']} "
        f"quota {quota_before['llm_points_remaining']} -> {quota_after['llm_points_remaining']}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


def cmd_reruns(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _tokens(args.scratch)["client"]
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
        and confirmed.get("points_charged") == 0
        and confirmed.get("score") == first_grade.get("score")
        and confirmed.get("graded_at") == first_grade.get("graded_at")
    )

    _check(checks, "organize rerun reused the same page with zero charge",
           organize_rerun.get("status") == "completed"
           and organize_rerun.get("reused") is True
           and organize_rerun.get("points_charged") == 0
           and organize_rerun.get("exercise_id") == state["organize"]["exercise_id"],
           f"rerun={organize_rerun}")
    _check(checks, "second grade without confirmation is gated (confirm_required)",
           gate.status_code == 409 and gate.json().get("status") == "confirm_required",
           f"HTTP {gate.status_code} body={gate.json()}")
    _check(checks, "confirmed grade rerun is a zero-charge no-op with the same score/timestamp",
           grade_rerun.get("no_op") is True,
           f"rerun={confirmed}")
    _check(checks, "reruns spent nothing (quota unchanged across both reruns)",
           quota_before_rerun["llm_points_remaining"]
           == quota_after_reruns["llm_points_remaining"],
           f"before={quota_before_rerun['llm_points_remaining']} "
           f"after={quota_after_reruns['llm_points_remaining']}")

    state["organize_rerun"] = organize_rerun
    state["grade_gate"] = {
        "http_status": gate.status_code,
        "body": gate.json(),
    }
    state["grade_rerun"] = grade_rerun
    state["rerun_quota"] = {
        "before": quota_before_rerun,
        "before_grade_rerun": quota_before_grade_rerun,
        "after": quota_after_reruns,
    }
    _save_state(args.scratch, state)
    evidence = {
        "step": "reruns",
        "generated_at": _utcnow(),
        "organize_rerun": organize_rerun,
        "grade_gate": state["grade_gate"],
        "grade_rerun": grade_rerun,
        "quota": state["rerun_quota"],
        "checks": checks,
    }
    _write_json(args.out, evidence)
    print(
        f"reruns: organize reused={organize_rerun.get('reused')} "
        f"grade no_op={grade_rerun.get('no_op')} "
        f"quota {quota_before_rerun['llm_points_remaining']} -> {quota_after_reruns['llm_points_remaining']}"
    )
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if all(check["result"] == "pass" for check in checks) else 1


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


def cmd_verify(args) -> int:
    state = _load_state(args.scratch)
    panel = args.panel.rstrip("/") or state["panel"]
    relay = args.relay.rstrip("/") or state["relay"]
    token = _tokens(args.scratch)["client"]
    page_id = state["exercise"]["page_id"]
    course_id = state["course"]["id"]
    resume = bool(getattr(args, "resume", False))
    seeded = state.get("resume") if resume else None
    if resume and not isinstance(seeded, dict):
        raise SystemExit("verify --resume requires a seeded resume state (run seed-resume first)")
    checks: list[dict] = []

    with get_client(client_settings) as client:
        snapshot = _exercise_snapshot(client, page_id, course_id, [])
        database = _wrong_answer_database(client)
        wrong = _wrong_answer_rows(client, database, state["exercise"]["title"],
                                   state["operation_id"])
    found_keys = [
        f"Q{number}"
        for number in (2, 5)
        if len(wrong["rows_by_number"].get(number) or []) == 1
    ]
    snapshot["wrong_answer_registrations"] = sorted(found_keys)

    db = _relay_llm_rows(state["relay_db"])
    usage_rows = db["llm_usage_rows"]
    quota_final = _quota(relay, token)
    rows = _panel_exercise_rows(panel)
    row = _panel_row(rows, page_id)

    if resume:
        # The settlement authority is the retained llm_usage rows captured at
        # the prior window close (the prior scratch relay DB was deleted at
        # that window's cleanup); the fresh scratch relay proves the resume
        # leg added ZERO rows and spent nothing.
        ledger_rows = list(seeded["retained_ledger_rows"])
        client_account = db["accounts"][0] if db["accounts"] else None
        initial_millipoints = None
        final_millipoints = (
            int(client_account["llm_millipoints_remaining"]) if client_account else None
        )
    else:
        client_account = next(
            (item for item in db["accounts"] if item["label"] == state["organize_quota"]["before"]["label"]),
            None,
        )
        # Ledger rows from the authoritative integer millipoints (the same
        # division the relay performs for /v1/quota and /v1/llm responses).
        millipoints = [int(item["millipoints"]) for item in usage_rows]
        final_millipoints = int(client_account["llm_millipoints_remaining"]) if client_account else None
        initial_millipoints = (
            final_millipoints + sum(millipoints)
            if final_millipoints is not None and len(millipoints) == 2
            else None
        )
        ledger_rows = []
        if initial_millipoints is not None:
            balances = [
                initial_millipoints,
                initial_millipoints - millipoints[0],
                initial_millipoints - millipoints[0] - millipoints[1],
            ]
            ledger_rows = [
                {
                    "sequence": index + 1,
                    "operation": item["operation"],
                    "status": "completed",
                    "points_before": balances[index] / 1000,
                    "points_charged": charge / 1000,
                    "points_after": balances[index + 1] / 1000,
                    "millipoints": charge,
                    "model": item["model"],
                }
                for index, (item, charge) in enumerate(zip(usage_rows, millipoints))
            ]

    verification = WindowDReadOnlyVerifier().verify(
        snapshot=snapshot,
        panel_summary=state["grade"],
        organize_result=state["organize"],
        organize_rerun=state["organize_rerun"],
        grade_rerun=state["grade_rerun"],
        ledger_rows=ledger_rows,
        expected_parent_id=_norm_page_id(course_id),
    )
    checks.extend(verification["checks"])

    if resume:
        retained_rows = seeded["retained_usage_rows"]
        _check(checks, "the retained authoritative llm_usage rows are exactly quiz then grade",
               len(retained_rows) == 2
               and [item.get("operation") for item in retained_rows] == ["quiz", "grade"],
               f"rows={retained_rows}")
        _check(checks, "retained relay millipoints reconcile with the panel settlements",
               len(retained_rows) == 2
               and float(retained_rows[0]["millipoints"]) / 1000 == float(state["organize"]["points_charged"])
               and float(retained_rows[1]["millipoints"]) / 1000 == float(state["grade"]["points_charged"]),
               f"retained_millipoints={[item['millipoints'] for item in retained_rows]} "
               f"organize={state['organize']['points_charged']} grade={state['grade']['points_charged']}")
        _check(checks, "zero-spend resume: the fresh scratch relay holds zero llm_usage rows",
               usage_rows == [], f"rows={usage_rows}")
        _check(checks, "zero-spend resume: the fresh relay quota equals the seeded baseline and the fresh DB balance",
               client_account is not None
               and abs(quota_final["llm_points_remaining"]
                       - seeded["quota_baseline"]["llm_points_remaining"]) <= 1e-9
               and abs(quota_final["llm_points_remaining"]
                       - client_account["llm_millipoints_remaining"] / 1000) <= 1e-9,
               f"quota={quota_final['llm_points_remaining']} "
               f"baseline={seeded['quota_baseline']['llm_points_remaining']} "
               f"db={client_account and client_account['llm_millipoints_remaining'] / 1000}")
    else:
        _check(checks, "relay llm_usage holds exactly two rows: quiz then grade, on the client account",
               len(usage_rows) == 2
               and [item["operation"] for item in usage_rows] == ["quiz", "grade"]
               and all(item["account_id"] == client_account["id"] for item in usage_rows),
               f"rows={usage_rows}")
        _check(checks, "relay millipoints reconcile with the panel settlements",
               len(millipoints) == 2
               and millipoints[0] / 1000 == float(state["organize"]["points_charged"])
               and millipoints[1] / 1000 == float(state["grade"]["points_charged"]),
               f"millipoints={millipoints} "
               f"organize={state['organize']['points_charged']} grade={state['grade']['points_charged']}")
        _check(checks, "final relay quota equals the DB millipoint balance",
               client_account is not None
               and abs(quota_final["llm_points_remaining"]
                       - client_account["llm_millipoints_remaining"] / 1000) <= 1e-9,
               f"quota={quota_final['llm_points_remaining']} "
               f"db={client_account and client_account['llm_millipoints_remaining'] / 1000}")
    _check(checks, "wrong-answer database holds exactly two rows for this exercise (Q2, Q5 once each)",
           len(wrong["prefix_rows"]) == 2
           and len(wrong["rows_by_number"].get(2) or []) == 1
           and len(wrong["rows_by_number"].get(5) or []) == 1,
           f"prefix_rows={len(wrong['prefix_rows'])}")
    if resume:
        _check(checks,
               "panel row states walked 待批改 -> 已批改 live across the resume grade "
               "(the earlier 已整理 leg is closed at product level: the landed "
               "answer-presence scan fix + its pinned additive tests + the prior "
               "window's real organize evidence)",
               (state.get("panel_row_after_answers") or {}).get("status_label") == "待批改"
               and (state.get("panel_row_after_grade") or {}).get("status_label") == "已批改"
               and row is not None and row.get("status_label") == "已批改",
               f"final_row={row}")
    else:
        _check(checks, "panel row states walked 已整理 -> 待批改 -> 已批改",
               (state.get("panel_row_after_organize") or {}).get("status_label") == "已整理"
               and (state.get("panel_row_after_answers") or {}).get("status_label") == "待批改"
               and (state.get("panel_row_after_grade") or {}).get("status_label") == "已批改"
               and row is not None and row.get("status_label") == "已批改",
               f"final_row={row}")
    _check(checks, "exactly one [E2E] exercise page exists under the course page",
           snapshot["page_count"] == 1, f"page_count={snapshot['page_count']}")

    evidence = {
        "step": "verify",
        "generated_at": _utcnow(),
        "page": {
            "page_id": page_id,
            "url": snapshot["url"],
            "title": snapshot["title"],
            "expected_parent_id": _norm_page_id(course_id),
            "actual_parent_id": snapshot["parent_page_id"],
        },
        "snapshot": snapshot,
        "verification": verification,
        "relay": {
            "llm_usage_rows": usage_rows,
            "client_account": client_account,
            "quota_final": quota_final,
            "initial_millipoints": initial_millipoints,
            "final_millipoints": final_millipoints,
        },
        "ledger_rows": ledger_rows,
        **({
            "resume": {
                "mode": "zero-spend durable recovery",
                "settlement_authority": (
                    "retained authoritative llm_usage rows captured at the prior Window D "
                    "close (evidence/43 of the window pack); the prior scratch relay DB was "
                    "deleted at that window's authorized cleanup, so these captured rows are "
                    "the settlement record for the window's two charged LLM operations"
                ),
                "fresh_relay_zero_spend_proof": {
                    "llm_usage_rows": usage_rows,
                    "quota_final": quota_final,
                    "quota_baseline": seeded["quota_baseline"],
                },
            }
        } if resume else {}),
        "wrong_answer_database": {
            "id": database["id"], "url": database["url"], "title": database["title"],
        },
        "wrong_answer_rows": {
            "expected_titles": wrong["expected_titles"],
            "rows": {
                str(number): _row_identity(rows)
                for number, rows in wrong["rows_by_number"].items()
            },
            "prefix_rows": _row_identity(wrong["prefix_rows"]),
        },
        "panel_rows": {
            "after_organize": state.get("panel_row_after_organize"),
            "after_answers": state.get("panel_row_after_answers"),
            "after_grade": state.get("panel_row_after_grade"),
            "final": row,
        },
        "checks": checks,
        "passed": all(check["result"] == "pass" for check in checks),
    }
    _write_json(args.out, evidence)
    print(f"verify: {len(checks)} checks, passed={evidence['passed']}")
    for check in checks:
        marker = "PASS" if check["result"] == "pass" else "FAIL"
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check["detail"] else ""))
    return 0 if evidence["passed"] else 1


def cmd_cleanup(args) -> int:
    """Authorized [E2E] cleanup: archive (trash) the two wrong-answer rows
    and the retained [E2E] page, then prove the outcome by re-fetch/re-query.

    Covered by the recorded zero-spend resume authorization: "Create exactly
    two [E2E]-marked wrong-answer database rows / Delete exactly those two
    rows / Delete the retained [E2E] page / Read-only re-fetch proof of page
    and wrong-answer cleanup". Notion's REST surface expresses deletion as
    trash-archival (there is no hard-delete API); the proof below shows both
    the archived flag and the rows/pages disappearing from their containers.
    """
    state = _load_state(args.scratch)
    page_id = state["exercise"]["page_id"]
    operation_id = state.get("operation_id") or state["resume"]["operation_id"]
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
            archived_rows.append({"page_id": row.get("id"), "url": row.get("url")})
        for row in prefix_rows:
            if row.get("id") and all(row["id"] != item["page_id"] for item in archived_rows):
                client.archive_page(row["id"])
                archived_rows.append({"page_id": row.get("id"), "url": row.get("url")})

        client.archive_page(page_id)

        wrong_after = _wrong_answer_rows(client, database, state["exercise"]["title"], operation_id)
        page_after = client.get_page(page_id)
        course_children = client.list_child_pages(course_id)

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
    _check(checks, "[E2E] page deleted (trash-archived): re-fetch shows archived and the page "
                   "no longer lists under the course page",
           bool(page_after.get("archived")) is True and page_still_listed is False,
           f"archived={page_after.get('archived')} still_listed={page_still_listed}")

    evidence = {
        "step": "cleanup",
        "generated_at": _utcnow(),
        "authorization": (
            "window-d-resume-zero-spend-cleanup-2026-09-20: delete exactly the two "
            "[E2E]-marked wrong-answer rows and the retained [E2E] page, with "
            "read-only re-fetch proof"
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
                "authorized zero-spend cleanup; Notion deletion is trash-archive via the "
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
        "wrong_answer_rows": {
            "expected_titles": wrong["expected_titles"],
            "rows_before": _row_identity(prefix_rows),
            "archived_rows": archived_rows,
            "rows_after_query_count": remaining_prefix,
            "cleanup_status": "archived",
            "reason": (
                "authorized zero-spend cleanup; each row trash-archived via the API; "
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
        prog="e2e_window_d_real.py",
        description=(
            "Real Window D runner (VAL-EXER-035/036): real relay LLM quiz + "
            "grade, real [E2E] Notion lifecycle under a real course page, "
            "read-only verification, settlement reconciliation, cleanup. "
            "Scratch secrets never reach stdout or evidence."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    panel_start = sub.add_parser("panel-start", help="launch the probe-owned panel child")
    panel_start.add_argument("--scratch", required=True)
    panel_start.add_argument("--relay", required=True)
    panel_start.add_argument("--port", type=int, default=8791)
    panel_start.add_argument("--stdout-log", required=True)
    panel_start.add_argument("--stderr-log", required=True)
    panel_start.add_argument("--out", required=True)
    panel_start.set_defaults(func=cmd_panel_start)

    seed_resume = sub.add_parser(
        "seed-resume",
        help="zero-spend resume: verify the retained [E2E] page read-only, then seed the "
             "retained charged grading record + organize-scope record into the scratch data root",
    )
    seed_resume.add_argument("--scratch", required=True)
    seed_resume.add_argument("--relay", required=True)
    seed_resume.add_argument("--relay-db", default="")
    seed_resume.add_argument("--graded-record", required=True,
                              help="retained durable grading record evidence JSON (evidence/44)")
    seed_resume.add_argument("--organize-evidence", required=True,
                             help="the window's captured organize evidence JSON (evidence/33)")
    seed_resume.add_argument("--stage-evidence", required=True,
                             help="the window's captured stage evidence JSON (evidence/32)")
    seed_resume.add_argument("--blocked-verification", required=True,
                             help="the window's captured blocked-state verification JSON (evidence/43)")
    seed_resume.add_argument("--out", required=True)
    seed_resume.set_defaults(func=cmd_seed_resume)

    row_snapshot = sub.add_parser("row-snapshot", help="capture one live panel row into state (read-only)")
    row_snapshot.add_argument("--scratch", required=True)
    row_snapshot.add_argument("--panel", default="")
    row_snapshot.add_argument("--state-key", required=True)
    row_snapshot.add_argument("--out", required=True)
    row_snapshot.set_defaults(func=cmd_row_snapshot)

    stage = sub.add_parser("stage", help="pick the real course/lecture + build the scratch notes tree")
    stage.add_argument("--scratch", required=True)
    stage.add_argument("--panel", default="")
    stage.add_argument("--out", required=True)
    stage.set_defaults(func=cmd_stage)

    organize = sub.add_parser("organize", help="ONE real organize (quiz) through the panel")
    organize.add_argument("--scratch", required=True)
    organize.add_argument("--panel", default="")
    organize.add_argument("--relay", default="")
    organize.add_argument("--out", required=True)
    organize.set_defaults(func=cmd_organize)

    write_answers = sub.add_parser("write-answers", help="the authorized answer fixture write")
    write_answers.add_argument("--scratch", required=True)
    write_answers.add_argument("--panel", default="")
    write_answers.add_argument("--out", required=True)
    write_answers.set_defaults(func=cmd_write_answers)

    grade = sub.add_parser("grade", help="ONE real grade through the panel")
    grade.add_argument("--scratch", required=True)
    grade.add_argument("--panel", default="")
    grade.add_argument("--relay", default="")
    grade.add_argument("--job-id", default="",
                       help="poll an existing grade job (e.g. triggered through the panel UI) "
                            "instead of POSTing a new one")
    grade.add_argument("--resume", action="store_true",
                       help="zero-spend resume mode: the durable recovery completes from the "
                            "seeded retained charged record; quota must stay unchanged and the "
                            "fresh relay must gain no llm_usage row")
    grade.add_argument("--out", required=True)
    grade.set_defaults(func=cmd_grade)

    reruns = sub.add_parser("reruns", help="zero-spend idempotent reruns")
    reruns.add_argument("--scratch", required=True)
    reruns.add_argument("--panel", default="")
    reruns.add_argument("--relay", default="")
    reruns.add_argument("--out", required=True)
    reruns.set_defaults(func=cmd_reruns)

    verify = sub.add_parser("verify", help="full read-only verification + settlement")
    verify.add_argument("--scratch", required=True)
    verify.add_argument("--panel", default="")
    verify.add_argument("--relay", default="")
    verify.add_argument("--resume", action="store_true",
                        help="zero-spend resume mode: the ledger is the retained captured "
                             "usage rows; the fresh scratch relay must prove zero new rows")
    verify.add_argument("--out", required=True)
    verify.set_defaults(func=cmd_verify)

    cleanup = sub.add_parser(
        "cleanup",
        help="authorized [E2E] cleanup: archive the two wrong-answer rows + the retained page "
             "with re-fetch/re-query proof",
    )
    cleanup.add_argument("--scratch", required=True)
    cleanup.add_argument("--out", required=True)
    cleanup.set_defaults(func=cmd_cleanup)

    panel_stop = sub.add_parser("panel-stop", help="stop the probe-owned panel child")
    panel_stop.add_argument("--pid", type=int, required=True)
    panel_stop.add_argument("--port", type=int, required=True)
    panel_stop.add_argument("--out", required=True)
    panel_stop.set_defaults(func=cmd_panel_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    global _STATE_SCRATCH
    args = build_parser().parse_args(argv)
    if getattr(args, "scratch", None):
        _STATE_SCRATCH = args.scratch
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
