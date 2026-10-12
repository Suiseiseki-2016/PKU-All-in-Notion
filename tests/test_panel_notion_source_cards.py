"""A teacher's latest requirements must stay above old Notion card content."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from pku_sync.panel.notion_home import (_item_blocks, _source_block_state,
                                        _source_state, _save_source_state,
                                        _sync_source_section, _sync_source_status,
                                        _next_formal_assignment_due)


class CardClient:
    def __init__(self):
        self.pages = {"section": []}
        self.titles = {}
        self.next_id = 0

    def _new(self, block):
        self.next_id += 1
        return {**deepcopy(block), "id": f"block-{self.next_id}"}

    def list_child_pages(self, page_id):
        return [{"id": identifier, "title": title}
                for title, identifier in self.titles.items()]

    def create_page(self, parent_id, title, *, children, retry):
        identifier = f"page-{len(self.titles) + 1}"
        self.titles[title] = identifier
        self.pages[identifier] = [self._new(block) for block in children]
        return {"id": identifier}

    def list_children(self, page_id):
        return deepcopy(self.pages[page_id])

    def append_blocks(self, page_id, blocks, *, after=None, retry=True):
        added = [self._new(block) for block in blocks]
        current = self.pages[page_id]
        index = next((i + 1 for i, block in enumerate(current)
                      if block["id"] == after), len(current))
        current[index:index] = added
        return deepcopy(added)

    def update_paragraph(self, block_id, text):
        for blocks in self.pages.values():
            for block in blocks:
                if block["id"] == block_id:
                    block["paragraph"]["rich_text"] = [
                        {"type": "text", "text": {"content": text}}]
                    return block
        raise AssertionError(block_id)

    def archive_block(self, block_id, *, retry=True):
        for blocks in self.pages.values():
            for index, block in enumerate(blocks):
                if block["id"] == block_id:
                    return blocks.pop(index)
        raise AssertionError(block_id)


def test_unchanged_section_status_retains_real_observation_without_timestamp_only_write():
    client = CardClient()
    detail = {'synced_at': '2026-10-08T00:00:00Z',
              'synced_at_label': '2026-10-08 08:00 北京时间'}
    _sync_source_status(client, 'section', '资料', detail, 3)
    before = deepcopy(client.pages)
    detail.update(synced_at='2026-10-08T00:05:00Z',
                  synced_at_label='2026-10-08 08:05 北京时间')
    def forbidden(*args, **kwargs):
        raise AssertionError('Unchanged source status must not issue a write just for its clock')
    original_update = client.update_paragraph
    client.update_paragraph = forbidden
    _sync_source_status(client, 'section', '资料', detail, 3)
    assert client.pages == before
    client.update_paragraph = original_update
    _sync_source_status(client, 'section', '资料', detail, 4)
    assert '08:05 北京时间 读取到 4 项资料' in visible(client.pages['section'][0])
    assert client.pages != before


def test_source_card_complete_reads_overlap_but_fail_before_any_partial_write(tmp_path):
    import threading
    client, state = CardClient(), {}
    detail = course('交 PDF', '10月1日')
    detail['assignments'] *= 4
    detail['assignments'] = [dict(item, content_id=f'_{number}_1',
                                 source_id=f'source-{number}', title=f'作业{number}')
                             for number, item in enumerate(detail['assignments'])]
    _sync_source_section(client, tmp_path, '_456_1', '作业', 'section', detail, state)
    before, old_state = deepcopy(client.pages), deepcopy(state)
    original_read = client.list_children
    barrier = threading.Barrier(4, timeout=2)
    observed = []
    def overlapping_read(page):
        if page != 'section':
            observed.append(page)
            barrier.wait()
            if page == client.titles['作业3']:
                raise RuntimeError('One real card could not be completely read')
        return original_read(page)
    client.list_children = overlapping_read
    with pytest.raises(RuntimeError, match='One real card'):
        _sync_source_section(client, tmp_path, '_456_1', '作业', 'section', detail, state)
    assert len(observed) == 4
    assert client.pages == before and state == old_state


def test_student_edit_after_batch_observation_is_detected_and_preserved(tmp_path):
    from pku_sync.notion import markdown_to_blocks
    client, state = CardClient(), {}
    sync(client, state, '交 PDF', '10月1日', tmp_path)
    page = client.titles['第一次书面作业']
    original_read = client.list_children
    changed = []
    def read(target):
        result = original_read(target)
        if target == page and not changed:
            changed.append(True)
            client.pages[page].append(client._new(markdown_to_blocks('学生在同步期间新增的答案')[0]))
        return result
    client.list_children = read
    with pytest.raises(ValueError, match='同步期间发生变化'):
        sync(client, state, '交 PDF', '10月1日', tmp_path)
    assert visible(client.pages[page][-1]) == '学生在同步期间新增的答案'


def test_remote_missing_requirement_is_repaired_even_with_matching_digest():
    client, state = CardClient(), {}
    blocks = sync(client, state, "请提交 PDF", "10月1日 23:30")
    index = next(i for i, block in enumerate(blocks) if "要交什么" in visible(block))
    blocks.pop(index)
    repaired = sync(client, state, "请提交 PDF", "10月1日 23:30")
    assert any("要交什么：请提交 PDF" == visible(block) for block in repaired)


def test_workspace_adopts_only_matching_child_receipt_without_touching_legacy(tmp_path):
    import json
    client, old = CardClient(), {}
    detail = course('交 PDF', '10月1日')
    _sync_source_section(client, tmp_path, '_1_1', '作业', 'section', detail, old)
    legacy_bytes = (tmp_path / 'notion-source-state.json').read_bytes()
    before = deepcopy(client.pages)
    journal = tmp_path / '.workspace/source-state.json'
    current = {}
    _sync_source_section(client, tmp_path, '_1_1', '作业', 'section', detail, current,
                         state_path=journal, recovery_state=old)
    assert client.pages == before
    assert json.loads(journal.read_text('utf-8')) == old == current
    assert (tmp_path / 'notion-source-state.json').read_bytes() == legacy_bytes


def test_foreign_pending_receipt_cannot_be_adopted_into_workspace(tmp_path):
    import json
    client, old = CardClient(), {}
    detail = course('交 PDF', '10月1日')
    _sync_source_section(client, tmp_path, '_1_1', '作业', 'section', detail, old)
    foreign = deepcopy(old)
    for row in foreign.values():
        row['id'] = 'foreign-page'
        row['pending'] = {'id': 'foreign-page', 'created_blocks': {'foreign-block': 'x'}}
    journal, current = tmp_path / '.workspace/source-state.json', {}
    _sync_source_section(client, tmp_path, '_1_1', '作业', 'section', detail, current,
                         state_path=journal, recovery_state=foreign)
    saved = json.loads(journal.read_text('utf-8'))
    assert saved and all(row['id'] != 'foreign-page' for row in saved.values())
    assert all('foreign-block' not in row['owned_blocks'] for row in saved.values())


def test_new_device_updates_same_source_card_without_local_manifest_and_keeps_notes():
    client = CardClient()
    data = course("交 PDF", "10月1日")
    data["assignments"][0]["source_id"] = "pku:course:assignments:_123_1"
    _sync_source_section(client, None, "_456_1", "作业", "section", data, {})
    page_id = client.titles["第一次书面作业"]
    client.append_blocks(page_id, [paragraph("我写的补充提醒")])
    data["assignments"][0]["instructions"] = "老师改为交 Word"
    data["assignments"][0]["due_at_label"] = "10月3日"
    _sync_source_section(client, None, "_456_1", "作业", "section", data, {})
    texts = [visible(block) for block in client.pages[page_id]]
    assert "10月3日" in texts[0]
    assert "要交什么：老师改为交 Word" in texts
    assert "我写的补充提醒" in texts


class InterruptedCardClient(CardClient):
    def __init__(self):
        super().__init__()
        self.fail_after_append = False
        self.fail_after_archive = False

    def append_blocks(self, page_id, blocks, *, after=None, retry=True):
        added = super().append_blocks(page_id, blocks, after=after, retry=retry)
        if self.fail_after_append:
            self.fail_after_append = False
            raise RuntimeError("response lost after Notion accepted append")
        return added

    def archive_block(self, block_id, *, retry=True):
        archived = super().archive_block(block_id, retry=retry)
        if self.fail_after_archive:
            self.fail_after_archive = False
            raise RuntimeError("interrupted partway through archiving")
        return archived


class FullPageAppendResponseClient(CardClient):
    """Simulate PATCH returning existing children along with inserted blocks."""

    def __init__(self):
        super().__init__()
        self.fail_next_update = False

    def append_blocks(self, page_id, blocks, *, after=None, retry=True):
        super().append_blocks(page_id, blocks, after=after, retry=retry)
        return self.list_children(page_id)

    def update_paragraph(self, block_id, text):
        if self.fail_next_update:
            self.fail_next_update = False
            raise RuntimeError("interrupted after new IDs were saved")
        return super().update_paragraph(block_id, text)


def paragraph(text):
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text}}]}}


def visible(block):
    body = block.get(block.get("type") or "") or {}
    return "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "")
                   for part in body.get("rich_text") or [])


def course(instructions, due):
    return {"assignments": [{"title": "第一次书面作业", "content_id": "_123_1",
                             "instructions": instructions, "due_at_label": due}],
            "announcement_tasks": [], "synced_at": "2026-09-27T00:00:00+08:00",
            "synced_at_label": "2026-09-27 00:00 北京时间"}


def sync(client, state, instructions, due, data_dir=None):
    _sync_source_section(client, data_dir, "_456_1", "作业", "section",
                         course(instructions, due), state)
    return client.pages[client.titles["第一次书面作业"]]


def test_formal_card_links_original_teacher_file_with_login_context():
    blocks = _item_blocks("_456_1", "作业", {
        "title": "第一次书面作业", "source": "content-tree",
        "content_id": "_123_1", "files": ["书面作业1-2026.pdf"],
        "attachments": [{"filename": "书面作业1-2026.pdf",
                         "path": "/bbcswebdav/pid-1/xid-1"}],
    }, "2026-09-27 00:00 北京时间")
    link = next(block for block in blocks if "登录教学网查看原题" in visible(block))
    assert link["paragraph"]["rich_text"][0]["text"]["link"]["url"] == (
        "https://course.pku.edu.cn/bbcswebdav/pid-1/xid-1")
    assert "需要教学网登录" in " ".join(map(visible, blocks))
    assert any("打开教学网作业页面（提交或查看历史）" in visible(block)
               for block in blocks)


def test_formal_card_warns_when_teacher_file_has_no_stable_link():
    blocks = _item_blocks("_456_1", "作业", {
        "title": "第一次书面作业", "source": "content-tree",
        "content_id": "_123_1", "files": ["书面作业1-2026.pdf"],
        "attachments": [{"filename": "书面作业1-2026.pdf",
                         "path": "/bbcswebdav/pid-1/xid-1?token=secret"}],
    }, "2026-09-27 00:00 北京时间")
    texts = " ".join(map(visible, blocks))
    assert "原题附件尚无可用直达链接" in texts
    assert "登录教学网查看原题" not in texts
    assert "secret" not in str(blocks)


def test_explicit_class_attendance_requirement_is_marked_as_irreplaceable():
    blocks = _item_blocks("_456_1", "作业", {
        "title": "随堂测试一", "source": "content-tree",
        "content_id": "_124_1", "instructions": "随堂考勤，计入平时成绩~",
    }, "2026-09-27 00:00 北京时间")
    assert "笔记不能代替本人参与" in " ".join(map(visible, blocks))

    ordinary = _item_blocks("_456_1", "作业", {
        "title": "书面作业", "source": "content-tree",
        "content_id": "_125_1", "instructions": "上传书面答案",
    }, "2026-09-27 00:00 北京时间")
    assert "笔记不能代替本人参与" not in " ".join(map(visible, ordinary))


def test_old_layout_keeps_manual_notes_but_places_current_requirements_first():
    client = CardClient()
    client.titles["第一次书面作业"] = "page-1"
    client.pages["page-1"] = [client._new(paragraph(text)) for text in (
        "来源：教学网正式作业目录", "截止时间：旧日期", "我的手写提醒")]
    state = {"_456_1|作业|_123_1": {"id": "page-1", "hash": "old", "layout": 1}}

    blocks = sync(client, state, "交 PDF", "10月1日 23:30")
    texts = [visible(block) for block in blocks]
    assert "10月1日 23:30" in texts[0]
    assert texts.index("要交什么：交 PDF") < next(i for i, value in enumerate(texts)
                                             if "旧版同步记录" in value)
    assert texts.index("截止时间：旧日期") > next(i for i, value in enumerate(texts)
                                             if "旧版同步记录" in value)
    assert "我的手写提醒" in texts

    blocks = sync(client, state, "改交 DOCX", "10月3日 23:30")
    texts = [visible(block) for block in blocks]
    assert "10月3日 23:30" in texts[0]
    assert "10月1日 23:30" not in " ".join(texts)
    assert "要交什么：交 PDF" not in texts
    assert texts.count("我的手写提醒") == 1
    assert sum("旧版同步记录" in value for value in texts) == 1


def test_new_card_replaces_owned_blocks_and_preserves_later_manual_content():
    client = CardClient()
    state = {}
    blocks = sync(client, state, "初版要求", "10月1日")
    page_id = client.titles["第一次书面作业"]
    client.append_blocks(page_id, [paragraph("我自己的笔记")])

    blocks = sync(client, state, "新版要求", "10月3日")
    texts = [visible(block) for block in blocks]
    assert "10月3日" in texts[0]
    assert "初版要求" not in " ".join(texts)
    assert texts.count("我自己的笔记") == 1
    assert texts.index("我自己的笔记") > next(i for i, value in enumerate(texts)
                                             if "旧版同步记录" in value)


def test_new_card_update_removes_old_requirements_without_history_note():
    client = CardClient()
    state = {}
    sync(client, state, "初版要求", "10月1日")

    blocks = sync(client, state, "新版要求", "10月3日")
    texts = [visible(block) for block in blocks]
    assert "10月3日" in texts[0]
    assert "新版要求" in " ".join(texts)
    assert "初版要求" not in " ".join(texts)
    assert "10月1日" not in " ".join(texts)
    assert not any("旧版同步记录" in text for text in texts)


def test_refresh_time_only_updates_owned_timestamp_without_retiring_old_blocks():
    client, state = CardClient(), {}
    detail = course("原有正式要求", "10月1日")
    detail["assignments"][0]["source_id"] = "pku:course:assignments:_123_1"
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    page_id = client.titles["第一次书面作业"]
    client.append_blocks(page_id, [paragraph("学生保留内容")])
    original = deepcopy(client.pages[page_id])
    def forbid_retirement(*args, **kwargs):
        raise AssertionError("a later runner cannot retire original owned blocks")
    client.archive_block = forbid_retirement
    detail["synced_at"] = "2026-10-08T18:00:00+08:00"
    detail["synced_at_label"] = "2026-10-08 18:00 北京时间"
    # A source field that is not rendered must not force a body replacement.
    detail["assignments"][0]["observed_status"] = "fresh"
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    actual = client.pages[page_id]
    assert [row["id"] for row in actual] == [row["id"] for row in original]
    changed = [(before, after) for before, after in zip(original, actual) if before != after]
    assert len(changed) == 1
    assert "目录读取：2026-10-08 18:00 北京时间" in visible(changed[0][1])
    assert actual[-1] == original[-1]
    assert not next(iter(state.values())).get("pending")


def test_unchanged_source_hash_does_not_rewrite_card_for_new_read_time():
    client, state = CardClient(), {}
    detail = course("原有正式要求", "10月1日")
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    page_id = client.titles["第一次书面作业"]
    original = deepcopy(client.pages[page_id])
    detail["synced_at"] = "2026-10-08T18:00:00+08:00"
    detail["synced_at_label"] = "2026-10-08 18:00 北京时间"
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    assert client.pages[page_id] == original


def test_already_present_current_source_body_reuses_readback_without_adopting_blocks():
    client, state = CardClient(), {}
    detail = course("原有正式要求", "10月1日")
    detail["assignments"][0]["source_id"] = "pku:course:assignments:_123_1"
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    page_id = client.titles["第一次书面作业"]
    owned_before = deepcopy(next(iter(state.values()))["owned_blocks"])
    # A different writer already inserted the verified source text. Its IDs
    # must never be granted local mutation ownership by this read-only reuse.
    client.append_blocks(page_id, _item_blocks("_456_1", "作业", detail["assignments"][0],
                                              detail["synced_at_label"])[1:],
                         after=client.pages[page_id][0]["id"])
    client.append_blocks(page_id, [paragraph("学生保留内容")])
    original = deepcopy(client.pages[page_id])
    def forbidden(*args, **kwargs):
        raise AssertionError("Do not replay an append or retire another writer's original blocks")
    client.append_blocks = client.archive_block = forbidden
    detail["assignments"][0]["observed_status"] = "fresh"
    detail["synced_at_label"] = "2026-10-08 18:00 北京时间"
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    assert client.pages[page_id] == original
    assert next(iter(state.values()))["owned_blocks"] == owned_before
    # A matching digest cannot conceal a later edit of the unowned prefix.
    changed = client.pages[page_id][1]
    changed["paragraph"]["rich_text"][0]["text"]["content"] = "学生修改的要求"
    edited = deepcopy(client.pages[page_id])
    with pytest.raises(AssertionError, match="Do not replay"):
        _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    assert client.pages[page_id] == edited
    assert next(iter(state.values()))["owned_blocks"] == owned_before


def test_remote_read_time_change_reuses_verified_body_without_adopting_signature():
    client, state = CardClient(), {}
    detail = course("原有正式要求", "10月1日")
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    page_id = client.titles["第一次书面作业"]
    owned_before = deepcopy(next(iter(state.values()))["owned_blocks"])
    timestamp = client.pages[page_id][-1]
    client.update_paragraph(timestamp["id"], "请以教学网原页面为准。目录读取：2026-10-08 18:00 北京时间。课程标识：_456_1。")
    client.append_blocks(page_id, [paragraph("学生保留内容")])
    original = deepcopy(client.pages[page_id])
    def forbidden(*args, **kwargs):
        raise AssertionError("A different observation time is not authority to rewrite or own the body")
    client.append_blocks = client.archive_block = client.update_paragraph = forbidden
    _sync_source_section(client, None, "_456_1", "作业", "section", detail, state)
    assert client.pages[page_id] == original
    assert next(iter(state.values()))["owned_blocks"] == owned_before


def test_existing_layout_two_card_marks_untracked_old_body_as_historical():
    client = CardClient()
    client.titles["第一次书面作业"] = "page-1"
    client.pages["page-1"] = [client._new(paragraph(text)) for text in (
        "正式作业 · 截止：10月1日 · 第一次书面作业",
        "要交什么：旧要求", "目录读取时间：旧同步")]
    state = {"_456_1|作业|_123_1": {"id": "page-1", "hash": "old", "layout": 2}}

    blocks = sync(client, state, "最新要求", "10月3日")
    texts = [visible(block) for block in blocks]
    note_index = next(i for i, value in enumerate(texts) if "旧版同步记录" in value)
    assert "10月3日" in texts[0]
    assert texts.index("要交什么：最新要求") < note_index
    assert texts.index("要交什么：旧要求") > note_index


def test_layout_three_migration_removes_owned_duplicate_history_but_keeps_student_notes():
    client = CardClient()
    state = {}
    blocks = sync(client, state, "交 PDF", "10月1日")
    page_id = client.titles["第一次书面作业"]
    key = "_456_1|作业|_123_1"
    copied = client.append_blocks(page_id, [
        {k: deepcopy(v) for k, v in block.items() if k != "id"}
        for block in blocks[1:]
    ])
    state[key]["owned_blocks"].update(_source_block_state(copied))
    state[key]["layout"] = 3
    state[key]["legacy"] = True
    client.append_blocks(page_id, [paragraph("我自己补充的交作业提醒")])

    migrated = sync(client, state, "交 PDF", "10月1日")
    texts = [visible(block) for block in migrated]
    assert texts.count("要交什么：交 PDF") == 1
    assert texts.count("我自己补充的交作业提醒") == 1
    assert sum("旧版同步记录" in text for text in texts) == 1
    assert state[key]["layout"] == 4

    unchanged = sync(client, state, "交 PDF", "10月1日")
    assert [block["id"] for block in unchanged] == [block["id"] for block in migrated]


def test_layout_three_migration_preserves_student_edit_to_owned_body():
    client = CardClient()
    state = {}
    blocks = sync(client, state, "交 PDF", "10月1日")
    state["_456_1|作业|_123_1"]["layout"] = 3
    client.update_paragraph(blocks[1]["id"], "我的手写核对事项")

    migrated = sync(client, state, "交 PDF", "10月1日")
    texts = [visible(block) for block in migrated]
    assert texts.count("要交什么：交 PDF") == 1
    assert texts.count("我的手写核对事项") == 1
    assert texts.index("我的手写核对事项") > next(
        i for i, text in enumerate(texts) if "旧版同步记录" in text)


@pytest.mark.parametrize("failure", ["append", "archive"])
def test_interrupted_layout_migration_recovers_without_duplicate_or_lost_student_note(
        tmp_path, failure):
    client = InterruptedCardClient()
    state = {}
    sync(client, state, "交 PDF", "10月1日")
    key = "_456_1|作业|_123_1"
    page_id = client.titles["第一次书面作业"]
    state[key]["layout"] = 3
    client.append_blocks(page_id, [paragraph("我自己写的注意事项")])
    _save_source_state(tmp_path, state)

    if failure == "append":
        client.fail_after_append = True
    else:
        client.fail_after_archive = True
    with pytest.raises(RuntimeError):
        sync(client, state, "交 PDF", "10月1日", tmp_path)

    recovered_state = _source_state(tmp_path)
    assert recovered_state[key]["pending"]["fresh_signatures"]
    if failure == "archive":
        assert recovered_state[key]["pending"]["created_blocks"]
    migrated = sync(client, recovered_state, "交 PDF", "10月1日", tmp_path)
    texts = [visible(block) for block in migrated]
    assert texts.count("要交什么：交 PDF") == 1
    assert texts.count("我自己写的注意事项") == 1
    assert sum("旧版同步记录" in text for text in texts) == 1
    assert "pending" not in recovered_state[key]
    _save_source_state(tmp_path, recovered_state)
    again = sync(client, _source_state(tmp_path), "交 PDF", "10月1日", tmp_path)
    assert [block["id"] for block in again] == [block["id"] for block in migrated]


def test_recovery_preserves_edited_inflight_block(tmp_path):
    client = InterruptedCardClient()
    state = {}
    sync(client, state, "交 PDF", "10月1日")
    state["_456_1|作业|_123_1"]["layout"] = 3
    _save_source_state(tmp_path, state)
    client.fail_after_append = True
    with pytest.raises(RuntimeError):
        sync(client, state, "交 PDF", "10月1日", tmp_path)
    page_id = client.titles["第一次书面作业"]
    client.update_paragraph(client.pages[page_id][2]["id"], "我手动补充的截止提醒")

    migrated = sync(client, _source_state(tmp_path), "交 PDF", "10月1日", tmp_path)
    texts = [visible(block) for block in migrated]
    assert texts.count("要交什么：交 PDF") == 1
    assert texts.count("我手动补充的截止提醒") == 1


def test_full_page_append_response_tracks_only_new_blocks_and_recovers(tmp_path):
    client = FullPageAppendResponseClient()
    state = {}
    sync(client, state, "交 PDF", "10月1日")
    key = "_456_1|作业|_123_1"
    page_id = client.titles["第一次书面作业"]
    old_ids = {block["id"] for block in client.pages[page_id]}
    state[key]["layout"] = 3
    _save_source_state(tmp_path, state)

    client.fail_next_update = True
    with pytest.raises(RuntimeError):
        sync(client, state, "交 PDF", "10月1日", tmp_path)
    pending = _source_state(tmp_path)[key]["pending"]
    assert len(pending["created_blocks"]) == len(pending["fresh_signatures"])
    assert not old_ids.intersection(pending["created_blocks"])

    recovered = _source_state(tmp_path)
    blocks = sync(client, recovered, "交 PDF", "10月1日", tmp_path)
    assert [visible(block) for block in blocks].count("要交什么：交 PDF") == 1
    assert set(recovered[key]["owned_blocks"]) == {block["id"] for block in blocks}


def test_assignment_section_surfaces_nearest_formal_due_in_beijing_without_guessing():
    client = CardClient()
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    item = {
        "synced_at": "2026-09-28T12:00:00+00:00",
        "synced_at_label": "2026-09-28 20:00 北京时间",
        "assignments_status": "available",
        "assignments": [
            {"title": "已过期", "due_at": "2026-09-27T12:00:00Z"},
            {"title": "未公布日期的随堂测试", "due_at": ""},
            {"title": "无时区的日期不可推断", "due_at": "2026-09-29 12:00"},
            {"title": "第一次书面作业", "due_at": "2026-10-01T15:30:00.000Z"},
            {"title": "第二次书面作业", "due_at": "2026-10-02T15:30:00.000Z"},
        ],
        "announcement_tasks": [],
        "handbook_reviews": [{"title": "手册", "time_preview": [
            {"page": 8, "text": "12月28日24点前交报告"}]}],
    }
    _sync_source_status(client, "section", "作业", item, 6, now=now)
    status = visible(client.pages["section"][0])
    assert "5 项正式作业" in status
    assert "另有 1 份课程手册待核对" in status
    assert "最近已知正式作业截止：2026-10-01 23:30 北京时间 · 第一次书面作业" in status
    assert "未公布日期" not in status
    assert "12月28日" not in status
    assert "无时区" not in status

    item["assignments_status"] = "partial"
    _sync_source_status(client, "section", "作业", item, 6, now=now)
    assert "来源读取不完整" in visible(client.pages["section"][0])
    assert "最近已知正式作业截止" not in visible(client.pages["section"][0])


def test_assignment_due_summary_returns_empty_for_unknown_or_past_dates():
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert _next_formal_assignment_due({"assignments": [
        {"title": "未知", "due_at": ""},
        {"title": "过期", "due_at": "2026-09-27T00:00:00Z"},
        {"title": "本地日期无时区", "due_at": "2026-10-01 23:30"},
    ]}, now) == ""


def test_teacher_written_deadline_is_visible_without_claiming_system_due():
    item = {
        "title": "Lab1发布", "source": "content-tree", "content_id": "_1717599_1",
        "instructions": "作业 DDL：2026/10/25（周日）23:59，逾期扣分",
        "due_at": "", "due_at_label": "",
        "teacher_deadline_quote": "2026/10/25（周日）23:59",
    }
    texts = [visible(block) for block in _item_blocks("_12_1", "作业", item, "2026-09-29")]
    assert "老师原文截止线索 2026/10/25（周日）23:59" in texts[0]
    assert any("教学网未给独立截止字段" in text for text in texts)
    client = CardClient()
    course = {
        "assignments_status": "available", "synced_at": "2026-09-29T00:00:00Z",
        "assignments": [item], "announcement_tasks": [], "handbook_reviews": [],
    }
    _sync_source_status(client, "section", "作业", course, 1)
    status = visible(client.pages["section"][0])
    assert "Lab1发布 · 2026/10/25（周日）23:59" in status
    assert "系统无独立截止字段" in status
    assert "最近已知正式作业截止" not in status


def test_formal_card_links_teacher_starter_resources_and_rejects_token_url():
    item = {
        "title": "Lab1发布", "source": "content-tree", "content_id": "_1717599_1",
        "source_links": [
            {"label": "Lab 1 说明", "url": "https://edu.n2sys.cn/"},
            {"label": "GitHub 模板", "url": "https://github.com/N2Sys-EDU/2026-lab1-myFTP-Template"},
            {"label": "含令牌", "url": "https://example.com/?token=secret"},
        ],
    }
    blocks = _item_blocks("_12_1", "作业", item, "2026-09-29")
    links = [block["paragraph"]["rich_text"][0]["text"]["link"]["url"]
             for block in blocks if "老师原页面链接" in visible(block)]
    assert links == ["https://edu.n2sys.cn/",
                     "https://github.com/N2Sys-EDU/2026-lab1-myFTP-Template"]
