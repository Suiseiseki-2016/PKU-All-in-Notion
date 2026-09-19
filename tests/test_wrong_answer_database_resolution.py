"""Wrong-answer database discovery, production wiring, and write safety."""
from __future__ import annotations

import json

import pytest

import notion_meta_fake as fake
from pku_sync.notion_meta import IdentityCache, NotionDirectory
from pku_sync.notion_meta.entities import PageRef
from pku_sync.panel.directory import DirectoryApiError, DirectoryService
from pku_sync.panel.exercise_grader import ExerciseGrader, GradeBlocked, GradeTarget, RealGradingPageAdapter, make_grading_service

DB_WRONG = fake.synth(33)
DB_WRONG_DUPLICATE = fake.synth(34)
WRONG_TITLE = "知识点与错题"
SCHEMA_HONEYPOT = "HONEYPOT_SCHEMA_PRIVATE"
ROW_HONEYPOT = "HONEYPOT_WRONG_ANSWER_BODY"


def _workspace(*, matches: int = 1):
    ws = fake.build_verified_workspace()
    for database_id in (DB_WRONG, DB_WRONG_DUPLICATE)[:matches]:
        ws.children[fake.HUB].append(fake.child_database_block(database_id, f"  {WRONG_TITLE}  "))
        ws.databases[database_id] = {
            "id": database_id,
            "properties": {
                "错题名称": {"id": "title", "type": "title", "title": {}},
                SCHEMA_HONEYPOT: {"id": "private", "type": "rich_text", "rich_text": {}},
            },
        }
        ws.rows[database_id] = [{"id": fake.synth(900), "private": ROW_HONEYPOT}]
    return ws


def _directory(ws, *, cache_path=None):
    client = ws.client()
    return client, NotionDirectory(client, semester=fake.SEMESTER, cache_path=cache_path)


def test_unique_normalized_direct_child_is_backend_only_and_uses_no_extra_search():
    client, directory = _directory(_workspace())
    data = directory.load()
    assert data.wrong_answer_database == PageRef(id=DB_WRONG, url=fake.notion_url(DB_WRONG), title=WRONG_TITLE)
    assert data.wrong_answer_setup_error == ""
    assert [call for call in client.calls if call[0] == "search"] == [("search", "Class Notes", "page")]
    assert ("get_database", DB_WRONG) not in client.calls
    assert ("query_database", DB_WRONG) not in client.calls
    dumped = json.dumps(data.to_dict(), ensure_ascii=False)
    assert DB_WRONG not in dumped and WRONG_TITLE not in dumped


@pytest.mark.parametrize(("matches", "message"), [(0, "未找到"), (2, "找到 2 个")])
def test_missing_or_duplicate_database_is_an_explicit_backend_preflight_failure(matches, message):
    _client, directory = _directory(_workspace(matches=matches))
    data = directory.load()
    service = DirectoryService(type("Provider", (), {"load": lambda _self: data})())
    service.load()
    with pytest.raises(DirectoryApiError, match=message):
        service.wrong_answer_database_identity()
    assert data.wrong_answer_database is None
    assert message in data.wrong_answer_setup_error


def test_cache_round_trip_contains_only_page_ref_and_old_cache_refreshes(tmp_path):
    path = tmp_path / "identity_cache.json"
    _client, directory = _directory(_workspace(), cache_path=path)
    directory.load()
    cached = IdentityCache(path).load()
    assert cached["wrong_answer_database"] == {"id": DB_WRONG, "url": fake.notion_url(DB_WRONG), "title": WRONG_TITLE}
    dumped = json.dumps(cached, ensure_ascii=False)
    assert SCHEMA_HONEYPOT not in dumped and ROW_HONEYPOT not in dumped
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"version": 1, "hub": {"id": fake.HUB}}), encoding="utf-8")
    assert IdentityCache(old).load() is None


class _Directory:
    def __init__(self, identity: PageRef | None, error: str = ""):
        self.identity = identity
        self.error = error
        self.local_grading_records = {}
        self.grading_record_store = None

    def exercise_grade_target(self, _exercise_id):
        return {"page_id": "exercise-page", "page_url": "https://www.notion.so/exercise-page", "title": "考前练习", "course_id": "course-page", "course_title": "计算机网络", "scope": "考前练习"}

    def wrong_answer_database_identity(self):
        if self.identity is None:
            raise DirectoryApiError(503, self.error or "错题数据库不可用。")
        return self.identity


class _Relay:
    def __init__(self): self.calls = 0
    def quota(self): return {"available": True, "llm_points_remaining": 20}
    def grade(self, _prompt):
        self.calls += 1
        return {"content": json.dumps({"score": 100}), "points_charged": 1}


class _DatabaseClient:
    def __init__(self, *, ambiguous=False, duplicate_titles=False):
        self.ambiguous = ambiguous
        self.duplicate_titles = duplicate_titles
        self.calls = []
        self.rows = []

    def __enter__(self): return self
    def __exit__(self, *_args): return None

    def get_database(self, database_id):
        self.calls.append(("get_database", database_id))
        properties = {
            "实际标题": {"id": "title", "type": "title", "title": {}},
            SCHEMA_HONEYPOT: {"id": "private", "type": "rich_text", "rich_text": {}},
        }
        if self.duplicate_titles:
            properties["另一个标题"] = {"id": "also-title", "type": "title", "title": {}}
        return {"id": database_id, "properties": properties}

    def query_database(
        self, database_id, *, filter=None, page_size=100, max_results=None
    ):
        self.calls.append(("query_database", database_id, filter, page_size, max_results))
        expected = ((filter or {}).get("title") or {}).get("equals")
        return [row for row in self.rows if row["title"] == expected]

    def create_database_row(self, database_id, properties, *, retry=True):
        self.calls.append(("create_database_row", database_id, properties, retry))
        title = properties["实际标题"]["title"][0]["text"]["content"]
        self.rows.append({"id": "created-row", "title": title, "private": ROW_HONEYPOT})
        if self.ambiguous:
            self.ambiguous = False
            raise OSError("response lost after commit")
        return self.rows[-1]

    def get_page(self, _page_id): return {"parent": {"page_id": "course-page"}}
    def list_children(self, _page_id): return []


def _target():
    return GradeTarget("grade", "exercise-page", "https://www.notion.so/exercise-page", "考前练习", "course-page", "计算机网络", "考前练习")


def test_production_grading_preflight_blocks_missing_database_before_relay(monkeypatch):
    directory = _Directory(None, "未找到「知识点与错题」数据库，请先完成 Notion 设置。")
    relay = _Relay()
    adapter = RealGradingPageAdapter(object(), directory)
    monkeypatch.setattr("pku_sync.panel.exercise_grader.get_client", lambda _settings: (_ for _ in ()).throw(AssertionError("Notion must not be called")))
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    with pytest.raises(GradeBlocked, match="未找到"):
        grader.prepare("exercise-page")
    assert relay.calls == 0


def test_schema_aware_database_row_write_and_ambiguous_create_reconcile(monkeypatch):
    identity = PageRef(id=DB_WRONG, url=fake.notion_url(DB_WRONG), title=WRONG_TITLE)
    directory = _Directory(identity)
    client = _DatabaseClient(ambiguous=True)
    monkeypatch.setattr("pku_sync.panel.exercise_grader.get_client", lambda _settings: client)
    adapter = RealGradingPageAdapter(object(), directory)
    adapter.preflight_wrong_answer_database()
    question = {"number": 2, "type": "判断", "wrong_answer_key": "source-q2"}
    adapter.ensure_wrong_answer(_target(), question, key="operation:source-q2", operation_id="operation")
    assert adapter.verify_wrong_answer(_target(), question, key="operation:source-q2", operation_id="operation")
    assert len(client.rows) == 1
    create = next(call for call in client.calls if call[0] == "create_database_row")
    assert create[1] == DB_WRONG and set(create[2]) == {"实际标题"} and create[3] is False
    assert all(call[0] != "list_child_pages" for call in client.calls)


def test_schema_with_non_unique_title_property_fails_before_charge(monkeypatch):
    identity = PageRef(id=DB_WRONG, url=fake.notion_url(DB_WRONG), title=WRONG_TITLE)
    directory = _Directory(identity)
    relay = _Relay()
    client = _DatabaseClient(duplicate_titles=True)
    monkeypatch.setattr("pku_sync.panel.exercise_grader.get_client", lambda _settings: client)
    adapter = RealGradingPageAdapter(object(), directory)
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    with pytest.raises(GradeBlocked, match="标题属性"):
        grader.prepare("exercise-page")
    assert relay.calls == 0


def test_production_factory_injects_directory_and_panel_payload_stays_private():
    data = _directory(_workspace())[1].load()
    directory = DirectoryService(type("Provider", (), {"load": lambda _self: data})())
    payload = directory.load()
    service = make_grading_service(object(), directory, _Relay())
    assert service.page_adapter.directory_service is directory
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in (DB_WRONG, WRONG_TITLE, SCHEMA_HONEYPOT, ROW_HONEYPOT):
        assert secret not in dumped


def test_preflight_rebinds_after_directory_refresh(monkeypatch):
    first = PageRef(id=DB_WRONG, url=fake.notion_url(DB_WRONG), title=WRONG_TITLE)
    second = PageRef(id=DB_WRONG_DUPLICATE, url=fake.notion_url(DB_WRONG_DUPLICATE), title=WRONG_TITLE)
    directory = _Directory(first)
    client = _DatabaseClient()
    monkeypatch.setattr("pku_sync.panel.exercise_grader.get_client", lambda _settings: client)
    adapter = RealGradingPageAdapter(object(), directory)

    adapter.preflight_wrong_answer_database()
    directory.identity = second
    adapter.preflight_wrong_answer_database()

    assert adapter._wrong_answer_database == second
    assert [call[1] for call in client.calls if call[0] == "get_database"] == [DB_WRONG, DB_WRONG_DUPLICATE]
