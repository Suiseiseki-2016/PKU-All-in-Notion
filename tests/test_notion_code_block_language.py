"""Additive tests: every Notion code block carries a defined code.language.

Covers the Window D resume blocker (evidence/56-resume-traceback.txt): Notion
400 `code.language should be defined` for fenced json grade-result envelopes.
Backs the pku_sync/notion.py markdown_to_blocks fix (preserve supported fence
languages such as json, map aliases, fall back to Notion's "plain text" enum)
and the bounded normalization of stored language-less append plans in
pku_sync/panel/exercise_grader.py. All transport legs run through
httpx.MockTransport or an in-memory PageClient; zero relay/Notion spend.
"""
from __future__ import annotations
import copy, json
from pathlib import Path
import httpx, pytest
import pku_sync.notion as notion_module
import pku_sync.panel.exercise_grader as grader_module
from pku_sync.notion import NotionClient, markdown_to_blocks
from pku_sync.notion_meta.entities import PageRef
from pku_sync.panel.exercise_grader import (
    PHASE_COMPLETED, PHASE_RELAY_SUCCEEDED,
    ExerciseGrader, GradeBlocked, GradeTarget, GradingInput,
    RealGradingPageAdapter, _operation_result_markdown,
)
from pku_sync.panel.exercises import JsonGradingRecordStore

CONTENT = json.dumps({"score": 72, "questions": [
    {"number": 2, "type": "判断", "score": 0, "max_score": 2},
    {"number": 5, "type": "论述", "score": 0, "max_score": 2},
]}, ensure_ascii=False)
GRADED_AT = "2026-09-20T12:00:00+08:00"

# The documented Notion blocks API code.language enum (developer docs,
# code block `language` field). Used to prove every emitted value is valid.
_NOTION_CODE_LANGUAGES = frozenset({
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript",
    "c++", "c#", "css", "dart", "diff", "docker", "elixir", "elm",
    "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go", "graphql",
    "groovy", "haskell", "html", "java", "javascript", "json", "julia",
    "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile",
    "markdown", "markup", "matlab", "mermaid", "nix", "objective-c",
    "ocaml", "pascal", "perl", "php", "plain text", "powershell", "prolog",
    "protobuf", "python", "r", "reason", "ruby", "rust", "sass", "scala",
    "scheme", "scss", "shell", "sql", "swift", "typescript", "vb.net",
    "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
    "java/c/c++/c#",
})


def target():
    return GradeTarget("grade", "1" * 32, "https://www.notion.so/" + "1" * 32,
                       "考前练习", "2" * 32, "计算机网络", "考前练习")


def prepared(marker=False):
    return target(), GradingInput(prompt="grade-prompt", unanswered=[],
                                  marker_present=marker, answer_fingerprint="answers-v1",
                                  answer_provenance="known")


def with_ids(blocks, start=1):
    result = copy.deepcopy(blocks)
    for index, block in enumerate(result, start):
        block["id"] = f"{index:032x}"
    return result


def plan(operation_id="operation"):
    return markdown_to_blocks(
        _operation_result_markdown(operation_id, CONTENT, score=72, graded_at=GRADED_AT)
    )


def legacy_plan(operation_id="operation", *, stale_language=None):
    """A stored append plan as the pre-fix writer produced it: code blocks
    carry no language (or a stale invalid value) and are otherwise identical."""
    blocks = plan(operation_id)
    for block in blocks:
        if block.get("type") == "code":
            if stale_language is None:
                block["code"].pop("language", None)
            else:
                block["code"]["language"] = stale_language
    return blocks


class PageClient:
    """In-memory Notion page client modeled on the durable recovery tests."""

    def __init__(self, blocks):
        self.blocks, self.appended, self.archived = list(blocks), [], []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_children(self, _page_id):
        return copy.deepcopy(self.blocks)

    def append_blocks(self, _page_id, blocks, **_kwargs):
        created = with_ids(blocks, len(self.blocks) + 1)
        self.blocks.extend(created)
        self.appended.append(copy.deepcopy(blocks))
        return created

    def archive_block(self, block_id, **_kwargs):
        self.archived.append(block_id)
        self.blocks = [b for b in self.blocks if b.get("id") != block_id]
        return {"archived": True}


def ensure(adapter, append_plan):
    adapter.ensure_result(
        target(), operation_id="operation", content=CONTENT, score=72,
        graded_at=GRADED_AT, regrade=False, result_marker="批改结果",
        append_plan=append_plan,
    )


class Relay:
    def __init__(self):
        self.calls = 0

    def quota(self):
        return {"available": True, "llm_points_remaining": 20.0}

    def grade(self, _prompt):
        self.calls += 1
        return {"content": CONTENT, "points_charged": 3.0}


class Directory:
    def __init__(self, path: Path):
        self.grading_record_store = JsonGradingRecordStore(path)
        self.local_grading_records = self.grading_record_store.snapshot()

    def record_grading(self, exercise_id, record):
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = copy.deepcopy(record)


class NotionBackend:
    """MockTransport backend with a real page plus wrong-answer database."""

    def __init__(self):
        self.blocks: list[dict] = []
        self.rows: list[dict] = []
        self.patch_payloads: list[dict] = []

    def handler(self, request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/children"):
            return httpx.Response(200, json={
                "results": copy.deepcopy(self.blocks), "has_more": False,
            })
        if request.method == "PATCH" and path.endswith("/children"):
            payload = json.loads(request.content)
            self.patch_payloads.append(payload)
            created = with_ids(payload["children"], 1000 + len(self.blocks))
            self.blocks.extend(created)
            return httpx.Response(200, json={"results": created})
        if request.method == "GET" and "/v1/databases/" in path:
            return httpx.Response(200, json={
                "properties": {"错题名称": {"type": "title"}},
            })
        if request.method == "POST" and path.endswith("/query"):
            title = json.loads(request.content)["filter"]["title"]["equals"]
            return httpx.Response(200, json={
                "results": [r for r in self.rows if r["title"] == title],
                "has_more": False,
            })
        if request.method == "POST" and path == "/v1/pages":
            title = json.loads(request.content)["properties"]["错题名称"]["title"][0]["text"]["content"]
            row = {"id": f"{2000 + len(self.rows):032x}", "title": title}
            self.rows.append(row)
            return httpx.Response(200, json=row)
        return httpx.Response(404, json={"message": f"unhandled {request.method} {path}"})


class IntegratedDirectory(Directory):
    def wrong_answer_database_identity(self):
        return PageRef(id="3" * 32, url="https://www.notion.so/" + "3" * 32,
                       title="知识点与错题")


# -- markdown_to_blocks: language emission ------------------------------------


def test_fenced_json_language_preserved():
    blocks = markdown_to_blocks('```json\n{"a": 1}\n```')
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "json"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == '{"a": 1}'


def test_untagged_fence_falls_back_to_plain_text():
    blocks = markdown_to_blocks("```\n- 不是列表\n缩进保留\n```")
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "plain text"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "- 不是列表\n缩进保留"


@pytest.mark.parametrize("fence", ["```unknownlang", "```foobar", "```not-a-real-lang"])
def test_unknown_fence_language_falls_back_to_plain_text(fence):
    blocks = markdown_to_blocks(fence + "\ncode\n```")
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "plain text"


@pytest.mark.parametrize("fence,expected", [
    ("```py", "python"), ("```python3", "python"), ("```py3", "python"),
    ("```js", "javascript"), ("```jsx", "javascript"),
    ("```ts", "typescript"), ("```tsx", "typescript"),
    ("```yml", "yaml"), ("```md", "markdown"),
    ("```sh", "shell"), ("```zsh", "shell"),
    ("```text", "plain text"), ("```txt", "plain text"),
    ("```cpp", "c++"), ("```csharp", "c#"), ("```cs", "c#"),
    ("```objc", "objective-c"),
])
def test_common_fence_aliases_map_to_notion_languages(fence, expected):
    blocks = markdown_to_blocks(fence + "\ncode\n```")
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == expected


def test_language_matching_is_case_insensitive():
    assert markdown_to_blocks("```JSON\nx\n```")[0]["code"]["language"] == "json"
    assert markdown_to_blocks("```C++\nx\n```")[0]["code"]["language"] == "c++"
    assert markdown_to_blocks("```Python\nx\n```")[0]["code"]["language"] == "python"


def test_extra_fence_info_attributes_ignored():
    assert markdown_to_blocks("```python {.numberLines}\nx\n```")[0]["code"]["language"] == "python"


@pytest.mark.parametrize("fence,value", [
    ("json", "json"), ("python", "python"), ("shell", "shell"),
    ("yaml", "yaml"), ("markdown", "markdown"), ("text", "plain text"),
    ("bogus", "plain text"), ("", "plain text"),
])
def test_every_emitted_language_is_a_documented_notion_value(fence, value):
    opening = "```" + fence
    blocks = markdown_to_blocks(opening + "\nx\n```")
    emitted = blocks[0]["code"]["language"]
    assert emitted == value
    assert emitted in _NOTION_CODE_LANGUAGES


# -- fake transport: the wire payload carries a defined language --------------


def _capture_append(blocks):
    captured: list[dict] = []

    def handler(request):
        if request.method == "PATCH" and request.url.path.endswith("/children"):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": "unhandled"})

    with NotionClient("secret", transport=httpx.MockTransport(handler)) as client:
        client.append_blocks("a" * 32, blocks, retry=False)
    return captured[0]["children"]


def test_append_transport_receives_defined_language_for_json_envelope():
    blocks = markdown_to_blocks(
        _operation_result_markdown("operation", CONTENT, score=72, graded_at=GRADED_AT)
    )
    sent = _capture_append(blocks)
    code = sent[-1]
    assert code["type"] == "code"
    assert code["code"]["language"] == "json"
    assert code["code"]["rich_text"][0]["text"]["content"] == CONTENT


def test_append_transport_receives_plain_text_for_unknown_fence():
    sent = _capture_append(markdown_to_blocks("```\n# bare shell snippet\n```\n"))
    code = sent[0]
    assert code["type"] == "code"
    assert code["code"]["language"] == "plain text"
    assert code["code"]["rich_text"][0]["text"]["content"] == "# bare shell snippet"


# -- bounded normalization of language-less stored append plans ---------------


@pytest.mark.parametrize("stale_language", [None, "JSON", "bogus"])
def test_legacy_append_plan_is_normalized_before_append(monkeypatch, stale_language):
    legacy = legacy_plan(stale_language=stale_language)
    preserved_rich_text = copy.deepcopy(legacy[-1]["code"]["rich_text"])
    untouched = copy.deepcopy(legacy)
    client = PageClient([])
    monkeypatch.setattr(grader_module, "get_client", lambda _s: client)

    ensure(RealGradingPageAdapter(object()), legacy)

    sent = client.appended[0]
    assert len(sent) == len(legacy)
    code = sent[-1]
    assert code["type"] == "code"
    assert code["code"]["language"] == "json"
    assert code["code"]["rich_text"] == preserved_rich_text
    # the stored plan object itself is not mutated; only the append copy is
    assert legacy == untouched
    assert any(b.get("code", {}).get("language") != "json" for b in legacy)


def test_durable_resume_normalizes_language_less_stored_plan_and_appends(tmp_path, monkeypatch):
    path = tmp_path / "grading.json"
    backend = NotionBackend()
    monkeypatch.setattr(grader_module, "get_client", lambda _s: NotionClient(
        "secret", transport=httpx.MockTransport(backend.handler)
    ))
    stored = legacy_plan()
    record = {
        "version": 2, "status": "pending", "phase": PHASE_RELAY_SUCCEEDED,
        "operation_id": "operation", "operation": "grade",
        "page_id": target().page_id, "page_url": target().page_url,
        "course_id": target().course_id, "course_title": target().course_title,
        "title": target().title, "scope": target().scope,
        "answer_fingerprint": "answers-v1", "answer_provenance": "known",
        "regrade": False, "result_marker": "批改结果",
        "content": CONTENT, "score": 72, "graded_at": GRADED_AT,
        "points_charged": 3.0, "points_remaining": 17.0,
        "result_page_url": target().page_url,
        "append_plan": stored,
    }
    JsonGradingRecordStore(path).put(target().page_id, record)

    relay = Relay()
    directory = IntegratedDirectory(path)
    adapter = RealGradingPageAdapter(object(), directory)
    adapter.preflight_wrong_answer_database()

    result = ExerciseGrader(directory_service=directory, relay=relay,
                            page_adapter=adapter, clock=lambda: GRADED_AT).grade(prepared(marker=True))

    assert result["status"] == "completed"
    assert relay.calls == 0  # durable retry: zero relay spend
    assert backend.patch_payloads, "resume must reach the result append"
    code = backend.patch_payloads[-1]["children"][-1]
    assert code["type"] == "code" and code["code"]["language"] == "json"
    assert code["code"]["rich_text"][0]["text"]["content"] == CONTENT
    assert len(backend.rows) == 2
    assert JsonGradingRecordStore(path).snapshot()[target().page_id]["phase"] == PHASE_COMPLETED
