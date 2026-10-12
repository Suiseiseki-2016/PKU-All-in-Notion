"""The September 29 incident, using only in-memory Notion and portal data."""
from copy import deepcopy

import pytest

from pku_sync.panel import notion_home


class IndexClient:
    def __init__(self):
        self.blocks = []
        self.sequence = 0
        self.drop_writes = False
        self.replacements = 0

    def list_child_pages(self, page_id):
        return []

    def list_children(self, page_id):
        return deepcopy(self.blocks)

    def append_blocks(self, page_id, blocks, **kwargs):
        if self.drop_writes:
            return []
        added = []
        for block in blocks:
            self.sequence += 1
            added.append({**deepcopy(block), "id": f"b{self.sequence}"})
        self.blocks.extend(added)
        after = kwargs.get("after")
        if after is not None:
            del self.blocks[-len(added):]
            index = next(i + 1 for i, block in enumerate(self.blocks) if block["id"] == after)
            self.blocks[index:index] = added
        return added

    def replace_page_content(self, page_id, blocks):
        self.replacements += 1
        self.blocks.clear()
        return self.append_blocks(page_id, blocks)


def fixture(count=5):
    dates = ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"]
    lessons = [{"id": f"source-{i}", "date": day, "title": "发展心理学"}
               for i, day in enumerate(dates[:count])]
    titles = notion_home._lecture_titles({"lessons": lessons})
    children = {titles[row["id"]]: {"id": f"page-{row['id']}"} for row in lessons}
    return lessons, titles, children


def sync(client, state, count=5):
    notion_home._sync_recording_section(client, "index", *fixture(count), state, "_101545_1")


def text(block):
    return "".join(p.get("plain_text") or p.get("text", {}).get("content", "")
                   for p in block.get(block["type"], {}).get("rich_text", []))


def test_five_row_local_digest_cannot_hide_four_row_remote_index():
    client, state = IndexClient(), {}
    sync(client, state)
    digest = deepcopy(state)
    client.blocks = [b for b in client.blocks if "2026-09-29" not in text(b)]
    sync(client, state)
    assert any("2026-09-29" in text(b) for b in client.blocks)
    assert state == digest


def test_old_four_lecture_device_cannot_remove_fifth_lecture_or_student_notes():
    client = IndexClient()
    sync(client, {})
    client.append_blocks("index", [notion_home._linked_paragraph("我的补充", "https://example.org")])
    sync(client, {}, count=4)
    assert any("2026-09-29" in text(b) for b in client.blocks)
    assert any("我的补充" in text(b) for b in client.blocks)
    assert client.replacements == 0


def test_write_not_visible_on_readback_must_not_advance_digest():
    client, state = IndexClient(), {}
    client.drop_writes = True
    with pytest.raises(ValueError, match="验证"):
        sync(client, state)
    assert "_101545_1|课堂记录" not in state


def test_legacy_digest_is_revalidated_without_duplicate_rows():
    client, state = IndexClient(), {}
    sync(client, state)
    before = deepcopy(client.blocks)
    sync(client, state)
    assert client.blocks == before


def test_generated_lecture_child_blocks_do_not_block_their_own_index():
    client, state = IndexClient(), {}
    lessons, titles, children = fixture()
    original = [{'id': page['id'], 'type': 'child_page', 'child_page': {'title': title}}
                for title, page in children.items()]
    client.blocks = deepcopy(original)
    sync(client, state)
    sync(client, state)
    assert client.blocks[:len(original)] == original
    assert sum('2026-09-29' in text(block) for block in client.blocks) == 1
    assert client.replacements == 0


@pytest.mark.parametrize('block', [
    {'id': 'student-page', 'type': 'child_page', 'child_page': {'title': '我的补充'}},
    notion_home._linked_paragraph('我的说明', 'https://example.org'),
])
def test_unrecognized_content_still_blocks_index_adoption(block):
    client = IndexClient()
    client.blocks = [deepcopy(block)]
    with pytest.raises(ValueError, match='手动内容'):
        sync(client, {})
    assert client.blocks == [block]


def test_changed_student_link_is_preserved_when_official_link_is_repaired():
    client, state = IndexClient(), {}
    sync(client, state)
    client.blocks[-1]["paragraph"]["rich_text"][0]["text"]["link"]["url"] = "https://example.org/my-notes"
    sync(client, state)
    links = [p["text"].get("link", {}).get("url") for b in client.blocks
             for p in b.get("paragraph", {}).get("rich_text", [])]
    assert "https://example.org/my-notes" in links
    assert "https://www.notion.so/pagesource4" in links


def test_catalog_cannot_write_without_account_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_home, "NotionClient", lambda *args: pytest.fail("Notion must not be contacted"))
    with pytest.raises(ValueError, match="账号"):
        notion_home.sync_catalog(tmp_path, "fake-token")


def test_failed_portal_read_prevents_shared_projection_even_with_local_digest(tmp_path, monkeypatch):
    from pku_sync import catalog_snapshots as catalog
    catalog.read_status(tmp_path, False, "authentication_failed")
    monkeypatch.setattr(notion_home, "NotionClient", lambda *args: pytest.fail("shared write after failure"))
    monkeypatch.setattr(catalog, "current", lambda *args: pytest.fail("should stop before account calls"))
    with pytest.raises(ValueError, match="只读"):
        notion_home.sync_catalog(tmp_path, "fake", settings=object())


@pytest.mark.parametrize('mutation', ['namespace', 'missing-course', 'boolean', 'reading'])
def test_partial_batch_projection_rejects_unknown_or_changed_read_authority(tmp_path, mutation):
    from pku_sync import catalog_snapshots as catalog
    import json
    cid = '_104157_1'
    value = {'complete': False, 'reason': 'partial',
             'account_namespace': catalog.cache_path(tmp_path, cid).parent.name,
             'courses': {cid: {'complete': True, 'reason': 'complete'}}}
    if mutation == 'namespace':
        value['account_namespace'] = 'another-account'
    elif mutation == 'missing-course':
        value['courses'] = {'_101545_1': {'complete': True}}
    elif mutation == 'boolean':
        value['courses'][cid]['complete'] = 1
    else:
        value['reason'] = 'reading'
    (tmp_path / 'portal-read-status.json').write_text(json.dumps(value), 'utf-8')
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.projection_readable_course_ids(tmp_path, [cid])


def test_generated_document_repairs_remote_drift_and_retains_manual_edits():
    client, state = IndexClient(), {}
    marker = notion_home._linked_paragraph("教学网正式目录", "https://course.pku.edu.cn")
    old = notion_home._linked_paragraph("正式作业旧要求", "https://course.pku.edu.cn/old")
    new = notion_home._linked_paragraph("正式作业新要求", "https://course.pku.edu.cn/new")
    notion_home._reconcile_document(client, "page", [marker, old], state, "document")
    client.blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"] = "我手写的补充"
    notion_home._reconcile_document(client, "page", [marker, new], state, "document")
    assert text(client.blocks[1]) == "正式作业新要求"
    assert any(text(block) == "我手写的补充" for block in client.blocks)
    client.blocks.pop(1)
    notion_home._reconcile_document(client, "page", [marker, new], state, "document")
    assert text(client.blocks[1]) == "正式作业新要求"


def test_generated_document_does_not_accept_unverified_write():
    client, state = IndexClient(), {}
    client.drop_writes = True
    with pytest.raises(ValueError, match="验证"):
        notion_home._reconcile_document(client, "page", [notion_home._linked_paragraph("来源", "https://course.pku.edu.cn")], state, "key")
    assert not state
