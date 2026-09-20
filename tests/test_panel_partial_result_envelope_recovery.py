"""Additive tests for durable partial result-envelope recovery."""
from __future__ import annotations
import copy, json
from pathlib import Path
import httpx, pytest
import pku_sync.notion as notion_module
import pku_sync.panel.exercise_grader as grader_module
from pku_sync.notion import NotionClient, markdown_to_blocks
from pku_sync.notion_meta.entities import PageRef
from pku_sync.panel.exercise_grader import (PHASE_COMPLETED, PHASE_RELAY_SUCCEEDED, ExerciseGrader, GradeBlocked, GradeSettlementError, GradeTarget, GradingInput, RealGradingPageAdapter, _operation_result_markdown)
from pku_sync.panel.exercises import JsonGradingRecordStore

CONTENT = json.dumps({"score": 72, "questions": [{"number": 2, "type": "判断", "score": 0, "max_score": 2}, {"number": 5, "type": "论述", "score": 0, "max_score": 2}]}, ensure_ascii=False)
GRADED_AT = "2026-09-20T12:00:00+08:00"

def target():
    return GradeTarget("grade", "1"*32, "https://www.notion.so/"+"1"*32, "考前练习", "2"*32, "计算机网络", "考前练习")

def prepared(marker=False):
    return target(), GradingInput(prompt="grade-prompt", unanswered=[], marker_present=marker, answer_fingerprint="answers-v1", answer_provenance="known")

def with_ids(blocks, start=1):
    result = copy.deepcopy(blocks)
    for index, block in enumerate(result, start): block["id"] = f"{index:032x}"
    return result

def plan(operation_id="operation"):
    return markdown_to_blocks(_operation_result_markdown(operation_id, CONTENT, score=72, graded_at=GRADED_AT))

class PageClient:
    def __init__(self, blocks):
        self.blocks, self.appended, self.archived = list(blocks), [], []
        self.raise_after_commit = False
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def list_children(self, _page_id): return copy.deepcopy(self.blocks)
    def append_blocks(self, _page_id, blocks, **_kwargs):
        created = with_ids(blocks, len(self.blocks)+1); self.blocks.extend(created); self.appended.append(copy.deepcopy(blocks))
        if self.raise_after_commit: self.raise_after_commit = False; raise OSError("response lost after commit")
        return created
    def archive_block(self, block_id, **_kwargs):
        self.archived.append(block_id); self.blocks = [b for b in self.blocks if b.get("id") != block_id]
        return {"archived": True}

def ensure(adapter, append_plan):
    adapter.ensure_result(target(), operation_id="operation", content=CONTENT, score=72, graded_at=GRADED_AT, regrade=False, result_marker="批改结果", append_plan=append_plan)

def test_exact_tail_prefix_retires_full_prefix_then_appends_full_plan(monkeypatch):
    frozen = plan(); prefix = with_ids(frozen[:3]); client = PageClient(prefix); monkeypatch.setattr(grader_module, "get_client", lambda _s: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.archived == [prefix[0]["id"], prefix[2]["id"], prefix[1]["id"]]
    assert client.appended == [frozen]
    assert len([block for block in client.blocks if grader_module._plain(block) == "PKU_GRADE_OPERATION:operation"]) == 1

def test_owned_prefix_before_user_content_retires_only_prefix(monkeypatch):
    frozen = plan(); prefix = with_ids(frozen[:3]); user = with_ids(markdown_to_blocks("### 我的补充\n\n不要删除"), 100)
    client = PageClient(prefix + user); monkeypatch.setattr(grader_module, "get_client", lambda _s: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.archived == [prefix[0]["id"], prefix[2]["id"], prefix[1]["id"]] and client.appended == [frozen]
    assert any(grader_module._plain(b) == "不要删除" for b in client.blocks)

@pytest.mark.parametrize("blocks", [lambda p: with_ids([p[0], p[1], markdown_to_blocks("批改时间：别的时间")[0]]), lambda p: with_ids([p[0], p[1], p[1]]), lambda p: with_ids([p[0]])])
def test_ambiguous_or_mismatched_ownership_never_mutates(monkeypatch, blocks):
    frozen = plan(); client = PageClient(blocks(frozen)); monkeypatch.setattr(grader_module, "get_client", lambda _s: client)
    with pytest.raises(GradeBlocked): ensure(RealGradingPageAdapter(object()), frozen)
    assert client.appended == [] and client.archived == []


def test_regrade_appends_after_structurally_complete_stale_envelope(monkeypatch):
    old = with_ids(markdown_to_blocks(_operation_result_markdown(
        "old-operation", "{}", score=60, graded_at="old"
    )))
    frozen = plan()
    client = PageClient(old)
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)

    RealGradingPageAdapter(object()).ensure_result(
        target(), operation_id="operation", content=CONTENT, score=72,
        graded_at=GRADED_AT, regrade=True, result_marker="\u6279\u6539\u7ed3\u679c",
        append_plan=frozen,
    )

    assert client.appended == [frozen]
    assert client.archived == []


def test_interrupted_prefix_retirement_recovers_around_verified_full_plan(monkeypatch):
    frozen = plan()
    prefix = with_ids(frozen[:3])
    user = with_ids(markdown_to_blocks("### \u6211\u7684\u8865\u5145\n\n\u4e0d\u8981\u5220\u9664"), 100)
    client = PageClient(prefix + user)
    original_archive = client.archive_block
    calls = 0

    def fail_second_archive(block_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("archive did not commit")
        return original_archive(block_id, **kwargs)

    client.archive_block = fail_second_archive
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)

    with pytest.raises(OSError):
        ensure(RealGradingPageAdapter(object()), frozen)
    client.archive_block = original_archive
    ensure(RealGradingPageAdapter(object()), frozen)

    assert client.appended == [frozen]
    assert len([b for b in client.blocks if grader_module._plain(b) == "PKU_GRADE_OPERATION:operation"]) == 1
    assert any(grader_module._plain(b) == "\u4e0d\u8981\u5220\u9664" for b in client.blocks)

def test_append_exception_accepts_exact_complete_envelope(monkeypatch):
    frozen = plan(); client = PageClient([]); client.raise_after_commit = True; monkeypatch.setattr(grader_module, "get_client", lambda _s: client)
    ensure(RealGradingPageAdapter(object()), frozen)
    assert client.appended == [frozen]
    assert len([b for b in client.blocks if grader_module._plain(b) == "PKU_GRADE_OPERATION:operation"]) == 1

class Relay:
    def __init__(self): self.calls = 0
    def quota(self): return {"available": True, "llm_points_remaining": 20.0}
    def grade(self, _prompt): self.calls += 1; return {"content": CONTENT, "points_charged": 3.0}

class Directory:
    def __init__(self, path: Path): self.grading_record_store = JsonGradingRecordStore(path); self.local_grading_records = self.grading_record_store.snapshot()
    def record_grading(self, exercise_id, record): self.grading_record_store.put(exercise_id, record); self.local_grading_records[exercise_id] = copy.deepcopy(record)

class ObservePlanAdapter:
    def __init__(self, directory): self.directory, self.seen = directory, None
    def ensure_result(self, selected, *, append_plan, **_kwargs):
        durable = self.directory.grading_record_store.snapshot()[selected.page_id]; self.seen = copy.deepcopy(append_plan)
        assert durable["phase"] == PHASE_RELAY_SUCCEEDED and durable["append_plan"] == append_plan
        raise OSError("stop at first mutation")

def test_append_plan_is_frozen_durably_before_first_result_mutation(tmp_path):
    path = tmp_path/"grading.json"; directory = Directory(path); relay = Relay(); adapter = ObservePlanAdapter(directory)
    with pytest.raises(GradeSettlementError): ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter, clock=lambda: GRADED_AT).grade(prepared())
    record = JsonGradingRecordStore(path).snapshot()[target().page_id]
    assert record["append_plan"] == adapter.seen == plan(record["operation_id"]); assert relay.calls == 1

class Backend:
    def __init__(self):
        self.answers = with_ids(markdown_to_blocks("### 第 1 题\n\n答案：原始答案"), 500); self.blocks = copy.deepcopy(self.answers); self.rows=[]; self.patch_calls=0; self.fail_second_patch=True
    def handler(self, request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/children"): return httpx.Response(200, json={"results": copy.deepcopy(self.blocks), "has_more": False})
        if request.method == "PATCH" and path.endswith("/children"):
            self.patch_calls += 1; payload=json.loads(request.content); created=with_ids(payload["children"], 1000+len(self.blocks)); self.blocks.extend(created)
            if self.fail_second_patch and self.patch_calls == 2: self.fail_second_patch=False; return httpx.Response(500, json={"message":"response lost"})
            return httpx.Response(200, json={"results": created})
        if request.method == "GET" and "/v1/databases/" in path: return httpx.Response(200, json={"properties":{"错题名称":{"type":"title"}}})
        if request.method == "POST" and path.endswith("/query"):
            title=json.loads(request.content)["filter"]["title"]["equals"]; return httpx.Response(200, json={"results":[r for r in self.rows if r["title"]==title], "has_more":False})
        if request.method == "POST" and path == "/v1/pages":
            title=json.loads(request.content)["properties"]["错题名称"]["title"][0]["text"]["content"]; row={"id":f"{2000+len(self.rows):032x}","title":title}; self.rows.append(row); return httpx.Response(200,json=row)
        if request.method == "DELETE" and "/v1/blocks/" in path:
            block_id=path.rsplit("/",1)[-1].replace("-",""); self.blocks=[b for b in self.blocks if str(b.get("id","")).replace("-","") != block_id]; return httpx.Response(200,json={"archived":True})
        return httpx.Response(404,json={"message":f"unhandled {request.method} {path}"})

class IntegratedDirectory(Directory):
    def wrong_answer_database_identity(self): return PageRef(id="3"*32, url="https://www.notion.so/"+"3"*32, title="知识点与错题")

def test_real_batched_partial_commit_restart_completes_result_and_wrong_answers(tmp_path, monkeypatch):
    path=tmp_path/"grading.json"; backend=Backend(); relay=Relay()
    monkeypatch.setattr(grader_module, "get_client", lambda _s: NotionClient("secret", transport=httpx.MockTransport(backend.handler)))
    monkeypatch.setattr(notion_module, "_BLOCK_BATCH", 2)
    directory=IntegratedDirectory(path); adapter=RealGradingPageAdapter(object(), directory); adapter.preflight_wrong_answer_database()
    with pytest.raises(GradeSettlementError): ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter, clock=lambda:GRADED_AT).grade(prepared())
    pending=JsonGradingRecordStore(path).snapshot()[target().page_id]
    assert pending["phase"] == PHASE_RELAY_SUCCEEDED and len(pending["append_plan"]) == 5
    assert backend.blocks[:len(backend.answers)] == backend.answers and len(backend.blocks) == len(backend.answers)+4
    restarted=IntegratedDirectory(path); adapter2=RealGradingPageAdapter(object(), restarted); adapter2.preflight_wrong_answer_database()
    result=ExerciseGrader(directory_service=restarted, relay=relay, page_adapter=adapter2, clock=lambda:"unused").grade(prepared(True))
    tokens=[b for b in backend.blocks if grader_module._plain(b).startswith("PKU_GRADE_OPERATION:")]
    assert result["status"] == "completed" and relay.calls == 1 and len(tokens) == 1
    assert backend.blocks[:len(backend.answers)] == backend.answers
    assert len(backend.rows) == 2 and len({r["title"] for r in backend.rows}) == 2
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_COMPLETED
