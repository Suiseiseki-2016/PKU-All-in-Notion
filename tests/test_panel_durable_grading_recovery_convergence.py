"""Additive convergence coverage for charged grading recovery."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

import pku_sync.panel.exercise_grader as grader_module
from pku_sync.notion import markdown_to_blocks
from pku_sync.panel.exercise_grader import (
    PHASE_COMPLETED,
    PHASE_RESULT_RECONCILED,
    PHASE_RESULT_VERIFIED,
    ExerciseGrader,
    GradeBlocked,
    GradeSettlementError,
    GradeTarget,
    GradingInput,
    RealGradingPageAdapter,
    _operation_result_markdown,
)
from pku_sync.panel.exercises import JsonGradingRecordStore
from pku_sync.panel.grading_api import GradeJobController

GRADED_AT = "2026-09-20T12:00:00+08:00"
WRONG_CONTENT = json.dumps({"score": 72, "questions": [{"number": 2, "type": "判断", "score": 0, "max_score": 2}]}, ensure_ascii=False)
CORRECT_CONTENT = json.dumps({"score": 100, "questions": [{"number": 1, "type": "选择", "score": 2, "max_score": 2}]}, ensure_ascii=False)


def target() -> GradeTarget:
    return GradeTarget("grade", "exercise-page", "https://www.notion.so/exercise-page", "考前练习", "course-page", "计算机网络", "考前练习")


def prepared(*, marker: bool = False) -> tuple[GradeTarget, GradingInput]:
    return target(), GradingInput(prompt="grade-prompt", unanswered=[], marker_present=marker, answer_fingerprint="answers-v1", answer_provenance="known")


class Relay:
    def __init__(self, content: str = WRONG_CONTENT):
        self.content, self.calls, self.balance = content, 0, 20.0

    def quota(self):
        return {"available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt):
        self.calls += 1
        self.balance -= 3
        return {"content": self.content, "points_charged": 3.0}


class Directory:
    def __init__(self, path: Path):
        self.grading_record_store = JsonGradingRecordStore(path)
        self.local_grading_records = self.grading_record_store.snapshot()

    def exercise_grade_target(self, _exercise_id):
        return {"page_id": target().page_id, "page_url": target().page_url, "title": target().title, "course_id": target().course_id, "course_title": target().course_title, "scope": target().scope}

    def record_grading(self, exercise_id, record):
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = copy.deepcopy(record)

    def resolve_exercise_launch(self, _exercise_id):
        return type("Launch", (), {"status": "opened"})()


class DependencyAdapter:
    def __init__(self):
        self.preflight_calls = 0
        self.dependency_available = True
        self.result_operation = ""
        self.wrong_answers: set[str] = set()

    def preflight_wrong_answer_database(self):
        self.preflight_calls += 1
        if not self.dependency_available:
            raise GradeBlocked("错题数据库暂时不可用。")

    def verify_parent(self, _target):
        return None

    def read_for_grading(self, _target):
        return prepared(marker=bool(self.result_operation))[1]

    def ensure_result(self, _target, *, operation_id, **_kwargs):
        self.result_operation = operation_id

    def verify_result(self, _target, *, operation_id, **_kwargs):
        return self.result_operation == operation_id

    def result_cleanup_plan(self, _target, **_kwargs):
        return []

    def archive_result_block(self, _target, **_kwargs):
        raise AssertionError("no stale blocks")

    def ensure_wrong_answer(self, _target, _row, *, key, **_kwargs):
        self.wrong_answers.add(key)

    def verify_wrong_answer(self, _target, _row, *, key, **_kwargs):
        return key in self.wrong_answers


def restart(path: Path, relay: Relay, adapter: DependencyAdapter) -> ExerciseGrader:
    return ExerciseGrader(directory_service=Directory(path), relay=relay, page_adapter=adapter, clock=lambda: GRADED_AT)


def wait(controller: GradeJobController, job_id: str) -> dict:
    for _ in range(200):
        body, _ = controller.snapshot(job_id)
        if body["status"] != "running":
            return body
        time.sleep(0.005)
    raise AssertionError("grading job did not finish")


def test_controller_charged_dependency_failure_then_ordinary_retry_uses_no_relay(tmp_path):
    path, relay, adapter = tmp_path / "grading.json", Relay(), DependencyAdapter()
    first_service = restart(path, relay, adapter)
    original_ensure = adapter.ensure_result

    def fail_after_result(*args, **kwargs):
        original_ensure(*args, **kwargs)
        adapter.dependency_available = False

    adapter.ensure_result = fail_after_result
    controller = GradeJobController(first_service)
    started, code = controller.start(target().page_id)
    assert code == 202
    failed = wait(controller, started["job_id"])
    assert failed == {"status": "failed", "reason": "错题数据库暂时不可用。", "retryable": True, "points_charged": 3.0, "points_remaining": 17.0}
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_RESULT_RECONCILED
    assert relay.calls == 1

    adapter.ensure_result = original_ensure
    adapter.dependency_available = True
    retry_controller = GradeJobController(restart(path, relay, adapter))
    retried, code = retry_controller.start(target().page_id)
    assert code == 202
    completed = wait(retry_controller, retried["job_id"])
    assert completed["status"] == "completed"
    assert completed["points_charged"] == 3.0 and completed["points_remaining"] == 17.0
    assert relay.calls == 1
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_COMPLETED


def test_postcharge_result_without_wrong_answers_skips_dependency(tmp_path):
    path, relay, adapter = tmp_path / "grading.json", Relay(CORRECT_CONTENT), DependencyAdapter()
    adapter.dependency_available = False
    record = {"version": 2, "status": "pending", "phase": PHASE_RESULT_RECONCILED, "operation_id": "stable-operation", "operation": "grade", "page_id": target().page_id, "page_url": target().page_url, "course_id": target().course_id, "course_title": target().course_title, "title": target().title, "scope": target().scope, "answer_fingerprint": "answers-v1", "answer_provenance": "known", "regrade": False, "content": CORRECT_CONTENT, "score": 100, "graded_at": GRADED_AT, "points_charged": 3.0, "points_remaining": 17.0, "result_page_url": target().page_url}
    adapter.result_operation = record["operation_id"]
    JsonGradingRecordStore(path).put(target().page_id, record)
    service = restart(path, relay, adapter)
    result = service.grade(service.prepare(target().page_id))
    assert result["status"] == "completed"
    assert adapter.preflight_calls == 0 and relay.calls == 0


def with_ids(blocks, start=1):
    result = copy.deepcopy(blocks)
    for index, block in enumerate(result, start):
        block["id"] = f"{index:032x}"
    return result


class PageClient:
    def __init__(self, blocks):
        self.blocks, self.archived, self.appended = list(blocks), [], []
        self.insert_user_after_classification = False

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def list_children(self, _page_id): return copy.deepcopy(self.blocks)

    def archive_block(self, block_id, **_kwargs):
        if self.insert_user_after_classification:
            self.insert_user_after_classification = False
            self.blocks.extend(with_ids(markdown_to_blocks("### 并发补充\n\n保留这段"), 900))
        self.archived.append(block_id)
        self.blocks = [block for block in self.blocks if block.get("id") != block_id]
        return {"archived": True}

    def append_blocks(self, _page_id, blocks, **_kwargs):
        self.appended.append(copy.deepcopy(blocks))
        created = with_ids(blocks, len(self.blocks) + 100)
        self.blocks.extend(created)
        return created


def ensure(adapter, frozen):
    adapter.ensure_result(target(), operation_id="operation", content=WRONG_CONTENT, score=72, graded_at=GRADED_AT, regrade=False, append_plan=frozen)


def test_nonzero_token_only_retirement_preserves_answers_and_appends_full_plan(monkeypatch):
    frozen = markdown_to_blocks(_operation_result_markdown("operation", WRONG_CONTENT, score=72, graded_at=GRADED_AT))
    answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500)
    token_only = with_ids([frozen[1]], 700)
    unrelated = with_ids(markdown_to_blocks("### 我的补充\n\n不要删除"), 800)
    client = PageClient(answers + token_only + unrelated)
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.archived == [token_only[0]["id"]] and client.appended == [frozen]
    assert client.blocks[:len(answers)] == answers
    assert any(grader_module._plain(block) == "不要删除" for block in client.blocks)
    assert len([block for block in client.blocks if grader_module._plain(block) == "PKU_GRADE_OPERATION:operation"]) == 1


def test_concurrent_user_append_during_retirement_survives_and_cannot_split_envelope(monkeypatch):
    frozen = markdown_to_blocks(_operation_result_markdown("operation", WRONG_CONTENT, score=72, graded_at=GRADED_AT))
    answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500)
    client = PageClient(answers + with_ids(frozen[:3], 700))
    client.insert_user_after_classification = True
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.blocks[:len(answers)] == answers
    assert any(grader_module._plain(block) == "保留这段" for block in client.blocks)
    assert client.appended == [frozen]
    assert len([block for block in client.blocks if grader_module._plain(block) == "PKU_GRADE_OPERATION:operation"]) == 1


def test_cleanup_success_response_is_not_completion_until_external_absence(tmp_path):
    class FalseSuccessAdapter(DependencyAdapter):
        def __init__(self):
            super().__init__(); self.stale = ["stale-marker"]; self.false_success = True
        def result_cleanup_plan(self, _target, **_kwargs): return list(self.stale)
        def archive_result_block(self, _target, *, block_id):
            if not self.false_success: self.stale.remove(block_id)

    path, relay, adapter = tmp_path / "grading.json", Relay(CORRECT_CONTENT), FalseSuccessAdapter()
    adapter.result_operation = "stable-operation"
    JsonGradingRecordStore(path).put(target().page_id, {"version": 2, "status": "pending", "phase": PHASE_RESULT_VERIFIED, "operation_id": "stable-operation", "operation": "grade", "page_id": target().page_id, "page_url": target().page_url, "course_id": target().course_id, "course_title": target().course_title, "title": target().title, "scope": target().scope, "answer_fingerprint": "answers-v1", "answer_provenance": "known", "regrade": True, "content": CORRECT_CONTENT, "score": 100, "graded_at": GRADED_AT, "points_charged": 3.0, "points_remaining": 17.0, "result_page_url": target().page_url})
    service = restart(path, relay, adapter)
    with pytest.raises(GradeSettlementError): service.grade(service.prepare(target().page_id))
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_RESULT_VERIFIED
    adapter.false_success = False
    retried = restart(path, relay, adapter)
    result = retried.grade(retried.prepare(target().page_id))
    assert result["status"] == "completed" and adapter.stale == [] and relay.calls == 0



def test_fresh_preflight_failure_never_charges_relay(tmp_path):
    path, relay, adapter = tmp_path / "grading.json", Relay(), DependencyAdapter()
    adapter.dependency_available = False
    service = restart(path, relay, adapter)
    with pytest.raises(GradeBlocked, match="错题数据库暂时不可用"):
        service.prepare(target().page_id)
    assert relay.calls == 0
    assert JsonGradingRecordStore(path).snapshot() == {}


def test_durable_intent_preflight_failure_never_starts_relay(tmp_path):
    path, relay, adapter = tmp_path / "grading.json", Relay(), DependencyAdapter()
    adapter.dependency_available = False
    record = {
        "version": 2, "status": "pending", "phase": "intent", "operation_id": "intent-operation",
        "operation": "grade", "page_id": target().page_id, "page_url": target().page_url,
        "course_id": target().course_id, "course_title": target().course_title,
        "title": target().title, "scope": target().scope,
        "answer_fingerprint": "answers-v1", "answer_provenance": "known", "regrade": False,
    }
    JsonGradingRecordStore(path).put(target().page_id, record)
    service = restart(path, relay, adapter)
    with pytest.raises(GradeBlocked, match="错题数据库暂时不可用"):
        service.prepare(target().page_id)
    assert relay.calls == 0
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == "intent"


def test_complete_exact_envelope_advances_without_append(monkeypatch):
    frozen = markdown_to_blocks(_operation_result_markdown("operation", WRONG_CONTENT, score=72, graded_at=GRADED_AT))
    answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500)
    client = PageClient(answers + with_ids(frozen, 700))
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.appended == [] and client.archived == []
    assert client.blocks[:len(answers)] == answers


def test_realistic_partial_retirement_is_marker_first_reverse_owned_then_token(monkeypatch):
    frozen = markdown_to_blocks(_operation_result_markdown("operation", WRONG_CONTENT, score=72, graded_at=GRADED_AT))
    answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500)
    partial = with_ids(frozen[:4], 700)
    client = PageClient(answers + partial)
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.archived == [partial[0]["id"], partial[3]["id"], partial[2]["id"], partial[1]["id"]]
    assert client.appended == [frozen]
    assert client.blocks[:len(answers)] == answers


def test_conflicting_token_ownership_never_mutates_realistic_page(monkeypatch):
    frozen = markdown_to_blocks(_operation_result_markdown("operation", WRONG_CONTENT, score=72, graded_at=GRADED_AT))
    answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500)
    conflict = with_ids([frozen[0], frozen[1], markdown_to_blocks("批改时间：冲突")[0]], 700)
    client = PageClient(answers + conflict)
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)
    with pytest.raises(GradeBlocked):
        ensure(RealGradingPageAdapter(object()), frozen)
    assert client.appended == [] and client.archived == []
    assert client.blocks == answers + conflict


def test_fake_recovery_fixture_fails_charged_then_retries_without_second_relay():
    from fastapi.testclient import TestClient
    from pku_sync.panel.fake_directory import build_fake_directory
    from pku_sync.panel.fake_workspace import EXERCISE_GENERATED
    from pku_sync.panel.platform_bridge import FakePlatformBridge
    from pku_sync.panel.webapi import create_app

    directory = build_fake_directory("grade-recovery")
    relay = FakePlatformBridge(activated=True, llm_points=20.0, grade_with_wrong_answer=True)
    grader = grader_module.make_fake_grading_service(
        directory, relay, fail_wrong_answer_preflight_once=True
    )
    client = TestClient(create_app(
        directory_service=directory, platform_service=relay, grading_service=grader
    ))
    assert client.get("/api/directory").status_code == 200
    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade").json()
    failed = wait(client.app.state.grade_jobs, started["job_id"])
    assert failed["status"] == "failed"
    assert failed["points_charged"] == 3.0 and failed["points_remaining"] == 17.0
    assert len(relay.llm_calls) == 1

    retried = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade").json()
    completed = wait(client.app.state.grade_jobs, retried["job_id"])
    assert completed["status"] == "completed"
    assert completed["points_charged"] == 3.0 and completed["points_remaining"] == 17.0
    assert len(relay.llm_calls) == 1


def test_postcharge_recovery_ignores_newly_incomplete_answers(tmp_path):
    class NewlyIncompleteAdapter(DependencyAdapter):
        def read_for_grading(self, _target):
            return GradingInput(
                prompt="changed-prompt", unanswered=["第 1 题"], marker_present=False,
                answer_fingerprint="changed", answer_provenance="known",
            )

    path, relay, adapter = tmp_path / "grading.json", Relay(CORRECT_CONTENT), NewlyIncompleteAdapter()
    record = {
        "version": 2, "status": "pending", "phase": "relay_succeeded",
        "operation_id": "paid-operation", "operation": "grade",
        "page_id": target().page_id, "page_url": target().page_url,
        "course_id": target().course_id, "course_title": target().course_title,
        "title": target().title, "scope": target().scope,
        "answer_fingerprint": "answers-v1", "answer_provenance": "known", "regrade": False,
        "content": CORRECT_CONTENT, "points_charged": 3.0, "points_remaining": 17.0,
        "result_page_url": target().page_url, "graded_at": GRADED_AT,
    }
    JsonGradingRecordStore(path).put(target().page_id, record)
    service = restart(path, relay, adapter)
    result = service.grade(service.prepare(target().page_id))
    assert result["status"] == "completed"
    assert result["points_charged"] == 3.0 and relay.calls == 0
