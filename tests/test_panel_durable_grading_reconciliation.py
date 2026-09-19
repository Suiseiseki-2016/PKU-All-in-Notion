"""Durable grading reconciliation across restart and ambiguous writes."""
from __future__ import annotations
import json
from pathlib import Path
import httpx
import pytest
from pku_sync.notion import NotionClient
from pku_sync.panel.exercise_grader import (
    PHASE_COMPLETED, PHASE_INTENT, PHASE_RELAY_STARTED, PHASE_RELAY_SUCCEEDED,
    PHASE_RESULT_RECONCILED, PHASE_RESULT_VERIFIED, PHASE_WRONG_ANSWERS_VERIFIED,
    ExerciseGrader,
    GradeSettlementError, GradeTarget, GradingInput,
)
from pku_sync.panel.exercises import JsonGradingRecordStore
from pku_sync.panel.grading_api import GradeJobController

CONTENT = json.dumps({"score": 72, "questions": [
    {"number": 1, "type": "选择", "score": 2, "max_score": 2},
    {"number": 2, "type": "判断", "score": 0, "max_score": 2},
    {"number": 5, "type": "论述", "score": 0, "max_score": 2},
]})

def target():
    return GradeTarget("grade", "exercise-page", "https://www.notion.so/exercise-page", "考前练习", "course-page", "计算机网络", "考前练习")

def prepared(*, marker=False):
    return target(), GradingInput(prompt="grade-prompt", unanswered=[], marker_present=marker, answer_fingerprint="answers-v1", answer_provenance="known")

class Relay:
    def __init__(self): self.calls, self.balance = 0, 20.0
    def quota(self): return {"available": True, "llm_points_remaining": self.balance}
    def grade(self, _prompt):
        self.calls += 1; self.balance -= 3
        return {"content": CONTENT, "points_charged": 3}

class DurableDirectory:
    def __init__(self, path: Path):
        self.grading_record_store = JsonGradingRecordStore(path)
        self.local_grading_records = self.grading_record_store.snapshot()
        self.fail_completed_once = False
    def record_grading(self, exercise_id, record):
        if self.fail_completed_once and record.get("phase") == PHASE_COMPLETED:
            self.fail_completed_once = False
            raise OSError("final persistence failed")
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = dict(record)

class ExternalStateAdapter:
    def __init__(self):
        self.result_operations = []
        self.stale_result_children = ["old-result-a", "old-result-b"]
        self.answer_children = ["answer-a", "answer-b"]
        self.wrong_answers = set()
        self.ensure_calls = self.cleanup_calls = self.create_calls = 0
        self.fail_ambiguous_result_once = False
        self.fail_cleanup_once = False
        self.fail_wrong_after_remote_once = False
    def ensure_result(self, _target, *, operation_id, **_kwargs):
        self.ensure_calls += 1
        if operation_id not in self.result_operations: self.result_operations.append(operation_id)
        if self.fail_ambiguous_result_once:
            self.fail_ambiguous_result_once = False
            raise OSError("response lost after result commit")
    def verify_result(self, _target, *, operation_id, **_kwargs):
        return operation_id in self.result_operations
    def cleanup_result(self, _target, *, operation_id, **_kwargs):
        self.cleanup_calls += 1
        assert operation_id in self.result_operations
        if self.stale_result_children: self.stale_result_children.pop(0)
        if self.fail_cleanup_once:
            self.fail_cleanup_once = False
            raise OSError("cleanup interrupted")
        self.stale_result_children.clear()
    def ensure_wrong_answer(self, _target, _question, *, key, **_kwargs):
        self.create_calls += 1; self.wrong_answers.add(key)
        if self.fail_wrong_after_remote_once:
            self.fail_wrong_after_remote_once = False
            raise OSError("response lost after wrong-answer create")
    def verify_wrong_answer(self, _target, _question, *, key, **_kwargs):
        return key in self.wrong_answers

def restart(path, relay, adapter):
    return ExerciseGrader(directory_service=DurableDirectory(path), relay=relay, page_adapter=adapter, clock=lambda: "2026-09-20T12:00:00+08:00")

def test_intent_persistence_failure_stops_before_relay(tmp_path, monkeypatch):
    directory = DurableDirectory(tmp_path / "grading.json"); relay = Relay(); adapter = ExternalStateAdapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    monkeypatch.setattr(directory.grading_record_store, "put", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(Exception): grader.grade(prepared())
    assert relay.calls == 0 and adapter.result_operations == []

def test_ambiguous_first_result_commit_reconciles_after_restart_without_duplicate(tmp_path):
    path = tmp_path / "grading.json"; relay, adapter = Relay(), ExternalStateAdapter()
    adapter.stale_result_children.clear(); adapter.fail_ambiguous_result_once = True
    with pytest.raises(GradeSettlementError): restart(path, relay, adapter).grade(prepared())
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_RELAY_SUCCEEDED
    result = restart(path, relay, adapter).grade(prepared(marker=True))
    assert result["status"] == "completed" and relay.calls == 1 and len(adapter.result_operations) == 1

def test_regrade_verifies_replacement_then_resumes_partial_cleanup_only(tmp_path):
    path = tmp_path / "grading.json"; relay, adapter = Relay(), ExternalStateAdapter(); adapter.fail_cleanup_once = True
    original_answers = list(adapter.answer_children)
    with pytest.raises(GradeSettlementError): restart(path, relay, adapter).grade(prepared(marker=True), force=True)
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_RESULT_VERIFIED
    result = restart(path, relay, adapter).grade(prepared(marker=True))
    assert result["status"] == "completed" and relay.calls == 1 and adapter.ensure_calls == 1
    assert adapter.answer_children == original_answers and adapter.stale_result_children == []

def test_ambiguous_wrong_answer_create_is_verified_exactly_once_after_restart(tmp_path):
    path = tmp_path / "grading.json"; relay, adapter = Relay(), ExternalStateAdapter()
    adapter.stale_result_children.clear(); adapter.fail_wrong_after_remote_once = True
    with pytest.raises(GradeSettlementError): restart(path, relay, adapter).grade(prepared())
    record = JsonGradingRecordStore(path).snapshot()[target().page_id]
    assert record["phase"] == PHASE_RESULT_RECONCILED
    result = restart(path, relay, adapter).grade(prepared(marker=True))
    assert result["status"] == "completed" and relay.calls == 1 and len(adapter.wrong_answers) == 2
    assert all(key.startswith(record["operation_id"] + ":") for key in adapter.wrong_answers)

def test_final_completion_persistence_failure_restarts_from_verified_phase(tmp_path):
    path = tmp_path / "grading.json"; relay, adapter = Relay(), ExternalStateAdapter(); adapter.stale_result_children.clear()
    directory = DurableDirectory(path); directory.fail_completed_once = True
    with pytest.raises(GradeSettlementError): ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter).grade(prepared())
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_WRONG_ANSWERS_VERIFIED
    result = restart(path, relay, adapter).grade(prepared(marker=True))
    assert result["status"] == "completed" and relay.calls == 1
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_COMPLETED

def test_controller_recovers_pending_before_marker_confirmation():
    class Service:
        def __init__(self): self.directory_service = type("Directory", (), {"resolve_exercise_launch": lambda _self, _id: type("Launch", (), {"status": "opened"})()})()
        def has_pending(self, _exercise_id): return True
        def prepare(self, _exercise_id): return prepared(marker=True)
        def grade(self, _prepared, *, force=False): return {"status": "completed", "exercise_id": "exercise-page", "title": "考前练习", "score": 72, "graded_at": "now", "points_charged": 3, "points_remaining": 17, "result_page_url": "https://www.notion.so/exercise-page"}
    body, code = GradeJobController(Service()).start("exercise-page")
    assert code == 202 and body["status"] == "running"

def test_production_notion_mutations_return_created_ids_and_support_archive():
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "PATCH": return httpx.Response(200, json={"results": [{"id": "created-block"}]})
        return httpx.Response(200, json={"archived": True})
    with NotionClient("secret", transport=httpx.MockTransport(handler)) as client:
        created = client.append_blocks("a" * 32, [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}], retry=False)
        archived = client.archive_block("b" * 32, retry=False)
    assert created == [{"id": "created-block"}] and archived["archived"] is True and len(calls) == 2


@pytest.mark.parametrize("phase", [
    PHASE_INTENT, PHASE_RELAY_SUCCEEDED, PHASE_RESULT_VERIFIED,
    PHASE_RESULT_RECONCILED, PHASE_WRONG_ANSWERS_VERIFIED,
])
def test_restart_from_every_recoverable_durable_phase(tmp_path, phase):
    path = tmp_path / f"{phase}.json"
    relay, adapter = Relay(), ExternalStateAdapter()
    operation_id = "stable-operation"
    record = {
        "version": 2, "status": "pending", "phase": phase,
        "operation_id": operation_id, "operation": "grade",
        "page_id": target().page_id, "page_url": target().page_url,
        "course_id": target().course_id, "course_title": target().course_title,
        "title": target().title, "scope": target().scope,
        "answer_fingerprint": "answers-v1", "answer_provenance": "known",
        "regrade": True,
    }
    if phase != PHASE_INTENT:
        record.update(content=CONTENT, score=72, graded_at="2026-09-20T12:00:00+08:00",
                      points_charged=3.0, points_remaining=17.0,
                      result_page_url=target().page_url)
    if phase in {PHASE_RESULT_VERIFIED, PHASE_RESULT_RECONCILED, PHASE_WRONG_ANSWERS_VERIFIED}:
        adapter.result_operations.append(operation_id)
    if phase in {PHASE_RESULT_RECONCILED, PHASE_WRONG_ANSWERS_VERIFIED}:
        adapter.stale_result_children.clear()
    if phase == PHASE_WRONG_ANSWERS_VERIFIED:
        adapter.wrong_answers.update({operation_id + ":2", operation_id + ":5"})
    JsonGradingRecordStore(path).put(target().page_id, record)

    result = restart(path, relay, adapter).grade(prepared(marker=True))

    assert result["status"] == "completed"
    assert relay.calls == (1 if phase == PHASE_INTENT else 0)
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_COMPLETED


def test_restart_from_relay_started_is_explicitly_not_replayed(tmp_path):
    path = tmp_path / "relay-started.json"
    record = {"version": 2, "status": "pending", "phase": PHASE_RELAY_STARTED,
              "operation_id": "ambiguous", "page_id": target().page_id,
              "answer_fingerprint": "answers-v1"}
    JsonGradingRecordStore(path).put(target().page_id, record)
    relay, adapter = Relay(), ExternalStateAdapter()

    from pku_sync.panel.exercise_grader import GradeBlocked
    with pytest.raises(GradeBlocked, match="\u65e0\u6cd5\u786e\u8ba4"):
        restart(path, relay, adapter).grade(prepared(marker=True))

    assert relay.calls == 0 and adapter.result_operations == []


def test_production_result_envelope_keeps_marker_and_cleanup_stops_at_user_content():
    from pku_sync.panel.exercise_grader import (
        _find_marker, _find_operation, _operation_result_markdown,
        _stale_result_blocks,
    )
    from pku_sync.notion import markdown_to_blocks

    old = markdown_to_blocks("## \u6279\u6539\u7ed3\u679c\n\nPKU_GRADE_OPERATION:old\n\n\u6279\u6539\u65f6\u95f4\uff1aold\n\n\u603b\u5206\uff1a60\n\n```json\n{}\n```")
    user = markdown_to_blocks("### \u6211\u7684\u5907\u6ce8\n\nkeep me")
    current = markdown_to_blocks(_operation_result_markdown("new", CONTENT, score=72, graded_at="now"))
    blocks = old + user + current

    assert _find_marker(current) is not None
    operation = _find_operation(blocks, "new")
    stale = _stale_result_blocks(blocks, operation)
    assert old[0] in stale and old[-1] in stale
    assert not any(block in stale for block in user)


def test_wrong_answer_create_can_disable_ambiguous_post_retry():
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"message": "ambiguous"})
    with NotionClient("secret", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception):
            client.create_page("a" * 32, "wrong answer", retry=False)
    assert calls == 1



def test_partial_wrong_answer_progress_is_durable_before_next_create(tmp_path):
    path = tmp_path / "wrong-progress.json"; relay, adapter = Relay(), ExternalStateAdapter()
    adapter.stale_result_children.clear()
    original_ensure = adapter.ensure_wrong_answer
    def fail_second(target_arg, question, *, key, **kwargs):
        if adapter.create_calls == 1:
            raise OSError("second create did not start")
        original_ensure(target_arg, question, key=key, **kwargs)
    adapter.ensure_wrong_answer = fail_second

    with pytest.raises(GradeSettlementError):
        restart(path, relay, adapter).grade(prepared())

    record = JsonGradingRecordStore(path).snapshot()[target().page_id]
    assert record["phase"] == PHASE_RESULT_RECONCILED
    assert record["wrong_answer_progress"] == [record["operation_id"] + ":2"]
    assert adapter.wrong_answers == {record["operation_id"] + ":2"}


def test_wrong_answer_identity_prefers_explicit_logical_key():
    from pku_sync.panel.exercise_grader import _wrong_answer_operation_key
    first = {"number": None, "wrong_answer_key": "source-question-a"}
    second = {"number": None, "wrong_answer_key": "source-question-b"}
    assert _wrong_answer_operation_key("operation", first) == "operation:source-question-a"
    assert _wrong_answer_operation_key("operation", second) == "operation:source-question-b"


def test_production_cleanup_journal_reconciles_ambiguous_archive_after_restart(tmp_path):
    class DurableCleanupAdapter(ExternalStateAdapter):
        def __init__(self):
            super().__init__()
            self.block_ids = ["old-marker", "old-token", "old-time", "old-score", "old-code"]
            self.archive_calls = []
            self.ambiguous_once = True
        def result_cleanup_plan(self, _target, *, operation_id, **_kwargs):
            assert operation_id in self.result_operations
            return list(self.block_ids)
        def archive_result_block(self, _target, *, block_id):
            self.archive_calls.append(block_id)
            if block_id not in self.block_ids:
                return
            self.block_ids.remove(block_id)
            if self.ambiguous_once:
                self.ambiguous_once = False
                raise OSError("response lost after archive")

    path = tmp_path / "cleanup-progress.json"; relay = Relay(); adapter = DurableCleanupAdapter()
    original_answers = list(adapter.answer_children)
    with pytest.raises(GradeSettlementError):
        restart(path, relay, adapter).grade(prepared(marker=True), force=True)
    pending = JsonGradingRecordStore(path).snapshot()[target().page_id]
    assert pending["phase"] == PHASE_RESULT_VERIFIED
    assert pending["cleanup_block_ids"] == ["old-marker", "old-token", "old-time", "old-score", "old-code"]

    result = restart(path, relay, adapter).grade(prepared(marker=True))

    assert result["status"] == "completed" and relay.calls == 1
    assert adapter.block_ids == [] and adapter.answer_children == original_answers
    assert adapter.archive_calls.count("old-marker") == 2



def test_relay_started_controller_result_has_no_fabricated_settlement(tmp_path):
    path = tmp_path / "relay-started-controller.json"
    JsonGradingRecordStore(path).put(target().page_id, {
        "version": 2, "status": "pending", "phase": PHASE_RELAY_STARTED,
        "operation_id": "ambiguous", "page_id": target().page_id,
        "answer_fingerprint": "answers-v1",
    })
    service = restart(path, Relay(), ExternalStateAdapter())
    service.directory_service.resolve_exercise_launch = lambda _exercise_id: type("Launch", (), {"status": "opened"})()
    service.prepare = lambda _exercise_id: prepared(marker=True)
    controller = GradeJobController(service)
    body, code = controller.start(target().page_id)
    assert code == 202
    import time
    for _ in range(100):
        result, _ = controller.snapshot(body["job_id"])
        if result["status"] != "running":
            break
        time.sleep(0.005)
    assert result["status"] == "blocked"
    assert "points_charged" not in result and "points_remaining" not in result


def test_partial_matching_result_envelope_blocks_without_second_append():
    from pku_sync.panel.exercise_grader import RealGradingPageAdapter
    from pku_sync.notion import markdown_to_blocks
    import pku_sync.panel.exercise_grader as grading_module

    blocks = markdown_to_blocks("## \u6279\u6539\u7ed3\u679c\n\nPKU_GRADE_OPERATION:operation")
    class Client:
        append_calls = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def list_children(self, _page_id): return blocks
        def append_blocks(self, *_args, **_kwargs): self.append_calls += 1
    client = Client()
    original = grading_module.get_client
    grading_module.get_client = lambda _settings: client
    try:
        with pytest.raises(Exception, match="\u4e0d\u5b8c\u6574"):
            RealGradingPageAdapter(object()).ensure_result(
                target(), operation_id="operation", content=CONTENT, score=72,
                graded_at="now", regrade=False,
            )
    finally:
        grading_module.get_client = original
    assert client.append_calls == 0


def test_e2e_result_marker_is_preserved_in_operation_envelope():
    from pku_sync.panel.exercise_grader import _operation_result_markdown
    from pku_sync.notion import markdown_to_blocks
    blocks = markdown_to_blocks(_operation_result_markdown(
        "operation", CONTENT, score=72, graded_at="now", marker="E2E_GRADE_RESULT",
    ))
    assert blocks[0]["heading_2"]["rich_text"][0]["text"]["content"] == "E2E_GRADE_RESULT"


def test_required_wrong_answers_block_completion_without_configured_hub():
    from pku_sync.panel.exercise_grader import RealGradingPageAdapter, GradeBlocked
    settings = type("Settings", (), {"notion_wrong_answer_hub_id": ""})()
    adapter = RealGradingPageAdapter(settings)
    assert adapter.verify_wrong_answer(target(), {"number": 2}, key="operation:q2", operation_id="operation") is False
    with pytest.raises(GradeBlocked, match="\u672a\u914d\u7f6e"):
        adapter.ensure_wrong_answer(target(), {"number": 2}, key="operation:q2", operation_id="operation")



def test_restart_from_wrong_answers_verified_rechecks_actual_external_state(tmp_path):
    path = tmp_path / "verify-final-state.json"; relay, adapter = Relay(), ExternalStateAdapter()
    record = {
        "version": 2, "status": "pending", "phase": PHASE_WRONG_ANSWERS_VERIFIED,
        "operation_id": "stable-operation", "operation": "grade",
        "page_id": target().page_id, "page_url": target().page_url,
        "course_id": target().course_id, "course_title": target().course_title,
        "title": target().title, "scope": target().scope,
        "answer_fingerprint": "answers-v1", "answer_provenance": "known",
        "regrade": True, "content": CONTENT, "score": 72,
        "graded_at": "2026-09-20T12:00:00+08:00", "points_charged": 3.0,
        "points_remaining": 17.0, "result_page_url": target().page_url,
    }
    adapter.result_operations.append("stable-operation")
    JsonGradingRecordStore(path).put(target().page_id, record)

    with pytest.raises(GradeSettlementError, match="\u9519\u9898"):
        restart(path, relay, adapter).grade(prepared(marker=True))

    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_WRONG_ANSWERS_VERIFIED
    assert relay.calls == 0



def test_cleanup_does_not_archive_partial_or_user_authored_marker_section():
    from pku_sync.panel.exercise_grader import _find_operation, _operation_result_markdown, _stale_result_blocks
    from pku_sync.notion import markdown_to_blocks
    partial = markdown_to_blocks("## \u6279\u6539\u7ed3\u679c\n\n```text\nstudent code\n```")
    current = markdown_to_blocks(_operation_result_markdown("current", CONTENT, score=72, graded_at="now"))
    blocks = partial + current
    assert _stale_result_blocks(blocks, _find_operation(blocks, "current")) == []
