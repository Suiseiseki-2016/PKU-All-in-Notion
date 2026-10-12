"""Durable, account/profile-bound Notion intentions for catalog writers.

Recovery performs reads only. An absent, partial, edited or ambiguous effect
never authorizes replay or release. Old receipts without intentions stay fenced.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re

from . import catalog_snapshots as catalog


def _binding(settings, state):
    common = {key: state[key] for key in
        ("course_id", "revision", "snapshot_sha256", "permit_id")}
    if state["schema"] == 1:
        # A legacy service has no durable server nonce. Still record effects
        # before requests; its interrupted permits keep the existing manual
        # recovery fence, rather than pretending it supports the new protocol.
        from .projection_receipts import root_key
        return {**common, "local_receipt_id": state["local_receipt_id"],
                "root": root_key(settings), "session": catalog.fingerprint(
                    {"session": getattr(settings, "platform_token", "")})}
    return {**common, **{key: state[key] for key in ("client_request_id", "owner", "root")}}


def intent_path(settings, state):
    if state["schema"] == 1:
        binding = _binding(settings, state)
        return (Path(settings.data_dir) / ".notion-projection-intents" /
                binding["session"] / (binding["local_receipt_id"] + ".json"))
    return (Path(settings.data_dir) / ".notion-projection-intents" /
            state["owner"] / (state["client_request_id"] + ".json"))


def initialize(group):
    for settings, _, state in group.members:
        path = intent_path(settings, state)
        if path.exists():
            raise catalog.CatalogUnavailable("Notion 写入意图已经存在，不能覆盖。")
        catalog._save_projection_journal(path, {
            "schema": 1, "binding": _binding(settings, state), "sequence": 0, "pending": None})


def assert_complete(group):
    for settings, _, state in group.members:
        _, value = _load(settings, state)
        if value["pending"] is not None:
            raise catalog.CatalogUnavailable("仍有未核验的 Notion 写入意图，不能声明目录写入完成。")


def _load(settings, state):
    path = intent_path(settings, state)
    try:
        value = json.loads(path.read_text("utf-8"))
        if (not isinstance(value, dict) or set(value) !=
                {"schema", "binding", "sequence", "pending"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or not isinstance(value["binding"], dict)
                or catalog.fingerprint(value["binding"]) != catalog.fingerprint(_binding(settings, state))
                or type(value["sequence"]) is not int or value["sequence"] < 0
                or (value["pending"] is not None and not isinstance(value["pending"], dict))):
            raise ValueError("invalid bound Notion intention")
        return path, value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise catalog.CatalogUnavailable("Notion 写入意图缺失或身份不符，结果保持未确认。") from exc


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value.replace("-", "")):
        raise catalog.CatalogUnavailable("Notion 写入目标身份不完整。")
    return value.replace("-", "").lower()


def _subset(expected, actual):
    """Compare requested fields; allow server metadata, never extra text rows."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key])
            for key, value in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
            and all(_subset(a, b) for a, b in zip(expected, actual)))
    return type(expected) is type(actual) and expected == actual


def _shape(row):
    from .panel.notion_home import _document_signature
    kind = row.get("type", "")
    return {"id": _id(row["id"]), "signature": _document_signature(row),
            "child_title": (row.get(kind) or {}).get("title") if kind == "child_page" else None}


def _rows_match(expected, actual):
    return len(expected) == len(actual) and all(_subset(a, b) for a, b in zip(expected, actual))


def _children(client, target):
    rows = client.list_children(target)
    # Notion retrieves nested table rows through a separate children request;
    # a top-level successful response alone cannot verify their contents.
    for row in rows:
        if row.get("type") == "table" and row.get("has_children"):
            row.setdefault("table", {})["children"] = _children(client, row["id"])
    return rows


def _prepare(client, method, url, body):
    body = deepcopy(body or {})
    if method == "POST" and url == "/v1/pages" and set(body) <= {"parent", "properties", "children"}:
        parent = body.get("parent") or {}
        if set(parent) != {"page_id"}:
            raise catalog.CatalogUnavailable("共享课程写入仅允许已核验的父页。")
        target = _id(parent["page_id"])
        return {"operation": "create", "target": target, "body": body,
                "before": [_id(row["id"]) for row in client.list_child_pages(target)]}
    match = re.fullmatch(r"/v1/blocks/([0-9a-fA-F-]+)/children", url)
    if method == "PATCH" and match and set(body) <= {"children", "after", "position"}:
        target = _id(match[1])
        if not body.get("children"):
            raise catalog.CatalogUnavailable("空 Notion 写入不能建立效果凭证。")
        return {"operation": "append", "target": target, "body": body,
                "before": [_shape(row) for row in _children(client, target)]}
    match = re.fullmatch(r"/v1/(pages|blocks)/([0-9a-fA-F-]+)", url)
    if match and method in {"PATCH", "DELETE"}:
        target = _id(match[2])
        page = match[1] == "pages"
        allowed = {"properties", "archived"} if page else {"paragraph"}
        if method == "DELETE":
            if page or body:
                raise catalog.CatalogUnavailable("未支持的 Notion 删除已阻止。")
            body = {"archived": True}
        if not set(body) <= (allowed | ({"archived"} if not page else set())) or not body:
            raise catalog.CatalogUnavailable("未支持的 Notion 更新已阻止。")
        before = client.get_page(target) if page else client.get_block(target)
        return {"operation": "page" if page else "block", "target": target, "body": body,
                "before": {"id": _id(before["id"]),
                    "parent": before.get("parent"), "shape": None if page else _shape(before)}}
    raise catalog.CatalogUnavailable("该 Notion 变更缺少独立效果校验，未发送请求。")


def effect_matches(client, request):
    if not isinstance(request, dict) or set(request) != {"operation", "target", "body", "before"}:
        return False
    target, body, before = _id(request["target"]), request["body"], request["before"]
    if request["operation"] == "create":
        matches = []
        for row in client.list_child_pages(target):
            if _id(row["id"]) in before:
                continue
            page = client.get_page(row["id"])
            if _subset(body.get("properties"), page.get("properties")):
                # A matching title with a partial/wrong body is ambiguous too.
                matches.append(page)
        if len(matches) != 1:
            return False
        page = matches[0]
        rows = _children(client, page["id"])
        wanted = body.get("children") or []
        return (not page.get("archived") and not page.get("in_trash")
                and _id(page.get("parent", {}).get("page_id")) == target
                and _rows_match(wanted, rows[:len(wanted)]))
    if request["operation"] == "append":
        rows = _children(client, target)
        previous_ids = {row["id"] for row in before}
        preserved = [_shape(row) for row in rows if _id(row["id"]) in previous_ids]
        inserted = [row for row in rows if _id(row["id"]) not in previous_ids]
        if preserved != before or not _rows_match(body["children"], inserted):
            return False
        after = body.get("after")
        if body.get("position"):
            position = body["position"]
            if position.get("type") != "after_block":
                return False
            after = position.get("after_block", {}).get("id")
        expected_ids = [row["id"] for row in before]
        offset = expected_ids.index(_id(after)) + 1 if after else len(expected_ids)
        expected_ids[offset:offset] = [_id(row["id"]) for row in inserted]
        return [_id(row["id"]) for row in rows] == expected_ids
    if request["operation"] in {"page", "block"}:
        page = request["operation"] == "page"
        row = client.get_page(target) if page else client.get_block(target)
        if (_id(row["id"]) != target or row.get("parent") != before["parent"]
                or not _subset(body, row)):
            return False
        if body.get("archived") is True and not page and _shape(row) != before["shape"]:
            return False
        return True
    return False


@contextmanager
def tracked_notion_mutation(client, method, url, body, *, files=None):
    from .projection_receipts import _cohort
    group = _cohort.get()
    readonly = method == "GET" or (method == "POST" and
        (url == "/v1/search" or re.fullmatch(r"/v1/databases/[0-9a-f-]+/query", url)))
    if group is None or not group.tracked_mutations or readonly:
        yield False
        return
    if not group.started or group.completed or files:
        raise catalog.CatalogUnavailable("目录阶段或上传无法建立 Notion 写入凭证。")
    loaded = [_load(settings, state) for settings, _, state in group.members]
    if not loaded or any(value["pending"] is not None for _, value in loaded):
        raise catalog.CatalogUnavailable("上次 Notion 写入结果尚未确认，禁止重复发送。")
    request = _prepare(client, method, url, body)
    for path, value in loaded:
        value["pending"] = request
        catalog._save_projection_journal(path, value)
    yield True  # A lost response keeps the durable intention, never a retry.
    if not effect_matches(client, request):
        raise catalog.CatalogUnavailable("Notion 写入后的独立回读未确认完整效果，许可保持保护。")
    for path, value in loaded:
        value["sequence"] += 1
        value["pending"] = None
        catalog._save_projection_journal(path, value)


def recover(settings, state):
    path = intent_path(settings, state)
    if not path.exists():
        return False  # In particular, never synthesize intentions for old schema-2 writers.
    _, value = _load(settings, state)
    if value["pending"] is None:
        return True
    from .notion import NotionClient, NotionError
    import httpx
    try:
        with NotionClient(settings.notion_token) as client:
            if not effect_matches(client, value["pending"]):
                return False
    except (NotionError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise catalog.CatalogUnavailable("Notion 写入结果暂时无法独立回读，许可保持保护。") from exc
    value["sequence"] += 1
    value["pending"] = None
    catalog._save_projection_journal(path, value)
    return True
