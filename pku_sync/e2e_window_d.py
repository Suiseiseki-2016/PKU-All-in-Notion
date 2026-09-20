"""Reusable fake-only Window D panel driver, verifier, evidence, and cleanup.

This module never constructs a real Notion client or a network relay.  The
panel HTTP controllers are real; their Notion and relay boundaries are
probe-owned in-memory fakes.  The later attended Window D runner can reuse the
same driver/verifier contracts while supplying real boundary adapters.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from .notion import markdown_to_blocks
from .notion_meta.entities import canonical_url
from .panel.exercise_grader import (
    E2E_GRADE_QUESTION_RUBRIC,
    GRADE_RESULT_CONTRACT_VERSION,
    ExerciseGrader,
    GradeBlocked,
    GradingInput,
    _fingerprint,
    _grading_prompt,
    _plain,
)
from .panel.exercise_organizer import ExerciseOrganizer, MemoryOrganizeRecordStore, _prompt
from .panel.fake_directory import build_fake_directory
from .panel.fake_workspace import COURSE_NET, NET_L1
from .panel.webapi import create_app

QUESTION_FIXTURE = tuple(row.provider_schema() for row in E2E_GRADE_QUESTION_RUBRIC)
ANSWER_FIXTURE = {
    "Q1": "A",
    "Q2": "错误（fixture 故意作答为正确）",
    "Q3": "协议层；错误的第二空",
    "Q4": "分层明确职责和接口，隔离实现变化并简化维护。",
    "Q5": "只讨论模块化，故意遗漏互操作性和故障定位。",
}
# Real relay settlements are millipoints/1000 floats; subtracting a charged
# value from a before-balance is not always bit-identical to the after
# balance's division result, so ledger arithmetic compares within this
# tolerance instead of exact float equality.
LEDGER_FLOAT_TOLERANCE = 1e-9


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()



def _check(checks: list[dict[str, str]], name: str, passed: bool, detail: str = "") -> None:
    checks.append({"name": name, "result": "pass" if passed else "fail", "detail": detail})


class FakeWindowDRelay:
    """Deterministic two-operation relay with a read-only usage ledger."""

    def __init__(
        self,
        points: float = 20.0,
        charge: float = 2.0,
        *,
        ledger_path: Path | None = None,
    ):
        self._points = points
        self._charge = charge
        self._ledger: list[dict[str, Any]] = []
        self._ledger_path = ledger_path
        self._write_ledger()

    def quota(self) -> dict[str, Any]:
        return {"available": True, "llm_points_remaining": self._points}

    def _record(self, operation: str) -> None:
        before = self._points
        self._points -= self._charge
        self._ledger.append({
            "sequence": len(self._ledger) + 1,
            "operation": operation,
            "status": "completed",
            "points_before": before,
            "points_charged": self._charge,
            "points_after": self._points,
        })
        self._write_ledger()

    def _write_ledger(self) -> None:
        if self._ledger_path is not None:
            self._ledger_path.write_text(
                json.dumps(self._ledger, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def quiz(self, _provider_prompt: str) -> dict[str, Any]:
        self._record("quiz")
        questions = [
            {
                "type": item["type"],
                "question": f"Window D {item['type']}题",
                "source": "第一讲",
                "answer": f"教师答案 {item['question_id']}",
            }
            for item in QUESTION_FIXTURE
        ]
        return {
            "content": json.dumps(
                {"title": "第一讲 · Window D 练习", "questions": questions},
                ensure_ascii=False,
            ),
            "points_charged": self._charge,
        }

    def grade(self, provider_prompt: str) -> dict[str, Any]:
        prompt = json.loads(provider_prompt)
        contract = prompt.get("response_contract") or {}
        if contract.get("version") != GRADE_RESULT_CONTRACT_VERSION:
            raise AssertionError("strict Window D grade contract was not requested")
        self._record("grade")
        questions = []
        for item in QUESTION_FIXTURE:
            questions.append({
                "question_id": item["question_id"],
                "number": item["number"],
                "type": item["type"],
                "max_score": item["max_score"],
                "expected_score": item["expected_score"],
                "expected_outcome": item["expected_outcome"],
                "score": item["expected_score"],
                "outcome": item["expected_outcome"],
                "register_wrong": item["register_wrong"],
                "wrong_answer_key": item["question_id"],
                "feedback": f"{item['question_id']} deterministic feedback",
            })
        return {
            "content": json.dumps({
                "contract_version": GRADE_RESULT_CONTRACT_VERSION,
                "score": 50,
                "earned_points": 5,
                "max_points": 10,
                "questions": questions,
            }, ensure_ascii=False),
            "points_charged": self._charge,
        }

    def read_ledger(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._ledger)


class FakeWindowDNotion:
    """One probe-owned page store with explicit mutation and read APIs."""

    page_id = "2c000001-0000-4000-8000-000000000904"

    def __init__(self):
        self.pages: dict[str, dict[str, Any]] = {}
        self.wrong_answers: set[str] = set()
        self.wrong_answer_operations: set[str] = set()
        self.result_operation_id = ""
        self.mutation_count = 0

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]) -> dict:
        if self.pages:
            raise AssertionError("Window D fixture permits exactly one page")
        self.pages[self.page_id] = {
            "id": self.page_id,
            "url": canonical_url(self.page_id),
            "parent_page_id": parent_page_id,
            "title": title,
            "children": copy.deepcopy(children),
            "answers": {},
            "answer_blocks": [],
            "grade_result": None,
            "result_block_sets": 0,
            "deleted": False,
        }
        self.mutation_count += 1
        return {"id": self.page_id, "url": self.pages[self.page_id]["url"], "last_edited_time": _now()}

    def write_answers(self, page_id: str, answers: dict[str, str]) -> None:
        page = self.pages[page_id]
        if tuple(answers) != tuple(ANSWER_FIXTURE) or any(not value.strip() for value in answers.values()):
            raise AssertionError("answer fixture must contain deterministic Q1-Q5 values")
        page["answers"] = dict(answers)
        page["answer_blocks"] = markdown_to_blocks(
            "## E2E_ANSWER_FIXTURE\n\n" + "\n\n".join(
                f"{index}. {answers[f'Q{index}']}" for index in range(1, 6)
            )
        )
        page["children"].extend(copy.deepcopy(page["answer_blocks"]))
        self.mutation_count += 1

    def preflight_wrong_answer_database(self) -> None:
        return None

    def verify_parent(self, target) -> None:
        if self.pages[target.page_id]["parent_page_id"] != target.course_id:
            raise GradeBlocked("练习页父级不匹配。")

    def read_for_grading(self, target) -> GradingInput:
        page = self.pages[target.page_id]
        blocks = page["children"]
        teacher_at = next((i for i, block in enumerate(blocks) if _plain(block) == "教师区（答案）"), len(blocks))
        provider_blocks = copy.deepcopy(blocks[:teacher_at] + page["answer_blocks"])
        prompt_payload = json.loads(_grading_prompt(target, provider_blocks)[0])
        return GradingInput(
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            unanswered=[],
            marker_present=page["grade_result"] is not None,
            existing_result=copy.deepcopy(page["grade_result"]),
            answer_fingerprint=_fingerprint(list(page["answers"].values())),
            answer_provenance="known",
            result_contract=GRADE_RESULT_CONTRACT_VERSION,
            result_marker="E2E_GRADE_RESULT",
        )

    def write_result(self, target, *, content: str, score: float, graded_at: str) -> None:
        page = self.pages[target.page_id]
        page["grade_result"] = {
            "status": "completed",
            "exercise_id": target.page_id,
            "title": target.title,
            "score": score,
            "graded_at": graded_at,
            "points_charged": 0,
            "points_remaining": None,
            "result_page_url": target.page_url,
            "content": json.loads(content),
            "answer_fingerprint": _fingerprint(list(page["answers"].values())),
            "marker": "E2E_GRADE_RESULT",
        }
        page["result_block_sets"] += 1
        self.mutation_count += 1

    def update_result(self, target, *, content: str, score: float, graded_at: str) -> None:
        self.write_result(target, content=content, score=score, graded_at=graded_at)

    def ensure_result(
        self,
        target,
        *,
        operation_id: str,
        content: str,
        score: float,
        graded_at: str,
        regrade: bool,
        **_kwargs,
    ) -> None:
        page = self.pages[target.page_id]
        if page["grade_result"] is None:
            self.result_operation_id = operation_id
            self.write_result(target, content=content, score=score, graded_at=graded_at)
            return
        if self.result_operation_id != operation_id or regrade:
            raise GradeBlocked("fake result reconciliation found an unexpected duplicate")

    def verify_result(
        self,
        target,
        *,
        operation_id: str,
        content: str,
        score: float,
        graded_at: str,
        **_kwargs,
    ) -> bool:
        page = self.pages[target.page_id]
        result = page["grade_result"]
        return bool(
            result
            and self.result_operation_id == operation_id
            and result["content"] == json.loads(content)
            and result["score"] == score
            and result["graded_at"] == graded_at
        )

    def result_cleanup_plan(self, _target, **_kwargs) -> list[str]:
        return []

    def archive_result_block(self, _target, *, block_id: str) -> None:
        raise AssertionError(f"fake fixture has no stale result block to archive: {block_id}")

    def ensure_wrong_answer(
        self,
        _target,
        question: dict[str, Any],
        *,
        key: str,
        operation_id: str,
    ) -> None:
        if key in self.wrong_answer_operations:
            return
        self.wrong_answer_operations.add(key)
        self.wrong_answers.add(str(question["wrong_answer_key"]))
        self.mutation_count += 1

    def verify_wrong_answer(
        self,
        _target,
        question: dict[str, Any],
        *,
        key: str,
        operation_id: str,
    ) -> bool:
        return (
            key in self.wrong_answer_operations
            and str(question["wrong_answer_key"]) in self.wrong_answers
            and bool(operation_id)
        )

    def wrong_answer_exists(self, _target, question: dict[str, Any]) -> bool:
        return str(question["wrong_answer_key"]) in self.wrong_answers

    def create_wrong_answer(self, _target, question: dict[str, Any]) -> None:
        self.wrong_answers.add(str(question["wrong_answer_key"]))
        self.mutation_count += 1

    def read_snapshot(self, page_id: str) -> dict[str, Any]:
        page = self.pages.get(page_id)
        if page is None or page["deleted"]:
            return {"fetch_result": "not_found", "page_count": 0}
        content = copy.deepcopy(page["grade_result"]["content"] if page["grade_result"] else None)
        return {
            "fetch_result": "present",
            "page_count": len([item for item in self.pages.values() if not item["deleted"]]),
            "page_id": page["id"],
            "url": page["url"],
            "title": page["title"],
            "parent_page_id": page["parent_page_id"],
            "question_types": [_plain(block).split(". ", 1)[-1] for block in page["children"] if block.get("type") == "heading_3"],
            "source_count": sum(_plain(block).startswith("来源：") for block in page["children"]),
            "teacher_answer_count": sum(_plain(block).startswith("答案：") for block in page["children"]),
            "answers": copy.deepcopy(page["answers"]),
            "grade_marker": page["grade_result"]["marker"] if page["grade_result"] else None,
            "score": page["grade_result"]["score"] if page["grade_result"] else None,
            "graded_at": page["grade_result"]["graded_at"] if page["grade_result"] else None,
            "per_question": content["questions"] if content else [],
            "result_block_sets": page["result_block_sets"],
            "wrong_answer_registrations": sorted(self.wrong_answers),
        }

    def cleanup_page(self, page_id: str) -> dict[str, Any]:
        self.pages[page_id]["deleted"] = True
        self.mutation_count += 1
        return {"page_id": page_id, "cleanup_status": "deleted"}



class _Notes:
    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict[str, str]]:
        return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "Window D fixture notes"}]


class WindowDPanelDriver:
    """Drive organize and grade only through panel HTTP routes."""

    def __init__(
        self,
        client: TestClient,
        write_answers: Callable[[str, dict[str, str]], None],
    ):
        self.client = client
        self.write_answers = write_answers
        self.requests: list[dict[str, Any]] = []
        self.created_page_id: str | None = None

    def _get(self, path: str, **kwargs):
        self.requests.append({"method": "GET", "path": path})
        return self.client.get(path, **kwargs)

    def _post(self, path: str, **kwargs):
        normalized = path
        if path.endswith("/grade"):
            normalized = "/api/exercises/{exercise_id}/grade"
        self.requests.append({"method": "POST", "path": normalized})
        return self.client.post(path, **kwargs)

    def _poll(self, path: str, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            response = self._get(path, params={"job_id": job_id})
            payload = response.json()
            if payload.get("status") != "running":
                return payload
            time.sleep(0.01)
        raise AssertionError(f"panel job did not finish within 5 seconds: {path}")

    def organize(self) -> dict[str, Any]:
        estimate = self._get("/api/exercises/organize/estimate")
        if estimate.status_code != 200:
            raise AssertionError("organize estimate failed")
        started = self._post("/api/exercises/organize", json={
            "course_id": COURSE_NET, "lecture_ids": [NET_L1], "e2e_mode": True,
        })
        if started.status_code != 202:
            raise AssertionError(f"organize did not start: {started.text}")
        return self._poll("/api/exercises/organize/status", started.json()["job_id"])

    def grade(self, exercise_id: str) -> dict[str, Any]:
        started = self._post(f"/api/exercises/{exercise_id}/grade", json={})
        if started.status_code != 202:
            raise AssertionError(f"grade did not start: {started.text}")
        return self._poll("/api/exercises/grade/status", started.json()["job_id"])

    def run(self) -> dict[str, Any]:
        organized = self.organize()
        self.created_page_id = organized["exercise_id"]
        self.write_answers(organized["exercise_id"], ANSWER_FIXTURE)
        graded = self.grade(organized["exercise_id"])
        organize_rerun = self.organize()
        grade_confirmation = self._post(
            f"/api/exercises/{organized['exercise_id']}/grade", json={}
        )
        if (
            grade_confirmation.status_code != 409
            or grade_confirmation.json().get("status") != "confirm_required"
        ):
            raise AssertionError(f"expected grade confirmation gate: {grade_confirmation.text}")
        grade_rerun_started = self._post(
            f"/api/exercises/{organized['exercise_id']}/grade",
            json={"regrade": True, "confirm": True},
        )
        if grade_rerun_started.status_code != 202:
            raise AssertionError(
                f"confirmed grade rerun did not start: {grade_rerun_started.text}"
            )
        grade_rerun = self._poll(
            "/api/exercises/grade/status", grade_rerun_started.json()["job_id"]
        )
        grade_rerun["no_op"] = (
            grade_rerun.get("points_charged") == 0
            and grade_rerun.get("score") == graded.get("score")
            and grade_rerun.get("graded_at") == graded.get("graded_at")
        )
        return {
            "course_id": COURSE_NET,
            "organize": organized,
            "grade": graded,
            "organize_rerun": organize_rerun,
            "grade_rerun": grade_rerun,
            "panel_requests": copy.deepcopy(self.requests),
        }


class WindowDReadOnlyVerifier:
    """Pure verifier: it receives snapshots and never receives a write client."""

    def verify(self, *, snapshot: dict[str, Any], panel_summary: dict[str, Any],
               organize_result: dict[str, Any], organize_rerun: dict[str, Any],
               grade_rerun: dict[str, Any], ledger_rows: list[dict[str, Any]],
               expected_parent_id: str) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        expected_types = [item["type"] for item in QUESTION_FIXTURE]
        expected_scores = [item["expected_score"] for item in QUESTION_FIXTURE]
        expected_outcomes = [item["expected_outcome"] for item in QUESTION_FIXTURE]
        per_question = snapshot.get("per_question") or []
        # Real relay charges are millipoints/1000 floats: subtracting the
        # charged value from the before-balance is not always bit-identical
        # to the after-balance's division result, so the arithmetic check
        # compares within a tight tolerance instead of exact equality
        # (clean fake values still pass trivially; contiguity stays exact
        # because both sides divide the same integer millipoint balance).
        ledger_balances_match = all(
            abs(
                float(row.get("points_before", 0))
                - float(row.get("points_charged", 0))
                - float(row.get("points_after", 0))
            )
            <= LEDGER_FLOAT_TOLERANCE
            for row in ledger_rows
        )
        ledger_is_contiguous = all(
            left.get("points_after") == right.get("points_before")
            for left, right in zip(ledger_rows, ledger_rows[1:])
        )
        _check(checks, "exactly one exercise page exists", snapshot.get("page_count") == 1)
        _check(checks, "page parent matches selected course", snapshot.get("parent_page_id") == expected_parent_id)
        _check(checks, "title carries exactly one E2E marker", snapshot.get("title", "").count("[E2E]") == 1)
        _check(checks, "exercise has the adopted five ordered types", snapshot.get("question_types") == expected_types)
        _check(checks, "every question has a source", snapshot.get("source_count") == 5)
        _check(checks, "teacher section has five standard answers", snapshot.get("teacher_answer_count") == 5)
        _check(checks, "deterministic answers remain verbatim", snapshot.get("answers") == ANSWER_FIXTURE)
        _check(checks, "exactly one E2E grading marker/result set exists", snapshot.get("grade_marker") == "E2E_GRADE_RESULT" and snapshot.get("result_block_sets") == 1)
        _check(
            checks,
            "five per-question scores and outcomes include Q3 partial credit",
            len(per_question) == 5
            and [row.get("score") for row in per_question] == expected_scores
            and [row.get("outcome") for row in per_question] == expected_outcomes
            and per_question[2].get("partial") is True,
        )
        _check(checks, "wrong answers Q2 and Q5 registered exactly once", snapshot.get("wrong_answer_registrations") == ["Q2", "Q5"])
        _check(
            checks,
            "panel summary matches Notion identity, score, and timestamp",
            panel_summary.get("exercise_id") == snapshot.get("page_id")
            and panel_summary.get("result_page_url") == snapshot.get("url")
            and panel_summary.get("score") == snapshot.get("score")
            and panel_summary.get("graded_at") == snapshot.get("graded_at"),
        )
        _check(checks, "organize rerun reused the same page without charge", organize_rerun.get("exercise_id") == organize_result.get("exercise_id") and organize_rerun.get("reused") is True and organize_rerun.get("points_charged") == 0)
        _check(checks, "grade rerun was a zero-charge no-op", grade_rerun.get("exercise_id") == organize_result.get("exercise_id") and grade_rerun.get("points_charged") == 0 and grade_rerun.get("no_op") is True)
        _check(checks, "relay ledger is exactly quiz then grade", [row.get("operation") for row in ledger_rows] == ["quiz", "grade"])
        _check(
            checks,
            "relay ledger balances are contiguous and arithmetically valid",
            ledger_balances_match and ledger_is_contiguous,
        )
        _check(checks, "relay ledger charges reconcile with panel settlements", sum(float(row.get("points_charged", 0)) for row in ledger_rows) == float(organize_result.get("points_charged", 0)) + float(panel_summary.get("points_charged", 0)))
        return {"passed": all(item["result"] == "pass" for item in checks), "checks": checks}


@dataclass
class WindowDDryRun:
    relay: FakeWindowDRelay
    notion: FakeWindowDNotion
    driver: WindowDPanelDriver
    scratch_root: Path

    def __enter__(self) -> "WindowDDryRun":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.cleanup(self.driver.created_page_id)

    def cleanup(self, page_id: str | None = None) -> dict[str, Any]:
        page_cleanup = {"cleanup_status": "not_applicable"}
        if (
            page_id is not None
            and self.notion.read_snapshot(page_id)["fetch_result"] == "present"
        ):
            page_cleanup = self.notion.cleanup_page(page_id)
        self.driver.client.close()
        if self.scratch_root.exists():
            shutil.rmtree(self.scratch_root)
        return {
            "page": page_cleanup,
            "post_cleanup_fetch": (
                self.notion.read_snapshot(page_id)["fetch_result"]
                if page_id is not None
                else "not_applicable"
            ),
            "scratch_root_removed": not self.scratch_root.exists(),
        }


def build_dry_run(scratch_root: Path) -> WindowDDryRun:
    scratch_root = Path(scratch_root)
    if scratch_root.exists() and any(scratch_root.iterdir()):
        raise ValueError("scratch_root must be absent or empty so cleanup is probe-owned")
    scratch_root.mkdir(parents=True, exist_ok=True)
    (scratch_root / ".window-d-probe-owned").write_text("dry-run", encoding="utf-8")
    (scratch_root / "client-data").mkdir(exist_ok=True)
    (scratch_root / "client-data" / "fixture.json").write_text(
        json.dumps(
            {"mode": "dry-run-fakes-only", "answer_ids": list(ANSWER_FIXTURE)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    ledger_path = scratch_root / "relay-ledger.json"
    try:
        directory = build_fake_directory()
        directory.load()
        relay = FakeWindowDRelay(ledger_path=ledger_path)
        notion = FakeWindowDNotion()
        organizer = ExerciseOrganizer(
            directory_service=directory,
            relay=relay,
            page_adapter=notion,
            notes_provider=_Notes(),
            record_store=MemoryOrganizeRecordStore(),
            e2e_enabled=True,
        )
        grader = ExerciseGrader(
            directory_service=directory,
            relay=relay,
            page_adapter=notion,
            clock=lambda: "2026-09-20T12:00:00+08:00",
        )
        client = TestClient(create_app(
            directory_service=directory,
            organizer_service=organizer,
            grading_service=grader,
            platform_service=relay,
        ))
        return WindowDDryRun(
            relay=relay,
            notion=notion,
            driver=WindowDPanelDriver(client, notion.write_answers),
            scratch_root=scratch_root,
        )
    except Exception:
        shutil.rmtree(scratch_root, ignore_errors=True)
        raise


def run_dry_run(*, output: Path, scratch_root: Path) -> dict[str, Any]:
    output = Path(output)
    scratch_root = Path(scratch_root)
    if output.resolve().is_relative_to(scratch_root.resolve()):
        raise ValueError("output must be outside scratch_root so cleanup cannot delete evidence")
    harness = build_dry_run(scratch_root)
    started = _now()
    page_id: str | None = None
    try:
        outcome = harness.driver.run()
        page_id = outcome["organize"]["exercise_id"]
        snapshot = harness.notion.read_snapshot(page_id)
        ledger = harness.relay.read_ledger()
        verification = WindowDReadOnlyVerifier().verify(
            snapshot=snapshot,
            panel_summary=outcome["grade"],
            organize_result=outcome["organize"],
            organize_rerun=outcome["organize_rerun"],
            grade_rerun=outcome["grade_rerun"],
            ledger_rows=ledger,
            expected_parent_id=outcome["course_id"],
        )
    finally:
        cleanup = harness.cleanup(page_id or harness.driver.created_page_id)
    cleanup_complete = (
        cleanup["page"]["cleanup_status"] == "deleted"
        and cleanup["post_cleanup_fetch"] == "not_found"
        and cleanup["scratch_root_removed"]
    )
    if not verification["passed"] or not cleanup_complete:
        raise AssertionError("Window D dry-run verification or cleanup failed")
    evidence = {
        "schema_version": 1,
        "mode": "dry-run-fakes-only",
        "outcome": "passed",
        "timestamps": {"started_at": started, "finished_at": _now()},
        "fixture": {
            "question_ids": [item["question_id"] for item in QUESTION_FIXTURE],
            "question_types": [item["type"] for item in QUESTION_FIXTURE],
            "answer_write": "deterministic Q1-Q5 fixture",
        },
        "page": {
            "page_id": snapshot["page_id"],
            "url": snapshot["url"],
            "title": snapshot["title"],
            "expected_parent_id": outcome["course_id"],
            "actual_parent_id": snapshot["parent_page_id"],
            "cleanup_status": cleanup["page"]["cleanup_status"],
            "post_cleanup_fetch": cleanup["post_cleanup_fetch"],
        },
        "panel": {
            "organize": outcome["organize"],
            "grade_summary": outcome["grade"],
            "organize_rerun": outcome["organize_rerun"],
            "grade_rerun": outcome["grade_rerun"],
            "requests": outcome["panel_requests"],
        },
        "checks": verification["checks"],
        "ledger_rows": ledger,
        "ledger_reconciliation": {
            "expected_llm_ops": 2,
            "actual_llm_ops": len(ledger),
            "rerun_llm_ops": 0,
            "matched": [row["operation"] for row in ledger] == ["quiz", "grade"],
        },
        "cleanup": {
            "scratch_root": str(harness.scratch_root),
            "scratch_root_removed": cleanup["scratch_root_removed"],
            "fake_notion_page": cleanup["page"]["cleanup_status"],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def write_dry_run_pack(*, pack_dir: Path, scratch_root: Path) -> dict[str, Any]:
    """Write a harness-compatible Window D fixture pack with zero real spend."""
    pack_dir = Path(pack_dir)
    scratch_root = Path(scratch_root)
    if pack_dir.resolve().is_relative_to(scratch_root.resolve()):
        raise ValueError("pack_dir must be outside scratch_root")
    if pack_dir.exists() and any(pack_dir.iterdir()):
        raise ValueError("pack_dir must be absent or empty to avoid stale evidence")
    scenario_id = "WINDOW-D-PANEL-TOOLING-DRY-RUN"
    raw_path = pack_dir / "evidence" / "window-d-dry-run.json"
    evidence = run_dry_run(output=raw_path, scratch_root=scratch_root)
    scenario = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "window": "D",
        "status": "passed",
        "timestamps": evidence["timestamps"],
        "inputs": {
            "surface": "in-process panel HTTP organize and grade routes",
            "mode": evidence["mode"],
            "fixture": "deterministic five-type Q1-Q5 exercise",
        },
        "outputs": {
            "urls": [evidence["page"]["url"]],
            "files": ["evidence/window-d-dry-run.json"],
        },
        "notion_pages": [
            {
                "page_id": evidence["page"]["page_id"],
                "url": evidence["page"]["url"],
                "title": evidence["page"]["title"],
                "expected_parent_id": evidence["page"]["expected_parent_id"],
                "pre_cleanup_fetch": {
                    "fetched_at": evidence["timestamps"]["finished_at"],
                    "title": evidence["page"]["title"],
                    "actual_parent_id": evidence["page"]["actual_parent_id"],
                    "title_verified": True,
                    "parent_verified": True,
                },
                "cleanup_status": evidence["page"]["cleanup_status"],
                "post_cleanup_fetch": {
                    "fetched_at": evidence["timestamps"]["finished_at"],
                    "result": evidence["page"]["post_cleanup_fetch"],
                },
            }
        ],
        "result_markers": [
            {"surface": "panel organize", "value": "completed"},
            {"surface": "panel grade", "value": "score=50"},
            {"surface": "read-only verifier", "value": "all checks passed"},
        ],
        "assertions": evidence["checks"],
        "settlement": {
            "asr_tasks": 0,
            "llm_ops": 0,
            "oauth_exchanges": 0,
            "points_charged": 0,
            "seconds_charged": 0,
        },
        "cleanup_status": "complete",
        "scratch_cleanup": [
            {"artifact": "probe-owned scratch client and fake relay ledger", "removed": True}
        ],
    }
    ledger = {
        "schema_version": 1,
        "window": "D",
        "ceiling": {"asr_tasks": 0, "llm_ops": 2, "oauth_exchanges": 0},
        "totals": {"asr_tasks": 0, "llm_ops": 0, "oauth_exchanges": 0},
        "within_ceiling": True,
    }
    scenario_path = pack_dir / "scenarios" / f"{scenario_id}.json"
    scenario_path.parent.mkdir(parents=True, exist_ok=True)
    scenario_path.write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pack_dir / "window-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"pack_dir": str(pack_dir), "scenario": scenario, "ledger": ledger, "raw": evidence}


__all__ = [
    "ANSWER_FIXTURE", "QUESTION_FIXTURE", "FakeWindowDNotion",
    "FakeWindowDRelay", "WindowDPanelDriver", "WindowDReadOnlyVerifier",
    "build_dry_run", "run_dry_run", "write_dry_run_pack",
]
