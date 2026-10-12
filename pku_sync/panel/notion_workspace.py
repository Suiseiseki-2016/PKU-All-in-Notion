"""Additive version-two Notion projection; never adopts the legacy home."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import threading

from ..notion import NotionClient
from ..projection_receipts import verified_workspace_operation, verified_workspace_step
from ..platform import request_scope
from . import notion_home as legacy
from .campus_catalog import campus_course, campus_courses

TITLE = "PKU All in Notion · 新版学习主页"
CENTERS = ("每节课", "作业中心", "通知中心", "小测中心", "课程资料")
SUMMARY_LINK = "查看已有总结（请核对内容）"
_MANIFEST_COMMIT_LOCK = threading.RLock()
_ACTIVE_READ_PLANS = {}


def _summary_page(client, url):
    from urllib.parse import urlsplit
    import re
    from uuid import UUID
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"notion.so", "www.notion.so", "app.notion.com"}:
        raise ValueError("已有总结不是已授权的 Notion 页面链接。")
    match = re.search(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", parts.path, re.I)
    if match is None:
        raise ValueError("已有总结链接缺少明确的 Notion 页面 ID。")
    page = client.get_page(str(UUID(match.group(1))))
    if page.get("archived") or page.get("in_trash"):
        raise ValueError("已有总结页面已不可读取，未迁移链接。")
    return page


@verified_workspace_operation('summary-link')
def _link_summary(client, lecture_id, url):
    _summary_page(client, url)
    block = legacy._linked_paragraph(SUMMARY_LINK, url)
    signature = legacy._source_block_signature(block)
    if not any(legacy._source_block_signature(row) == signature for row in client.list_children(lecture_id)):
        client.append_blocks(lecture_id, [block], retry=False)
    if not any(legacy._source_block_signature(row) == signature for row in client.list_children(lecture_id)):
        raise ValueError("已有总结链接写入后验证失败。")


def manifest_path(root: Path) -> Path:
    return root / "notion-workspace-v2.json"


def saved_workspace(root: Path) -> dict:
    try:
        value = json.loads(manifest_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != 2 or not value.get("id"):
        return {}
    try:
        namespace = (root / ".account-catalog" / "active-account").read_text("ascii")
    except OSError:
        namespace = None
    if namespace and value.get("account_namespace") != namespace:
        return {}
    return value


def _save(path: Path, value: dict) -> None:
    if path.name == 'notion-workspace-v2.json':
        with _MANIFEST_COMMIT_LOCK:
            _save_document(path, value)
        return
    _save_document(path, value)


def _save_document(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def _verify_home(page: dict, home_id: str, parent_id: str) -> None:
    """Reject a changed home before reading descendants or obtaining a writer."""
    from uuid import UUID
    message = "新版主页身份或位置已变化，请核对 Notion 授权、归档状态和原父页面后重试；已有记录已保留。"
    try:
        parent = page.get('parent') or {}
        valid = (not page.get('archived') and not page.get('in_trash')
                 and UUID(page['id']) == UUID(home_id)
                 and parent.get('type', 'page_id') == 'page_id'
                 and UUID(parent['page_id']) == UUID(parent_id))
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(message)


@verified_workspace_operation('center')
def _verified_child(client, parent: str, title: str) -> dict:
    page = legacy._child(client, parent, title)
    remote = client.get_page(page["id"])
    children = client.list_child_pages(parent)
    if remote.get("archived") or not any(
        row.get("id") == page["id"] and row.get("title") == title for row in children
    ):
        raise ValueError("新版 Notion 页面写入后验证失败。")
    return page


@verified_workspace_operation('center')
def _verified_centers(client, parent: str, *, observed_children=None, extra_pages_out=None) -> dict:
    """Verify this entire fixed directory with fresh initial and final reads."""
    # A course's immediately preceding full identity read already contains
    # the direct child_page blocks. Consume that one-operation observation;
    # Serial verification still rereads the directory below. A read-only view
    # is accepted only after a second independent complete graph verifies it.
    observed = ([{'id': block['id'], 'title': block['child_page'].get('title', '')}
                 for block in observed_children if block.get('type') == 'child_page']
                if observed_children is not None and any(
                    block.get('type') == 'child_page' for block in observed_children)
                else client.list_child_pages(parent))
    result = {}
    for title in CENTERS:
        matches = [page for page in observed if page.get('title') == title]
        if len(matches) > 1:
            raise ValueError(f'Notion 中有多个同名页面「{title}」，请先手动确认。')
    def read(page):
        from uuid import UUID
        remote = client.get_page(page['id'])
        try:
            same_id = UUID(remote['id']) == UUID(page['id'])
        except (KeyError, ValueError, TypeError, AttributeError):
            same_id = False
        if (not same_id or remote.get('archived') or remote.get('in_trash')
                or not _page_parent_matches(remote, parent)):
            raise ValueError('新版 Notion 中心身份或位置已变化，未确认目录完成。')
        return {'id': page['id'], 'url': remote.get('url', '')}
    existing = [(title, next(page for page in observed if page.get('title') == title))
                for title in CENTERS if any(page.get('title') == title for page in observed)]
    if existing:
        # Complete all independent existing-page reads before any creation.
        # No mutation or durable writer state crosses the worker boundary.
        with ThreadPoolExecutor(max_workers=min(4, len(existing))) as pool:
            result.update(zip((title for title, _ in existing), pool.map(read, (page for _, page in existing))))
    for title in CENTERS:
        if title not in result:
            page = client.create_page(parent, title, retry=False)
            result[title] = read(page)
    final = client.list_child_pages(parent)
    for title, page in result.items():
        matches = [row for row in final if row.get('title') == title]
        if len(matches) != 1 or matches[0]['id'] != page['id']:
            raise ValueError('新版 Notion 中心未唯一确认，未确认目录完成。')
    _, extra_pages = _classify_course_children(final, parent)
    if extra_pages_out is not None:
        extra_pages_out.update(extra_pages)
    return result


def _classify_course_children(pages: list[dict], parent: str) -> tuple[dict, dict]:
    """Keep the fixed centers distinct from every other actual direct subpage."""
    from uuid import UUID
    seen = set()
    for page in pages:
        try:
            identity = UUID(page['id'])
            title = page['title']
            if not isinstance(title, str) or identity in seen:
                raise ValueError
            seen.add(identity)
            observed_parent = page.get('parent')
            if observed_parent is not None:
                valid_parent = (UUID(observed_parent) == UUID(parent)
                                if isinstance(observed_parent, str)
                                else _page_parent_matches(page, parent))
                if not valid_parent:
                    raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError('新版课程直属子页身份或位置未唯一确认；原页面与记录已保留。') from None
    centers = {}
    for title in CENTERS:
        matches = [page for page in pages if page['title'] == title]
        if len(matches) != 1:
            raise ValueError(f'新版「{title}」目录未唯一确认；原页面与记录已保留。')
        centers[title] = deepcopy(matches[0])
    extras = {page['id']: deepcopy(page) for page in pages if page['title'] not in CENTERS}
    return centers, extras


def _page_parent_matches(page, expected):
    from uuid import UUID
    try:
        parent = page.get('parent') or {}
        return (parent.get('type', 'page_id') == 'page_id'
                and UUID(parent['page_id']) == UUID(expected))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _verify_selected_parent(client, parent_id):
    from uuid import UUID
    page = client.get_page(parent_id)
    try:
        valid = (UUID(page['id']) == UUID(parent_id)
                 and not page.get('archived') and not page.get('in_trash'))
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('已选 Notion 父页面已不可读取或已归档；已有记录已保留，未写入目录。')
    return page


def _course_identity_snapshot(client, parent):
    pages = client.list_child_pages(parent)
    if not pages:
        return []
    # These are independent complete reads. No mutation or writer state is
    # passed to the workers, and every response must finish successfully.
    with ThreadPoolExecutor(max_workers=min(4, len(pages))) as pool:
        return list(pool.map(lambda page: (page, client.list_children(page['id'])), pages))


def _existing_course_page(client, parent: str, course: dict, *, observed=None) -> dict | None:
    source = "https://course.pku.edu.cn/#pku-course:" + course["id"]
    matches = []
    if observed is None:
        observed = [(page, client.list_children(page["id"])) for page in client.list_child_pages(parent)]
    for page, blocks in observed:
        if any((part.get("text", {}).get("link") or {}).get("url") == source
               for block in blocks if block.get("type") == "paragraph"
               for part in block.get("paragraph", {}).get("rich_text", [])):
            matches.append(page)
    if len(matches) > 1:
        raise ValueError("新版目录存在重复教学网课程身份，请先核对。")
    if matches:
        return {"id": matches[0]["id"], "url": matches[0].get("url", "")}
    if any(page.get("title") == course["title"] for page, _ in observed):
        raise ValueError("新版目录中有同名课程但缺少稳定身份，请核对后再同步。")
    return None


@verified_workspace_operation('course')
def _course_page(client, parent: str, course: dict, *, create: bool = True,
                 observed=None, verify_selected: bool = False, children_out=None) -> dict:
    existing = _existing_course_page(client, parent, course, observed=observed)
    if existing:
        if verify_selected:
            remote = client.get_page(existing['id'])
            blocks = client.list_children(existing['id'])
            current = _existing_course_page(client, parent, course,
                observed=[(remote, blocks)])
            if (current is None or current['id'] != existing['id']
                    or remote.get('archived') or remote.get('in_trash')
                    or not _page_parent_matches(remote, parent)):
                raise ValueError('新版课程身份或位置在同步期间发生变化，未写入课程。')
            if children_out is not None:
                children_out.update({'id': existing['id'], 'children': deepcopy(blocks)})
            existing = {**existing, 'url': remote.get('url', existing.get('url', ''))}
        return existing
    if not create:
        raise ValueError("新版课程目录尚未验证，请先同步新版目录。")
    # A newly created course carries its stable identity in the same request.
    page = client.create_page(parent, course["title"], children=[
        legacy._linked_paragraph("查看教学网课程", "https://course.pku.edu.cn/#pku-course:" + course["id"])
    ], retry=False)
    remote = client.get_page(page["id"])
    blocks = client.list_children(page["id"])
    if remote.get("archived") or not any(
        (part.get("text", {}).get("link") or {}).get("url") == "https://course.pku.edu.cn/#pku-course:" + course["id"]
        for block in blocks if block.get("type") == "paragraph"
        for part in block.get("paragraph", {}).get("rich_text", [])
    ):
        raise ValueError("新版课程身份写入后验证失败。")
    if children_out is not None:
        children_out.update({'id': page['id'], 'children': deepcopy(blocks)})
    return {"id": page["id"], "url": remote.get("url", "")}


class _ReadPlanChanged(Exception):
    """A normal serial write is required; no write was attempted upstream."""


class _ReadScopeStopped(Exception):
    pass


def _same_read_graph(first, final):
    """Compare complete semantics; retain the unmodified physical responses."""
    from uuid import UUID
    if first.keys() != final.keys():
        return False
    for key, before in first.items():
        after = final[key]
        if (key[0] == 'page' and isinstance(before, dict) and isinstance(after, dict)
                and before.get('object') == after.get('object') == 'page'):
            try:
                # Actual Notion GET page responses assign a new transport UUID
                # to each request. Only that exact top-level field is transport
                # metadata; nested request_id, URLs, permissions, edit times,
                # student content and every other field remain strict.
                UUID(before.get('request_id'))
                UUID(after.get('request_id'))
            except (ValueError, TypeError, AttributeError):
                pass  # Missing/malformed transport fields retain exact checks.
            else:
                before = {name: value for name, value in before.items() if name != 'request_id'}
                after = {name: value for name, value in after.items() if name != 'request_id'}
        if before != after:
            return False
    return True


class _WorkspaceReadView:
    """One complete read graph, with writes prohibited and no persisted cache."""
    def __init__(self, client, semaphore, stopped, *, snapshot=None):
        self.client, self.semaphore, self.stopped = client, semaphore, stopped
        self.snapshot = deepcopy(snapshot) if snapshot is not None else {}
        self.sealed = snapshot is not None
        self.lock = threading.Lock()
        self.keys = {}

    def _read(self, kind, identifier, read):
        key = (kind, identifier)
        with self.lock:
            gate = self.keys.setdefault(key, threading.Lock())
        with gate:
            if self.stopped.is_set():
                raise _ReadScopeStopped()
            if key not in self.snapshot:
                if self.sealed:
                    raise ValueError('Notion 完整读取计划缺少验收数据；未确认完成。')
                with self.semaphore:
                    if self.stopped.is_set():
                        raise _ReadScopeStopped()
                    self.snapshot[key] = deepcopy(read(identifier))
            return deepcopy(self.snapshot[key])

    def get_page(self, identifier):
        return self._read('page', identifier, self.client.get_page)

    def list_children(self, identifier):
        return self._read('children', identifier, self.client.list_children)

    def list_child_pages(self, identifier):
        return [{'id': row['id'], 'title': row['child_page'].get('title', '')}
                for row in self.list_children(identifier) if row.get('type') == 'child_page']

    def __getattr__(self, name):
        # A new/changed source, a missing summary link, an unknown recovery
        # state, or any other writer discards the entire read plan. Never run
        # its mutation against an earlier observation.
        def requires_write(*args, **kwargs):
            raise _ReadPlanChanged()
        return requires_write


def _history_candidates(old, detail):
    current = {lesson['source_id'] for lesson in detail['lessons']}
    active, historical = old.get('lectures', {}), old.get('historical_lectures', {})
    if (set(active) & set(historical)
            or {item.get('source_id') for item in active.values()} & {item.get('source_id') for item in historical.values()}
            or {item.get('id') for item in active.values()} & {item.get('id') for item in historical.values()}):
        raise ValueError('当前讲次与历史映射身份重叠；未合并原记录。')
    result = {}
    for key, item in {**active, **historical}.items():
        if item.get('source_id') not in current:
            if not item.get('source_id') or not item.get('id'):
                raise ValueError('历史讲次原身份不完整；已有记录保留。')
            result[key] = deepcopy(item)
        elif key in old.get('historical_lectures', {}):
            raise ValueError('历史来源重新进入当前目录，需明确核对；未混合来源。')
    if (len({item['source_id'] for item in result.values()}) != len(result)
            or len({item['id'] for item in result.values()}) != len(result)):
        raise ValueError('历史讲次来源重复；已有记录保留。')
    return result


def _bind_history(settings, course_id, detail, old, value=None):
    """Only the calling operation obtains authenticated history provenance."""
    targets = _history_candidates(old, detail)
    if not targets:
        return detail, None
    from .. import recording_history, catalog_snapshots as catalog
    envelope = catalog.cached(Path(settings.data_dir), course_id)
    expected = {item['source_id'] for item in targets.values()}
    if value is None:
        value = recording_history.verified_history(settings, course_id, envelope,
                                                   expected_previous_sources=expected)
        recording_history.write_cache(settings, value)
    recording_history.assert_fresh(settings, course_id, envelope, value)
    rows = value['recordings']
    if len(rows) != len(expected) or {item['source_id'] for item in rows} != expected:
        raise ValueError('历史讲次证据未与原映射精确对应；未知历史拒绝投影。')
    evidence = {item['source_id']: item for item in rows}
    result = deepcopy(detail)
    result['_historical_targets'] = {key: {'mapping': item, 'evidence': deepcopy(evidence[item['source_id']])}
                                     for key, item in targets.items()}
    result['_historical_course_id'] = old.get('id')
    result['_historical_parent_id'] = old.get('centers', {}).get('每节课', {}).get('id')
    result['_history_binding'] = {key: value[key] for key in (
        'course_id', 'account_namespace', 'account_owner_sha256', 'current_revision', 'current_snapshot_sha256', 'raw_history_sha256')}
    return result, value


def _assert_history_scope(settings, histories, notion_token):
    if not histories:
        return
    from .. import recording_history, catalog_snapshots as catalog
    if getattr(settings, 'notion_token', '') != notion_token:
        raise ValueError('历史投影 Notion 授权发生变化；已有记录保留。')
    for course_id, value in histories.items():
        recording_history.assert_fresh(settings, course_id,
            catalog.cached(Path(settings.data_dir), course_id), value)


def _historical_projection(client, course, parent_id, detail):
    """Read every original historical page/block; never mutate or adopt one."""
    targets = detail.get('_historical_targets', {})
    if not targets:
        return {}
    from uuid import UUID
    if (course['id'] != detail['_historical_course_id'] or parent_id != detail['_historical_parent_id']):
        raise ValueError('历史讲次课程或父目录变化；原页面保留。')
    children = client.list_child_pages(parent_id)
    bodies = {page['id']: client.list_children(page['id']) for page in children}
    result = {}
    def page_tree(identifier, parent, pages, blocks, seen):
        if identifier in seen:
            raise ValueError('历史讲次子树身份重复；未确认历史内容。')
        seen.add(identifier)
        page = client.get_page(identifier)
        try:
            valid = UUID(page['id']) == UUID(identifier)
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid or page.get('archived') or page.get('in_trash') or not _page_parent_matches(page, parent):
            raise ValueError('历史讲次页面身份或位置变化；学生内容保留。')
        pages[identifier] = deepcopy(page)
        rows = client.list_children(identifier)
        blocks[identifier] = deepcopy(rows)
        for row in rows:
            if row.get('type') == 'child_page':
                page_tree(row['id'], identifier, pages, blocks, seen)
            elif row.get('has_children'):
                block_tree(row['id'], blocks, seen)
    def block_tree(identifier, blocks, seen):
        if identifier in seen:
            raise ValueError('历史讲次块身份重复；未确认历史内容。')
        seen.add(identifier)
        rows = client.list_children(identifier)
        blocks[identifier] = deepcopy(rows)
        for row in rows:
            if row.get('type') == 'child_page':
                raise ValueError('历史嵌套块中的页面父身份未确认；未确认历史内容。')
            if row.get('has_children'):
                block_tree(row['id'], blocks, seen)
    for key, target in targets.items():
        old, evidence = target['mapping'], target['evidence']
        source = evidence['source']
        if source.get('source_id') != old['source_id'] or evidence['source_id'] != old['source_id']:
            raise ValueError('历史原来源字段不一致；原页面保留。')
        if old.get('history_source') is not None and old['history_source'] != source:
            raise ValueError('历史原来源字段变化；原页面保留。')
        marker = legacy._source_block_signature(legacy._lecture_marker(source))
        matches = [identifier for identifier, rows in bodies.items()
                   if any(legacy._source_block_signature(row) == marker for row in rows)]
        if matches != [old['id']]:
            raise ValueError('历史讲次来源标记或原页面未唯一确认；学生内容保留。')
        pages, blocks = {}, {}
        page_tree(old['id'], parent_id, pages, blocks, set())
        graph = {('page', identifier): page for identifier, page in pages.items()}
        graph.update({('children', identifier): body for identifier, body in blocks.items()})
        prior = old.get('notion_readback')
        if prior is not None:
            previous = {('page', identifier): page for identifier, page in prior['pages'].items()}
            previous.update({('children', identifier): body for identifier, body in prior['blocks'].items()})
            if not _same_read_graph(previous, graph):
                raise ValueError('历史讲次或学生内容变化；原内容保留，未确认历史投影。')
        from ..catalog_schema import fingerprint
        readback = {'pages': pages, 'blocks': blocks}
        result[key] = {**deepcopy(old), 'history_source': deepcopy(source), 'parent_id': parent_id, 'read_only': True,
                       'processing_allowed': False,
                       **{field: evidence[field] for field in ('original_revision', 'original_snapshot_sha256',
                           'accepted_revision', 'review_id', 'review_sha256', 'evidence_sha256')},
                       'notion_readback': readback, 'notion_readback_sha256': fingerprint(readback)}
    return result


def historical_lecture(root, course_id, history_record, history):
    """A local verified history link; it grants no fresh projection authority."""
    from .. import catalog_snapshots as catalog, recording_history
    from ..catalog_schema import fingerprint
    try:
        envelope = catalog.cached(root, course_id)
        cached_history = recording_history.read_cache(root, course_id, envelope)
        if cached_history is None or history != cached_history or history_record not in history['recordings']:
            return None
        workspace = saved_workspace(Path(root))
        if workspace.get('active') is not True:
            return None
        course = workspace['courses'][course_id]
        current = envelope['snapshot']['sections']['recordings']['items']
        _history_candidates(course, {'lessons': current})
        expected = {key: history[key] for key in (
            'course_id', 'account_namespace', 'account_owner_sha256', 'current_revision', 'current_snapshot_sha256', 'raw_history_sha256')}
        if course.get('history_binding') != expected:
            return None
        matches = [item for item in course.get('historical_lectures', {}).values()
                   if item.get('source_id') == history_record['source_id']]
        if len(matches) != 1:
            return None
        item = matches[0]
        if (item.get('read_only') is not True or item.get('processing_allowed') is not False
                or item.get('history_source') != history_record['source']
                or any(item.get(key) != history_record[key] for key in (
                    'original_revision', 'original_snapshot_sha256', 'accepted_revision', 'review_id',
                    'review_sha256', 'evidence_sha256'))):
            return None
        parent = course['centers']['每节课']['id']
        readback = item['notion_readback']
        if item.get('parent_id') != parent or fingerprint(readback) != item.get('notion_readback_sha256'):
            return None
        page = readback['pages'][item['id']]
        from uuid import UUID
        if (UUID(page['id']) != UUID(item['id']) or page.get('archived') or page.get('in_trash')
                or not _page_parent_matches(page, parent)):
            return None
        marker = legacy._source_block_signature(legacy._lecture_marker(history_record['source']))
        if sum(legacy._source_block_signature(block) == marker for block in readback['blocks'][item['id']]) != 1:
            return None
        return deepcopy(item)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _verify_historical_course(client, home_id, row, detail, observed):
    if not detail.get('_historical_targets'):
        return
    known = detail['_historical_course_id']
    actual = _existing_course_page(client, home_id, row, observed=observed)
    if actual is None or actual['id'] != known:
        raise ValueError('历史课程原来源标记或身份变化；禁止创建替代页面。')
    page = client.get_page(known)
    if page.get('archived') or page.get('in_trash') or not _page_parent_matches(page, home_id):
        raise ValueError('历史课程原父页面变化；已有内容保留。')
    parent = detail['_historical_parent_id']
    children = client.list_child_pages(known)
    matches = [item for item in children if item.get('title') == '每节课']
    if len(matches) != 1 or matches[0]['id'] != parent:
        raise ValueError('历史讲次原父目录变化；禁止创建替代目录。')
    page = client.get_page(parent)
    if page.get('archived') or page.get('in_trash') or not _page_parent_matches(page, known):
        raise ValueError('历史讲次原父目录不可读取；已有内容保留。')


def _project_course(client, root, home, row, detail, state, state_path, recovery_state, observed):
    """The original verified operations, shared by probes and serial writers."""
    _verify_historical_course(client, home['id'], row, detail, observed)
    course_children = {}
    course = _course_page(client, home['id'], row, observed=observed,
                          verify_selected=True, children_out=course_children)
    extra_pages = {}
    centers = _verified_centers(client, course['id'], observed_children=(
        course_children['children'] if course_children.get('id') == course['id'] else None),
        extra_pages_out=extra_pages)
    historical = _historical_projection(client, course, centers['每节课']['id'], detail)
    for section, center in (('作业', '作业中心'), ('通知', '通知中心'), ('资料', '课程资料'), ('测验', '小测中心')):
        legacy._sync_source_section(client, root, row['id'], section, centers[center]['id'],
            detail, state, state_path=state_path, recovery_state=recovery_state)
    titles = legacy._lecture_titles(detail)
    children = legacy._indexed_children(client, centers['每节课']['id'])
    lectures, final_observation = {}, {}
    verified = legacy._sync_source_lectures(client, centers['每节课']['id'], detail['lessons'],
                                           titles, children, final_observation=final_observation)
    notes = {identifier: legacy._lecture_note_identity(blocks)
             for identifier, (_, blocks) in final_observation.items()}
    for lesson in detail['lessons']:
        page, _ = verified[lesson['id']]
        if lesson.get('notion_note_url'):
            notes = None
            _link_summary(client, page['id'], lesson['notion_note_url'])
        lectures[lesson['id']] = {'id': page['id'], 'source_id': lesson['source_id'],
            'url': page.get('url', ''), 'note_url': lesson.get('notion_note_url', '')}
    with verified_workspace_step('recording-index'):
        legacy._sync_recording_section(client, centers['每节课']['id'], detail['lessons'],
            titles, children, state, row['id'], observed_notes=notes)
    if historical:
        if {item['id'] for item in historical.values()} & {item['id'] for item in lectures.values()}:
            raise ValueError('当前讲次与历史讲次页面重叠；未确认投影。')
        after = _historical_projection(client, course, centers['每节课']['id'], {
            **detail, '_historical_targets': {key: {**detail['_historical_targets'][key], 'mapping': item}
                                           for key, item in historical.items()}})
        historical = after
    return {**course, 'centers': centers, 'extra_pages': extra_pages,
            'lectures': lectures, 'revision': detail.get('catalog_revision'),
            **({'historical_lectures': historical, 'history_binding': deepcopy(detail['_history_binding'])}
               if historical else {})}


def _workspace_source_state(root, home):
    from ..catalog_schema import fingerprint
    path = root / '.notion-workspace-v2' / fingerprint({'home': home['id']}) / 'source-state.json'
    try:
        state = json.loads(path.read_text('utf8'))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        raise ValueError('新版 Notion 恢复记录不可读取，已停止重复写入。') from exc
    if not isinstance(state, dict):
        raise ValueError('新版 Notion 恢复记录格式无效，已停止重复写入。')
    return path, state


class PendingWorkspaceReadPlan:
    """Own one diagnostic first graph; never prepare permits or perform writes."""
    def __init__(self, settings, *, operation_nonce, check_cancel=None):
        if not isinstance(operation_nonce, str) or not operation_nonce:
            raise ValueError('目录预读缺少本轮操作身份。')
        self.root = Path(settings.data_dir).resolve()
        self.settings = settings
        self.existing = deepcopy(saved_workspace(self.root))
        self.operation_nonce, self.check_cancel = operation_nonce, check_cancel
        self.owner = threading.get_ident()
        self.binding = self._binding(settings)
        self.token = getattr(settings, 'notion_token', '')
        self.enabled = bool(self.existing and self.token)
        self.closed = False
        self.stopped = threading.Event()
        self.semaphore = threading.BoundedSemaphore(4)
        self.pool = None
        self.client = None
        self.initial = None
        self.futures, self.details, self.envelopes, self.histories = {}, {}, {}, {}
        self.state, self.recovery_state, self.state_path = {}, {}, None
        self.preflight_taken = False
        self.first_taken = False
        self.reported_errors = set()
        self.registration = None
        # Only local snapshot/registration is atomic; all remote reads remain
        # outside this lock. A late hydration cannot replace this full ledger.
        with _MANIFEST_COMMIT_LOCK:
            self.existing = deepcopy(saved_workspace(self.root))
            self.binding = self._binding(settings)
            self.token = getattr(settings, 'notion_token', '')
            from ..catalog_schema import fingerprint
            if (self.binding[0] != str(self.root)
                    or fingerprint({'notion': self.token}) != self.binding[4]):
                raise ValueError('目录预读账号或目标身份发生变化；已有记录保留，未确认完成。')
            self.enabled = bool(self.existing and self.token)
            if self.enabled:
                self.registration = object()
                _ACTIVE_READ_PLANS[self.registration] = (str(self.root), self.binding)

    def _binding(self, settings):
        from ..catalog_schema import fingerprint
        from ..notion import API_HOST
        root = Path(settings.data_dir).resolve()
        try:
            namespace = (root / '.account-catalog/active-account').read_text('ascii')
        except FileNotFoundError:
            namespace = None
        return (str(root), fingerprint({'account': getattr(settings, 'platform_token', '')}),
                getattr(settings, 'cloud_transcribe_url', None), namespace,
                fingerprint({'notion': getattr(settings, 'notion_token', '')}), os.environ.get('PKU_NOTION_API_BASE') or API_HOST)

    def _check_owner(self):
        if threading.get_ident() != self.owner or self.closed:
            raise ValueError('目录预读必须由本轮操作管理；已有记录保留。')
        if self.check_cancel:
            self.check_cancel()

    def _check_binding(self, settings, existing):
        self._check_owner()
        if (saved_workspace(self.root) != self.existing or self.binding != self._binding(settings)) or any(
                existing.get(key) != self.existing.get(key) for key in ('id', 'parent_id', 'account_namespace')):
            raise ValueError('目录预读账号或目标身份发生变化；已有记录保留，未确认完成。')

    def _initialize(self):
        if self.stopped.is_set():
            raise _ReadScopeStopped()
        self.client = NotionClient(self.token)
        self.client.__enter__()
        _verify_home(self.client.get_page(self.existing['id']), self.existing['id'], self.existing['parent_id'])
        _verify_selected_parent(self.client, self.existing['parent_id'])
        return _course_identity_snapshot(self.client, self.existing['id'])

    def offer(self, course_id, verified_envelope):
        from .. import catalog_snapshots as catalog
        from ..catalog_schema import fingerprint
        self._check_owner()
        if not self.enabled:
            return
        self._check_binding(self.settings, self.existing)
        if course_id in self.futures:
            raise ValueError('本轮课程预读不能重复提交。')
        catalog.assert_portal_readable(self.root, course_id)
        if (not isinstance(verified_envelope, dict)
                or verified_envelope.get('snapshot', {}).get('course_id') != course_id
                or catalog.cached(self.root, course_id) != verified_envelope):
            raise ValueError('课程预读必须绑定本轮已确认的完整账号快照。')
        if self.pool is None:
            self.state_path, self.state = _workspace_source_state(self.root, self.existing)
            self.recovery_state = deepcopy(legacy._source_state(self.root))
            self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='workspace-prefetch')
            self.initial = self.pool.submit(self._initialize)
        row = next((row for row in campus_courses(self.root)['courses'] if row['id'] == course_id), None)
        if row is None:
            raise ValueError('本轮课程预读目标未唯一确认。')
        detail = deepcopy(campus_course(self.root, course_id))
        detail, history = _bind_history(self.settings, course_id, detail,
                                       self.existing.get('courses', {}).get(course_id, {}))
        if history is not None:
            self.histories[course_id] = history
        self.details[course_id] = detail
        self.envelopes[course_id] = fingerprint(verified_envelope)
        self.futures[course_id] = self.pool.submit(self._probe, deepcopy(row), detail)

    def _probe(self, row, detail):
        observed = self.initial.result()
        if self.stopped.is_set():
            raise _ReadScopeStopped()
        # No caller ContextVars, cohort, provider call or local state mutation.
        view = _WorkspaceReadView(self.client, self.semaphore, self.stopped)
        course = _existing_course_page(self.client, self.existing['id'], row, observed=observed)
        body = next((blocks for page, blocks in observed if course and page['id'] == course['id']), [])
        if not all(any(block.get('type') == 'child_page' and
                       block['child_page'].get('title') == title for block in body) for title in CENTERS):
            return view, False
        try:
            _project_course(view, None, self.existing, row, detail, deepcopy(self.state),
                            self.state_path, deepcopy(self.recovery_state), observed)
        except _ReadPlanChanged:
            return view, False
        return view, True

    def _result(self, future):
        while not future.done():
            self._check_owner()
            wait([future], timeout=.05)
        self._check_owner()
        try:
            return future.result()
        except BaseException as exc:
            self.reported_errors.add(id(exc))
            raise

    def wait_first(self):
        self._check_owner()
        if self.initial is not None:
            self._result(self.initial)
        return {key: self._result(future) for key, future in self.futures.items()}

    def take_diagnostic_preflight(self, settings, rows, existing):
        self._check_binding(settings, existing)
        if not self.enabled or self.initial is None:
            return None
        if self.preflight_taken:
            raise ValueError('目录预读诊断不能跨阶段重复使用。')
        observed = self._result(self.initial)
        for row in rows:
            _existing_course_page(self.client, existing['id'], row, observed=observed)
        self.preflight_taken = True
        return observed

    def take_first_round(self, settings, rows, details, home, state, recovery_state, observed):
        from .. import catalog_snapshots as catalog
        from ..catalog_schema import fingerprint
        self._check_binding(settings, {**self.existing, **home})
        if not self.enabled or self.initial is None:
            return None
        if not self.preflight_taken or self.first_taken:
            raise ValueError('目录预读阶段不匹配，不能恢复验收。')
        result = self.wait_first()  # Real failures propagate before any replay.
        self.first_taken = True
        def identity_graph(pages):
            return {key: value for page, body in pages for key, value in
                    ((('page', page['id']), page), (('children', page['id']), body))}
        if not _same_read_graph(identity_graph(self._result(self.initial)), identity_graph(observed)):
            raise ValueError('Notion 课程身份或内容在目录预读期间发生变化；已有内容保留，未确认完成。')
        keys = {row['id'] for row in rows}
        current_matches = (keys == self.details.keys() and state == self.state
            and recovery_state == self.recovery_state
            and all(details[key] == self.details[key]
                    and fingerprint(catalog.cached(self.root, key)) == self.envelopes[key] for key in keys))
        if not current_matches:
            # Structural/source changes need the existing fresh serial writer,
            # never a mutation authorized by these diagnostic observations.
            return result, True
        return result, any(not unchanged for _, unchanged in result.values())

    def verify_existing_write_targets(self, client, rows, observed):
        """A write fallback must retain every previously known managed ID."""
        from uuid import UUID
        def verify_page(identifier, parent):
            self._check_owner()
            page = client.get_page(identifier)
            try:
                valid = UUID(page['id']) == UUID(identifier)
            except (KeyError, ValueError, TypeError, AttributeError):
                valid = False
            if not valid or page.get('archived') or page.get('in_trash') or not _page_parent_matches(page, parent):
                raise ValueError('Notion 已有课程、中心或讲次身份或位置发生变化；原页面保留，未创建重复页面。')
        for row in rows:
            old = self.existing.get('courses', {}).get(row['id'])
            if not old:
                continue  # A genuinely new official course has no old ID.
            verify_page(old['id'], self.existing['id'])
            body = next((body for page, body in observed if page['id'] == old['id']), [])
            source = 'https://course.pku.edu.cn/#pku-course:' + row['id']
            if not any((part.get('text', {}).get('link') or {}).get('url') == source
                       for block in body if block.get('type') == 'paragraph'
                       for part in block.get('paragraph', {}).get('rich_text', [])):
                raise ValueError('Notion 已有课程来源身份发生变化；原页面保留，未创建重复课程。')
            for title, center in old['centers'].items():
                if not any(block.get('type') == 'child_page' and block.get('id') == center['id']
                           and block['child_page'].get('title') == title for block in body):
                    raise ValueError('Notion 已有课程中心身份或位置发生变化；原页面保留，未创建重复页面。')
                verify_page(center['id'], old['id'])
            parent = old.get('centers', {}).get('每节课', {}).get('id')
            for lecture in {**old.get('lectures', {}), **old.get('historical_lectures', {})}.values():
                if not parent:
                    raise ValueError('Notion 已有讲次父目录身份未确认；原页面保留，未创建重复页面。')
                verify_page(lecture['id'], parent)
                blocks = client.list_children(lecture['id'])
                marker = legacy._lecture_marker({'source_id': lecture.get('source_id')})
                if marker is None or not any(legacy._source_block_signature(block) ==
                                            legacy._source_block_signature(marker) for block in blocks):
                    raise ValueError('Notion 已有讲次来源身份发生变化；学生内容保留，未创建重复页面。')

    @property
    def live_futures(self):
        return [future for future in ([self.initial] if self.initial is not None else [])
                + list(self.futures.values()) if not future.done()]

    def close(self):
        if self.closed:
            return
        self.stopped.set()
        for future in self.live_futures:
            future.cancel()
        try:
            if self.pool is not None:
                self.pool.shutdown(wait=True, cancel_futures=True)
        finally:
            try:
                if self.client is not None:
                    self.client.__exit__(None, None, None)
            finally:
                self.closed = True
                with _MANIFEST_COMMIT_LOCK:
                    if self.registration is not None:
                        _ACTIVE_READ_PLANS.pop(self.registration, None)
                        self.registration = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                for future in ([self.initial] if self.initial is not None else []) + list(self.futures.values()):
                    reported = self.reported_errors.copy()
                    try:
                        self._result(future)
                    except BaseException as error:
                        if id(error) not in reported:
                            raise
        finally:
            self.close()


def _readonly_refresh_views(client, rows, details, home, state, state_path, recovery_state,
                            observed, check_cancel, first_round=None):
    """Probe every original predicate twice, with at most four upstream reads."""
    # Minimal/old trees already need writes. Do not spend an extra probe on a
    # known missing directory. This check grants no mutation or completion.
    for row in rows if first_round is None else []:
        course = _existing_course_page(client, home['id'], row, observed=observed)
        body = next((blocks for page, blocks in observed if course and page['id'] == course['id']), [])
        if not all(any(block.get('type') == 'child_page' and
                       block['child_page'].get('title') == title for block in body) for title in CENTERS):
            return None, False
    if not rows:
        return None, False
    semaphore, stopped = threading.BoundedSemaphore(4), threading.Event()

    def probe(row):
        view = _WorkspaceReadView(client, semaphore, stopped)
        try:
            # Worker contexts have no writer cohort. A probe cannot change any
            # Notion object or local completion receipt: source state is a copy
            # and root=None disables the original operations' local persistence.
            _project_course(view, None, home, row, details[row['id']], deepcopy(state),
                            state_path, recovery_state, observed)
        except _ReadPlanChanged:
            return view, False
        return view, True

    def collect(*, finish_all=False):
        pool = ThreadPoolExecutor(max_workers=min(4, len(rows)), thread_name_prefix='workspace-read')
        pending, result, changed = {}, {}, False
        iterator = iter(rows)
        def cancellation():
            if check_cancel:
                check_cancel()
        try:
            for _ in range(min(4, len(rows))):
                cancellation()
                row = next(iterator)
                pending[pool.submit(probe, row)] = row['id']
            while pending:
                cancellation()
                ready, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
                cancellation()
                for future in ready:
                    identifier = pending.pop(future)
                    result[identifier] = future.result()
                    changed |= not result[identifier][1]
                if finish_all or not changed:
                    for _ in ready:
                        cancellation()
                        row = next(iterator, None)
                        if row is not None:
                            pending[pool.submit(probe, row)] = row['id']
            return result, changed
        except BaseException:
            stopped.set()
            for future in pending:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    first, changed = first_round if first_round is not None else collect()
    if changed:
        if first_round is not None or any(detail.get('_historical_targets') for detail in details.values()):
            final, _ = collect(finish_all=True)
            if any(key not in final or not _same_read_graph(view.snapshot,
                        {read_key: final[key][0].snapshot[read_key] for read_key in view.snapshot
                         if read_key in final[key][0].snapshot})
                   for key, (view, _) in first.items()):
                raise ValueError('Notion 课程内容或讲次笔记在写入准备期间发生变化；学生内容保留，未确认完成。')
        return None, True
    final, changed = collect()
    # Independent complete responses include all student fields and every
    # nested read the original predicates requested. A concurrent edit cannot
    # be published as an unchanged, verified read-only workspace.
    if any(not _same_read_graph(first[key][0].snapshot, final[key][0].snapshot) for key in final):
        raise ValueError('Notion 课程内容或讲次笔记在同步期间发生变化；已有内容保留，未确认完成。')
    if changed:
        return None, True
    return {key: _WorkspaceReadView(client, semaphore, stopped, snapshot=view.snapshot)
            for key, (view, _) in final.items()}, True


@request_scope()
def sync_workspace(settings, parent_id: str | None = None, *, course_id: str | None = None,
                    check_cancel=None, pending_read_plan=None, operation_nonce=None,
                    replace_existing: bool = False) -> dict:
    """Create/update an independent tree under server projection permits.

    Both manifests and all generated block ownership stay separate from the
    legacy tree. No media, transcription or model call is involved.
    """
    if settings is None:
        raise ValueError("新版目录必须使用已验证的账号快照。")
    if pending_read_plan is not None and operation_nonce != pending_read_plan.operation_nonce:
        raise ValueError('目录预读不属于本轮操作；已有记录保留。')
    if pending_read_plan is not None:
        pending_read_plan._check_binding(settings, saved_workspace(Path(settings.data_dir)))
    from ..catalog_snapshots import activate_account
    activate_account(settings)
    root = Path(settings.data_dir)
    existing = {} if replace_existing else saved_workspace(root)
    old_home = legacy.legacy_home(root)
    if not getattr(settings, "notion_token", ""):
        raise ValueError("请先连接 Notion。")
    parent_id = parent_id or existing.get("parent_id")
    if not parent_id or parent_id == old_home.get("id"):
        raise ValueError("请选择旧主页之外的父页面，新版主页将单独创建。")
    if existing and existing["parent_id"] != parent_id:
        raise ValueError("新版主页已创建，请使用已有位置。")
    from .. import catalog_snapshots as catalog
    rows = campus_courses(root)["courses"]
    if course_id is not None:
        rows = [row for row in rows if row['id'] == course_id]
        if len(rows) != 1:
            raise ValueError('当前教学网课程未唯一确认，不能准备讲次。')
    status_path = root / "portal-read-status.json"
    if status_path.exists() and "courses" in json.loads(status_path.read_text("utf-8")):
        writable = []
        for row in rows:
            try:
                catalog.assert_portal_readable(root, row["id"])
            except catalog.CatalogUnavailable:
                continue
            if catalog.cached(root, row["id"]) is not None:
                writable.append(row)
        rows = writable
    if course_id is not None and len(rows) != 1:
        raise ValueError('当前教学网课程目录尚未完整确认，不能准备讲次。')
    if existing:
        # Diagnose a damaged course identity with reads before obtaining a
        # writer permit. This cannot clear an unknown writer, adopt an
        # unmarked same-name page, or authorize any Notion mutation. The
        # guarded body still rereads identities before performing writes.
        observed = (pending_read_plan.take_diagnostic_preflight(settings, rows, existing)
                    if pending_read_plan is not None else None)
        if observed is None:
            with NotionClient(settings.notion_token) as client:
                _verify_home(client.get_page(existing['id']), existing['id'], parent_id)
                _verify_selected_parent(client, parent_id)
                observed = _course_identity_snapshot(client, existing["id"])
                for row in rows:
                    _existing_course_page(client, existing["id"], row, observed=observed)
    with legacy._catalog_authority(root, settings, [row["id"] for row in rows],
                                   verified_steps=True, tracked_mutations=True,
                                   check_cancel=check_cancel,
                                   before_existing_projection=(pending_read_plan.wait_first
                                       if pending_read_plan is not None else None)):
        from . import workspace_profile
        from ..catalog_schema import fingerprint
        profile = workspace_profile.remote(settings)
        canonical = profile["profile"].get("notion_workspace_v2")
        if canonical and not replace_existing:
            if existing and existing["id"] != canonical["id"]:
                raise ValueError("本地新版主页与账号配置不一致，请重新登录读取。")
            existing = {**existing, **canonical, "schema": 2}
            if parent_id != canonical["parent_id"]:
                raise ValueError("账号新版主页位于另一父页面，请读取账号配置。")
        # Account preparation may overlap diagnostic reads, but the real
        # authority and second graph must never compete with those readers.
        # Joining here preserves the original global bound of four reads.
        if pending_read_plan is not None:
            pending_read_plan.wait_first()
        if not existing and parent_id not in {page["id"] for page in legacy.available_parents(settings.notion_token)}:
            raise ValueError("请选择已授权的 Notion 父页面。")
        with NotionClient(settings.notion_token) as client:
            if existing:
                # The selected canonical parent has an exact identity. A
                # fresh direct read proves present access and archive state;
                # global search pagination adds no authority for this page.
                # Reread after account calls inside the prepared boundary.
                _verify_selected_parent(client, parent_id)
            with verified_workspace_step('home'):
                if existing:
                    remote = client.get_page(existing["id"])
                    _verify_home(remote, existing['id'], parent_id)
                    if remote.get("archived") or not any(page.get("id") == existing["id"]
                                                           for page in client.list_child_pages(parent_id)):
                        raise ValueError("新版主页远端身份或位置已变化，未更新本地记录。")
                    home = {"id": existing["id"], "url": remote.get("url", existing.get("url", ""))}
                else:
                    made = client.create_page(parent_id, TITLE, retry=False)
                    remote = client.get_page(made["id"])
                    _verify_home(remote, made['id'], parent_id)
                    if remote.get("archived") or not any(page.get("id") == made["id"]
                                                           for page in client.list_child_pages(parent_id)):
                        raise ValueError("新版主页创建后验证失败。")
                    home = {"id": made["id"], "url": remote.get("url", "")}
                    _save(manifest_path(root), {"schema": 2, **home, "parent_id": parent_id,
                                                "active": False,
                                                "account_namespace": fingerprint({"session": settings.platform_token})})
            state_path, state = _workspace_source_state(root, home)
            recovery_state = legacy._source_state(root)
            courses = dict(existing.get("courses", {}))
            # Fresh inside the prepared writer boundary. The preflight reads
            # above diagnose damage before acquiring permits; they cannot be
            # reused as mutation authority after an intervening account call.
            observed_courses = _course_identity_snapshot(client, home['id'])
            details = {row['id']: campus_course(root, row['id']) for row in rows}
            histories, notion_token = {}, settings.notion_token
            for row in rows:
                details[row['id']], history = _bind_history(settings, row['id'], details[row['id']],
                    existing.get('courses', {}).get(row['id'], {}),
                    pending_read_plan.histories.get(row['id']) if pending_read_plan is not None else None)
                if history is not None:
                    histories[row['id']] = history
            first_round = (pending_read_plan.take_first_round(settings, rows, details, home, state, recovery_state, observed_courses)
                           if pending_read_plan is not None else None)
            views, attempted = _readonly_refresh_views(client, rows, details, home, state,
                state_path, recovery_state, observed_courses, check_cancel, first_round=first_round)
            _assert_history_scope(settings, histories, notion_token)
            if views is None and pending_read_plan is not None and first_round is not None:
                pending_read_plan.verify_existing_write_targets(client, rows, observed_courses)
            if views is None and attempted:
                # A probe requiring any write grants no authority. Discard all
                # its observations and restart the original serial operation
                # with fresh complete course identities.
                observed_courses = _course_identity_snapshot(client, home['id'])
            for row in rows:
                if check_cancel:
                    check_cancel()
                _assert_history_scope(settings, histories, notion_token)
                reader = views[row['id']] if views is not None else client
                # Keep every original verified journal step on the permit's
                # calling thread, after both full independent read rounds.
                courses[row['id']] = _project_course(reader, root, home, row,
                    details[row['id']], state, state_path, recovery_state, observed_courses)
            _assert_history_scope(settings, histories, notion_token)
            # A concurrent removal, move or duplicate introduced anywhere in
            # the batch cannot be published as a verified completed workspace.
            with verified_workspace_step('course'):
                final_courses = _course_identity_snapshot(client, home['id'])
                for row in rows:
                    actual = _existing_course_page(client, home['id'], row, observed=final_courses)
                    if actual is None or actual['id'] != courses[row['id']]['id']:
                        raise ValueError('新版课程身份在同步期间发生变化，未确认目录完成。')
            result = {"schema": 2, **home, "parent_id": parent_id, "courses": courses,
                      "legacy_home_id": old_home.get("id", ""), "active": False,
                      "account_namespace": fingerprint({"session": settings.platform_token})}
            _assert_history_scope(settings, histories, notion_token)
            _save(state_path, state)
            _save(manifest_path(root), result)
    # Every Notion write above has been independently read back. Finish the
    # projection permits before publishing account configuration: a later TLS
    # or account transport error cannot make those completed writes unknown.
    # If permit release itself is uncertain, the context raises and keeps its
    # durable releasing receipt; the workspace remains inactive.
    publish_options = {'replace_existing': True} if replace_existing else {}
    result["profile_revision"] = workspace_profile.publish(settings, result, profile, **publish_options)
    _assert_history_scope(settings, histories, notion_token)
    result["active"] = True
    _save(manifest_path(root), result)
    return result


def move_workspace(settings, parent_id: str) -> dict:
    from . import workspace_profile
    if settings is None:
        raise ValueError("新版主页移动需要已验证的账号配置。")
    from ..catalog_snapshots import activate_account
    activate_account(settings)
    root = Path(settings.data_dir)
    existing = saved_workspace(root)
    if not existing or parent_id in {existing["id"], legacy.legacy_home(root).get("id")}:
        raise ValueError("请选择新版主页和旧主页之外的父页面。")
    with legacy._catalog_authority(root, settings):
        profile = workspace_profile.remote(settings)
        canonical = profile["profile"].get("notion_workspace_v2") or {}
        if canonical.get("id") != existing["id"]:
            raise ValueError("新版主页账号身份未确认，禁止移动。")
        if parent_id not in {page["id"] for page in legacy.available_parents(settings.notion_token)}:
            raise ValueError("请选择已授权的 Notion 父页面。")
        with NotionClient(settings.notion_token) as client:
            page = client.get_page(existing["id"])
            if (page.get("parent") or {}).get("page_id") != parent_id:
                client.move_page(existing["id"], parent_id)
            page = client.get_page(existing["id"])
            if page.get("archived") or (page.get("parent") or {}).get("page_id") != parent_id:
                raise ValueError("新版主页移动后验证失败。")
        moved = {**existing, "parent_id": parent_id, "url": page.get("url", existing["url"]), "active": False}
        _save(manifest_path(root), moved)
        moved["profile_revision"] = workspace_profile.publish(settings, moved, profile)
        moved["active"] = True
        _save(manifest_path(root), moved)
        return moved


@request_scope()
def read_projection(settings, *, course_id: str | None = None) -> dict:
    """Hydrate lesson note links on another device without writing to Notion."""
    from urllib.parse import quote
    from ..catalog_snapshots import activate_account
    activate_account(settings)
    root = Path(settings.data_dir)
    with _MANIFEST_COMMIT_LOCK:
        workspace = saved_workspace(root)
        # Reuse exactly the plan's stateless six-field identity definition.
        binding = PendingWorkspaceReadPlan._binding(None, settings)
    if not workspace.get("active"):
        return workspace
    courses = dict(workspace.get("courses", {}))
    histories, notion_token = {}, settings.notion_token
    with NotionClient(settings.notion_token) as client:
        page = client.get_page(workspace["id"])
        _verify_home(page, workspace['id'], workspace.get('parent_id'))
        # One fresh remote identity snapshot per full read, rather than reading
        # every course's blocks again for each course. Keep all marker and
        # duplicate checks; a selected-course read retains its existing scope.
        observed = ([(page, client.list_children(page["id"]))
                     for page in client.list_child_pages(workspace["id"])]
                    if course_id is None else None)
        for row in campus_courses(root)["courses"]:
            if course_id is not None and row["id"] != course_id:
                continue
            from ..catalog_snapshots import cached
            if cached(root, row["id"]) is None:
                continue
            detail, history = _bind_history(settings, row['id'], campus_course(root, row['id']),
                                      workspace.get('courses', {}).get(row['id'], {}))
            if history is not None:
                histories[row['id']] = history
            _verify_historical_course(client, workspace['id'], row, detail, observed)
            course = _course_page(client, workspace["id"], row, create=False, observed=observed)
            directory_pages = client.list_child_pages(course["id"])
            centers, extra_pages = _classify_course_children(directory_pages, course['id'])
            center = centers.get("每节课")
            if not center:
                raise ValueError("新版每节课目录尚未验证。")
            children = client.list_child_pages(center["id"])
            blocks = {page["id"]: client.list_children(page["id"]) for page in children}
            lectures = {}
            for lesson in detail["lessons"]:
                marker = legacy._linked_paragraph("查看教学网讲次来源", "https://course.pku.edu.cn/" +
                    "webapps/bb-streammedia-hqy-BBLEARN/videoList.action#" + quote(lesson["source_id"], safe=""))
                signature = legacy._source_block_signature(marker)
                matches = [page for page in children if any(legacy._source_block_signature(block) == signature
                                                           for block in blocks[page["id"]])]
                if len(matches) != 1:
                    raise ValueError("新版讲次身份未唯一确认，保留原链接缓存。")
                lecture = client.get_page(matches[0]["id"])
                notes = [client.get_page(page["id"]) for page in client.list_child_pages(lecture["id"])
                         if page.get("title", "").startswith(("转写笔记 ·", "更新笔记 ·"))]
                notes = [page for page in notes if not page.get("archived")]
                latest = max(notes, key=lambda page: (page.get("created_time", ""), page["id"]), default={})
                if not latest:
                    prior = [item for item in workspace.get("courses", {}).get(row["id"], {}).get("lectures", {}).values()
                             if item.get("source_id") == lesson["source_id"]]
                    links = {part["text"]["link"]["url"]
                             for block in blocks[lecture["id"]] if block.get("type") == "paragraph"
                             for part in block.get("paragraph", {}).get("rich_text", [])
                             if part.get("text", {}).get("content") == SUMMARY_LINK
                             and part.get("text", {}).get("link", {}).get("url")}
                    if not links and len(prior) == 1 and prior[0].get("note_url"):
                        links.add(prior[0]["note_url"])
                    if len(links) > 1:
                        raise ValueError("已有总结链接不唯一，请先核对。")
                    if links:
                        latest = _summary_page(client, next(iter(links)))
                from .summary_acceptance import published_status
                acceptance = (published_status(client, latest["id"], lesson["source_id"])
                              if latest else {"accepted": False})
                lectures[lesson["id"]] = {"id": lecture["id"], "source_id": lesson["source_id"],
                                           "url": lecture.get("url", ""), "note_url": latest.get("url", ""),
                                           "note_parent_id": (latest.get("parent") or {}).get("page_id", ""),
                                           "summary_acceptance": acceptance}
            historical = _historical_projection(client, course, center['id'], detail)
            if historical:
                final = _historical_projection(client, course, center['id'], {
                    **detail, '_historical_targets': {key: {**detail['_historical_targets'][key], 'mapping': item}
                                                   for key, item in historical.items()}})
                if {item['id'] for item in final.values()} & {item['id'] for item in lectures.values()}:
                    raise ValueError('当前讲次与历史讲次页面重叠；未确认投影。')
                historical = final
            courses[row["id"]] = {**course, "centers": centers, "extra_pages": extra_pages,
                                  "lectures": lectures,
                                  **({'historical_lectures': historical,
                                      'history_binding': deepcopy(detail['_history_binding'])} if historical else {})}
    result = {**workspace, "courses": courses}
    _assert_history_scope(settings, histories, notion_token)
    with _MANIFEST_COMMIT_LOCK:
        if (binding != PendingWorkspaceReadPlan._binding(None, settings)
                or saved_workspace(root) != workspace):
            raise ValueError('目录回读账号或本地记录发生变化；保留已有记录，未覆盖。')
        # Return genuine fresh display data without changing an in-flight
        # writer's full binding. It can be hydrated again after that operation.
        if not any(plan_root == str(root.resolve()) for plan_root, _ in _ACTIVE_READ_PLANS.values()):
            _save(manifest_path(root), result)
    return result
