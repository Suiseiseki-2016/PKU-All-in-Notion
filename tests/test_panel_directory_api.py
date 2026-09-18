"""Panel directory API tests (VAL-META-015..018).

The student directory endpoints — whitelisted payloads, the three material
view groupings, identity-only launch, and the honest sync surface — are
verified here against the seeded-fake startup mode (which runs the REAL M2
adapter over a fake client, so the association/allowlist rules are exercised
exactly as production). Honeypot strings are scanned over every serialized
payload and launch response.

VAL-META-015: directory payload has exactly the whitelisted fields with
identity; honeypot scan clean.
VAL-META-016: 按讲次 / 按资料类型 / 全部资料 groupings behave per spec against
a seeded fixture, items carry stored id/url.
VAL-META-017: launch endpoint identity-only; missing identity → explicit
missing-mapping state + course fallback, 4xx, generic copy; no search calls;
no tokens.
VAL-META-018: sync surface reports syncing/error/empty/done + last-sync
timestamp with user-safe error reasons; failed sync never reports done.
"""

from __future__ import annotations

import datetime
import json
import threading
import time

from fastapi.testclient import TestClient

from pku_sync.panel.directory import (
    STATE_DONE,
    STATE_EMPTY,
    STATE_ERROR,
    STATE_SYNCING,
    SYNC_STATES,
    SYNC_ERROR_GENERIC,
    SYNC_ERROR_HUB_AMBIGUOUS,
    SYNC_ERROR_HUB_NOT_FOUND,
    SYNC_ERROR_NO_INDEX_DB,
    SYNC_ERROR_READ,
    SYNC_ERROR_SCHEMA,
    SYNC_ERROR_UNAVAILABLE,
    DirectoryApiError,
    DirectoryService,
    friendly_sync_time,
    user_safe_reason,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import (
    COURSE_COG,
    COURSE_CS,
    COURSE_DEV,
    COURSE_NET,
    COG_L1,
    DEV_L1,
    HUB,
    NET_L1,
    NET_L2,
    NET_MISSING,
    NET_ROWS,
    PANEL_FORBIDDEN_STRINGS,
    SEMESTER,
    FakeWorkspace,
    build_panel_workspace,
    child_database_block,
    material_row,
    material_schema,
    search_page,
    synth,
)
from pku_sync.panel.webapi import create_app

FIXED_NOW = datetime.datetime(2026, 9, 19, 9, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


def make_client(variant="normal", *, workspace=None, clock=None):
    """A TestClient over the seeded-fake panel plus the underlying service."""
    service = build_fake_directory(variant=variant, workspace=workspace, clock=clock or (lambda: FIXED_NOW))
    app = create_app(directory_service=service)
    return TestClient(app), service, app


def course_from(client, course_id: str) -> dict:
    payload = client.get("/api/directory").json()
    return next(c for c in payload["courses"] if c["id"] == course_id)


# -- VAL-META-015: whitelisted directory payload ---------------------------------


def test_directory_entities_serialize_exactly_the_whitelisted_fields():
    client, _, _ = make_client()
    body = client.get("/api/directory").json()
    assert body["semester"] == SEMESTER
    assert body["sync"]["state"] == STATE_DONE
    assert body["hub"]["id"] == HUB
    assert body["hub"]["url"] == f"https://www.notion.so/{HUB.replace('-', '')}"
    assert body["hub"]["title"] == "Class Notes 2026 下半学期"
    # the hub/course/lecture whitelists are EXACT key sets
    assert set(body["hub"]) == {
        "id", "url", "title", "counts", "sync_label", "scope", "mapping_state", "page_state",
    }
    for course in body["courses"]:
        assert set(course) == {
            "id", "url", "title", "counts", "sync_label", "scope",
            "mapping_state", "page_state", "lectures",
        }
        assert set(course["counts"]) == {"lectures", "materials", "notes", "indexed_materials"}
        for lecture in course["lectures"]:
            assert set(lecture) == {
                "id", "url", "title", "number", "date", "period", "counts",
                "sync_label", "scope", "mapping_state", "page_state",
            }
            assert set(lecture["counts"]) == {
                "confirmed_materials", "inferred_materials", "related_materials",
            }
    # no parent/body/media/path fields can exist anywhere in the payload
    dumped = json.dumps(body, ensure_ascii=False)
    for bomb in ("parent", "来源路径", "备注", "body_text", "media_url"):
        assert bomb not in dumped


def test_directory_payload_excludes_all_forbidden_strings():
    """Honeypot scan over the complete serialized directory payload."""
    client, _, _ = make_client()
    body = client.get("/api/directory")
    assert body.status_code == 200
    dumped = json.dumps(body.json(), ensure_ascii=False)
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert needle not in dumped


def test_directory_counts_come_from_the_adapter_not_hardcoded():
    client, _, _ = make_client()
    body = client.get("/api/directory").json()
    assert body["stats"] == {"courses": 4, "lectures": 4, "materials": 10, "indexed_materials": 5}
    assert body["hub"]["counts"] == {"courses": 4, "lectures": 4, "materials": 10}
    assert course_from(client, COURSE_NET)["counts"] == {
        "lectures": 2, "materials": 7, "notes": 2, "indexed_materials": 3,
    }
    assert course_from(client, COURSE_DEV)["counts"] == {
        "lectures": 1, "materials": 2, "notes": 0, "indexed_materials": 1,
    }
    assert course_from(client, COURSE_COG)["counts"] == {
        "lectures": 1, "materials": 0, "notes": 0, "indexed_materials": 0,
    }
    assert course_from(client, COURSE_CS)["counts"] == {
        "lectures": 0, "materials": 1, "notes": 0, "indexed_materials": 1,
    }


def test_course_and_lecture_mapping_states_derive_from_confirmed_links():
    client, _, _ = make_client()
    net = course_from(client, COURSE_NET)
    assert net["mapping_state"] == "mapped"
    by_number = {l["number"]: l for l in net["lectures"]}
    assert by_number[1]["mapping_state"] == "mapped"
    assert by_number[1]["counts"]["related_materials"] == 2  # confirmed material + 课堂笔记
    assert by_number[2]["mapping_state"] == "unmapped"
    assert by_number[2]["sync_label"] == "已同步 · 资料映射待确认"
    # unmapped courses and lectures stay visibly distinct
    assert course_from(client, COURSE_DEV)["mapping_state"] == "unmapped"
    assert course_from(client, COURSE_COG)["mapping_state"] == "unmapped"


def test_directory_loads_are_stable_across_repeated_reads():
    client, service, _ = make_client()
    first = client.get("/api/directory").json()
    second = client.get("/api/directory").json()
    for key in ("hub", "stats", "courses"):
        assert first[key] == second[key]


# -- VAL-META-016: the three material view groupings ------------------------------


def item_keys() -> set[str]:
    return {
        "id", "url", "title", "type", "status", "source", "course", "is_note",
        "linked_to_lecture", "association_state", "lecture", "updated",
    }


def test_view_lecture_returns_only_the_selected_lectures_materials():
    client, _, _ = make_client()
    body = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "lecture", "lecture_id": NET_L1}
    ).json()
    assert body["view"] == "lecture"
    assert body["course_id"] == COURSE_NET
    assert body["lecture"]["id"] == NET_L1
    assert body["mapped"] is True
    # confirmed items are EXPLICITLY linked and labeled as such
    for item in body["confirmed"]:
        assert set(item) == item_keys()
        assert item["lecture"]["id"] == NET_L1
    confirmed = body["confirmed"]
    assert {i["linked_to_lecture"] for i in confirmed} == {True}
    assert {i["association_state"] for i in confirmed} == {"confirmed"}
    # inferred items are a distinctly flagged 待确认 subset — never 已关联本讲
    assert len(body["inferred"]) == 1
    inferred = body["inferred"][0]
    assert inferred["association_state"] == "待确认"
    assert inferred["linked_to_lecture"] is False
    assert inferred["title"] == "第一讲（2）基础知识.pdf"
    # every item carries stored identity
    for item in confirmed + body["inferred"]:
        assert item["url"] == f"https://www.notion.so/{item['id'].replace('-', '')}"


def test_view_lecture_unmapped_and_inferred_empty_lectures():
    client, _, _ = make_client()
    dev = client.get(
        f"/api/courses/{COURSE_DEV}/materials", params={"view": "lecture", "lecture_id": DEV_L1}
    ).json()
    assert dev["mapped"] is False
    assert dev["confirmed"] == []
    assert len(dev["inferred"]) == 1
    assert dev["inferred"][0]["association_state"] == "待确认"
    # inferred-empty lecture: nothing at all (the approved empty state data)
    cog = client.get(
        f"/api/courses/{COURSE_COG}/materials", params={"view": "lecture", "lecture_id": COG_L1}
    ).json()
    assert cog["mapped"] is False
    assert cog["confirmed"] == []
    assert cog["inferred"] == []


def test_view_lecture_requires_a_lecture_id():
    client, _, _ = make_client()
    response = client.get(f"/api/courses/{COURSE_NET}/materials", params={"view": "lecture"})
    assert response.status_code == 400


def test_view_type_groups_linked_and_course_level_with_counts():
    client, _, _ = make_client()
    body = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "type"}
    ).json()
    assert body["view"] == "type"
    total = 0
    labels = []
    for group in body["groups"]:
        assert set(group) == {"type", "count", "items"}
        assert group["count"] == len(group["items"])
        total += group["count"]
        labels.append(group["type"])
        for item in group["items"]:
            assert set(item) == item_keys()
            assert item["url"].startswith("https://www.notion.so/")
    assert total == 7  # the full 计算机网络 pool (linked + course-level)
    assert "课堂课件" in labels and "课堂笔记" in labels and "课程手册" in labels
    # per-item association labels are preserved inside groups
    courseware = [i for i in body["groups"] if i["type"] == "课堂课件"][0]
    linked = [i for i in courseware["items"] if i["linked_to_lecture"]]
    inferred = [i for i in courseware["items"] if i["association_state"] == "待确认"]
    assert len(linked) == 1 and len(inferred) == 1


def test_view_type_empty_type_value_groups_under_label():
    ws = build_panel_workspace()
    import pku_sync.panel.fake_workspace as fw

    # append a row with an EMPTY 资料类型 — the verified "未标注" display case
    ws.rows[fw.DB_INDEX].append(
        material_row(synth(999), title="未分类资料.pdf", course="计算机网络", type_="", status="已索引")
    )
    client, _, _ = make_client(workspace=ws)
    body = client.get(f"/api/courses/{COURSE_NET}/materials", params={"view": "type"}).json()
    labels = [g["type"] for g in body["groups"]]
    assert "未标注" in labels
    unnamed = [g for g in body["groups"] if g["type"] == "未标注"][0]
    assert unnamed["count"] == 1
    assert unnamed["items"][0]["title"] == "未分类资料.pdf"


def test_view_all_is_the_flat_merged_list_with_association_labels():
    client, _, _ = make_client()
    body = client.get(f"/api/courses/{COURSE_NET}/materials", params={"view": "all"}).json()
    assert body["view"] == "all"
    assert len(body["items"]) == 7  # linked (2) + course-level (5) — one flat list
    # linked items come first, then course-level; labels carried per row
    states = [i["association_state"] for i in body["items"]]
    ordered = [i for i in body["items"] if i["linked_to_lecture"]]
    assert len(ordered) == 2
    assert any(i["id"] == NET_MISSING for i in body["items"])  # 待确认 note entry present
    pending_note = next(i for i in body["items"] if i["id"] == NET_MISSING)
    assert pending_note["is_note"] is True
    assert pending_note["association_state"] == "待确认"
    assert pending_note["linked_to_lecture"] is False
    for item in body["items"]:
        assert set(item) == item_keys()
        assert item["url"] == f"https://www.notion.so/{item['id'].replace('-', '')}"
    assert set(states) <= {"confirmed", "待确认", "none"}


def test_material_view_material_payloads_exclude_forbidden_strings():
    client, _, _ = make_client()
    dumps = []
    for view, params in (
        ("lecture", {"lecture_id": NET_L1}),
        ("type", {}),
        ("all", {}),
    ):
        response = client.get(
            f"/api/courses/{COURSE_NET}/materials", params={"view": view, **params}
        )
        assert response.status_code == 200
        dumps.append(json.dumps(response.json(), ensure_ascii=False))
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert all(needle not in dump for dump in dumps)


def test_material_view_unknown_course_unknown_lecture_unknown_view():
    client, _, _ = make_client()
    unknown = synth(777)
    assert client.get(f"/api/courses/{unknown}/materials", params={"view": "all"}).status_code == 404
    response = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "lecture", "lecture_id": unknown}
    )
    assert response.status_code == 404
    assert client.get(f"/api/courses/{COURSE_NET}/materials", params={"view": "wat"}).status_code == 400


# -- VAL-META-017: identity-only launch -------------------------------------------


def test_launch_known_target_opens_the_exact_stored_identity():
    client, service, _ = make_client()
    client.get("/api/directory")
    call_count = len(service.provider.client.calls)
    response = client.post("/api/launch", json={"target_id": NET_L2, "target_type": "lecture"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "opened"
    assert body["url"] == f"https://www.notion.so/{NET_L2.replace('-', '')}"
    assert body["fallback"] is None
    # zero client calls during launch — the resolution is pure stored identity
    assert service.provider.client.calls[call_count:] == []


def test_launch_missing_identity_returns_4xx_missing_mapping_with_course_fallback():
    client, _, _ = make_client()
    client.get("/api/directory")
    response = client.post(
        "/api/launch", json={"target_id": synth(777), "target_type": "lecture", "course_id": COURSE_NET}
    )
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "missing_mapping"
    assert body["url"] is None  # never a fabricated URL
    assert body["fallback"] == {
        "id": COURSE_NET,
        "url": f"https://www.notion.so/{COURSE_NET.replace('-', '')}",
        "title": "计算机网络",
    }
    assert "Notion" in body["detail"] or "页面" in body["detail"]  # generic copy


def test_launch_missing_identity_without_course_context_has_no_fallback():
    client, _, _ = make_client()
    client.get("/api/directory")
    response = client.post("/api/launch", json={"target_id": synth(777)})
    assert response.status_code == 404
    assert response.json()["fallback"] is None


def test_launch_never_searches_even_for_missing_or_garbage_targets():
    client, service, _ = make_client()
    client.get("/api/directory")
    search_count = sum(1 for call in service.provider.client.calls if call[0] == "search")
    call_count = len(service.provider.client.calls)
    for raw in (synth(777), "not-a-page-id", "  "):
        response = client.post("/api/launch", json={"target_id": raw, "course_id": COURSE_NET})
        assert response.status_code == 404
    # zero new client calls of ANY kind across every launch path
    assert service.provider.client.calls[call_count:] == []
    assert sum(1 for call in service.provider.client.calls if call[0] == "search") == search_count


def test_launch_request_and_response_contain_no_forbidden_strings():
    client, _, _ = make_client()
    client.get("/api/directory")
    opened = client.post("/api/launch", json={"target_id": NET_ROWS[0]}).json()
    missing = client.post(
        "/api/launch", json={"target_id": synth(777), "course_id": COURSE_NET}
    ).json()
    dumped = json.dumps({"opened": opened, "missing": missing}, ensure_ascii=False)
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert needle not in dumped


def test_launch_before_any_directory_load_is_explicit_missing_mapping():
    client, service, _ = make_client()
    response = client.post("/api/launch", json={"target_id": NET_L1})
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "missing_mapping"
    assert body["url"] is None
    assert service.provider.client.calls == []  # no load, no search, no guess


# -- VAL-META-018: the honest sync surface ---------------------------------------


def test_sync_states_are_exactly_the_four_approved_ones():
    assert SYNC_STATES == (STATE_SYNCING, STATE_ERROR, STATE_EMPTY, STATE_DONE)


def test_sync_state_reports_done_after_a_successful_load():
    client, _, _ = make_client()
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_SYNCING  # nothing loaded yet — honest, not fake-done
    client.get("/api/directory")
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_DONE
    assert sync["error_reason"] is None
    assert isinstance(sync["last_sync_at"], str)
    datetime.datetime.fromisoformat(sync["last_sync_at"])  # ISO 8601, parseable


def test_sync_state_reports_empty_when_there_are_no_courses():
    import pku_sync.panel.fake_workspace as fw

    ws = FakeWorkspace()
    ws.search_results = [search_page(HUB, "Class Notes 2026 下半学期")]
    ws.children[HUB] = [child_database_block(fw.DB_INDEX, "课程资料索引")]
    ws.databases[fw.DB_INDEX] = material_schema()
    client, _, _ = make_client(workspace=ws)
    body = client.get("/api/directory").json()
    assert body["courses"] == []  # an honest empty, NOT a leaked error or fake data
    assert body["hub"] is not None
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_EMPTY
    assert sync["last_sync_at"] is not None


def test_failed_sync_reports_error_and_never_done():
    client, _, _ = make_client(variant="fault")
    response = client.get("/api/directory")
    assert response.status_code == 503
    assert response.json()["detail"] == SYNC_ERROR_HUB_AMBIGUOUS
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_ERROR
    assert sync["state"] != STATE_DONE  # a failed sync never reports done
    assert sync["error_reason"] == SYNC_ERROR_HUB_AMBIGUOUS
    assert sync["last_sync_at"] is None


def test_failed_sync_after_a_success_keeps_the_timestamp_but_stays_error():
    class FlakyProvider:
        def __init__(self, inner):
            self._inner = inner
            self._loads = 0

        def load(self, *args, **kwargs):
            self._loads += 1
            if self._loads > 1:
                raise RuntimeError("boom")
            return self._inner.load()

        def resolve_launch(self, data, target_id, *, course_id=None):
            return self._inner.resolve_launch(data, target_id, course_id=course_id)

    from pku_sync.panel.fake_directory import PanelDirectoryProvider

    inner = PanelDirectoryProvider(build_panel_workspace())
    service = DirectoryService(FlakyProvider(inner), clock=lambda: FIXED_NOW)
    app = create_app(directory_service=service)
    client = TestClient(app)
    assert client.get("/api/directory").status_code == 200
    first_ts = client.get("/api/sync/state").json()["last_sync_at"]
    # second read fails: error, established timestamp preserved, never done
    response = client.get("/api/directory")
    assert response.status_code == 503
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_ERROR
    assert sync["error_reason"] == SYNC_ERROR_GENERIC
    assert sync["last_sync_at"] == first_ts
    assert sync["state"] != STATE_DONE


def test_sync_error_reasons_are_user_safe():
    client, _, _ = make_client(variant="fault")
    client.get("/api/directory")
    sync = client.get("/api/sync/state").json()
    dumped = json.dumps(sync, ensure_ascii=False)
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert needle not in dumped
    assert "Traceback" not in dumped


def test_user_safe_reason_mapping_covers_every_failure_family():
    """Every adapter/client failure maps to a pinned user-safe reason."""
    from pku_sync.notion import NotionError
    from pku_sync.notion_meta.errors import (
        HubAmbiguityError,
        HubNotFoundError,
        MaterialDatabaseNotFoundError,
        NotionRetryableError,
        SchemaError,
    )

    cases = [
        (HubAmbiguityError("Class Notes 2026 下半学期", ["hub 1", "hub 2"]), SYNC_ERROR_HUB_AMBIGUOUS),
        (HubNotFoundError("2026 下半学期"), SYNC_ERROR_HUB_NOT_FOUND),
        (SchemaError("课程资料索引 schema 不符", database="d", property_name="资料"), SYNC_ERROR_SCHEMA),
        (MaterialDatabaseNotFoundError(["其他数据库"]), SYNC_ERROR_NO_INDEX_DB),
        (NotionRetryableError(status=429), SYNC_ERROR_UNAVAILABLE),
        (NotionError("secret_honeypot_token wrapped in the error body", status=503, code="x", body="secret"), SYNC_ERROR_READ),
        (ValueError("any unexpected internal text"), SYNC_ERROR_GENERIC),
    ]
    for exc, expected in cases:
        reason = user_safe_reason(exc)
        assert reason == expected
        assert "secret" not in reason  # the API body never leaks into the reason
    # even a mapping that itself explodes still yields the generic copy
    assert user_safe_reason(RuntimeError("x")) == SYNC_ERROR_GENERIC


def test_sync_state_shows_syncing_while_a_slow_load_is_running():
    service = build_fake_directory(variant="slow", slow_delay=0.3, clock=lambda: FIXED_NOW)
    app = create_app(directory_service=service)
    loader = TestClient(app)
    poller = TestClient(app)
    started = threading.Event()
    outcome = {}

    def do_load():
        started.set()
        outcome["response"] = loader.get("/api/directory")

    thread = threading.Thread(target=do_load)
    thread.start()
    started.wait(timeout=5)
    observed = None
    for _ in range(60):
        state = poller.get("/api/sync/state").json()
        if state["state"] == STATE_SYNCING:
            observed = state
            break
        time.sleep(0.05)
    thread.join(timeout=30)
    assert observed is not None and observed["state"] == STATE_SYNCING
    assert outcome["response"].status_code == 200
    assert poller.get("/api/sync/state").json()["state"] == STATE_DONE


def test_friendly_sync_time_is_deterministic():
    assert friendly_sync_time("2026-09-19T09:12:00+08:00", FIXED_NOW) == "今天 09:12"
    assert (
        friendly_sync_time("2026-09-18T20:40:00+08:00", FIXED_NOW) == "昨天 20:40"
    )
    assert (
        friendly_sync_time("2026-09-08T08:30:00+08:00", FIXED_NOW) == "9月8日 08:30"
    )


def test_directory_api_error_carries_status_and_message():
    err = DirectoryApiError(503, "同步遇到问题，请稍后重试。")
    assert err.status == 503
    assert err.message == "同步遇到问题，请稍后重试。"
