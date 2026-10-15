from types import SimpleNamespace

import pytest

from pku_sync.models import Recording
from pku_sync.panel.summary_acceptance import accept, status


def job(tmp_path):
    (tmp_path / "notes.md").write_text("已审阅的课堂总结", "utf-8")
    return SimpleNamespace(directory=tmp_path, recording=Recording(
        course_id="_101545_1", title="第五讲", recorded_at="2026-09-29 13:00:00"))


def test_acceptance_requires_explicit_review_of_current_bytes(tmp_path):
    current = job(tmp_path)
    digest = status(current)["notes_sha256"]
    assert not status(current)["accepted"]
    with pytest.raises(ValueError, match="审阅"):
        accept(current, notes_sha256=digest, confirmed=False)
    with pytest.raises(ValueError, match="变化"):
        accept(current, notes_sha256="stale", confirmed=True)
    assert not (tmp_path / "summary-acceptance.json").exists()
    assert accept(current, notes_sha256=digest, confirmed=True)["accepted"]
    current.recording.title = "教学网修订标题"
    assert status(current)["accepted"]
    (tmp_path / "notes.md").write_text("重新生成的总结", "utf-8")
    assert not status(current)["accepted"]


def test_acceptance_cannot_be_reused_for_another_official_lecture(tmp_path):
    current = job(tmp_path)
    accept(current, notes_sha256=status(current)["notes_sha256"], confirmed=True)
    current.recording.recorded_at = "2026-09-22 13:00:00"
    assert not status(current)["accepted"]


def test_unaccepted_summary_never_enters_real_quiz_sources(tmp_path, monkeypatch):
    client = NotesClient()
    current = published_job(tmp_path, client)
    provider, projection, key = current_quiz_sources(current, client, monkeypatch)
    from pku_sync.panel.quiz_contract import OrganizeBlocked
    with pytest.raises(OrganizeBlocked, match='已验收'):
        provider.resolve(current.recording.course_id, [key])


class NotesClient:
    def __init__(self):
        from pku_sync.notion import markdown_to_blocks
        self.rows = markdown_to_blocks("已审阅的课堂总结")
        self.parent = "lecture"
        self.drop = False

    def get_page(self, page_id):
        return {"id": page_id, "parent": {"page_id": self.parent}}

    def list_children(self, page_id):
        return self.rows

    def append_blocks(self, page_id, rows, **kwargs):
        if not self.drop:
            self.rows.extend(rows)


def published_job(tmp_path, client):
    import json
    from pku_sync.panel.summary_acceptance import document_fingerprint
    current = job(tmp_path)
    value = {"note_id": "note", "lecture_id": "lecture",
             "source_id": status(current)["source_id"],
             "notes_sha256": status(current)["notes_sha256"],
             "remote_sha256": document_fingerprint(client, "note")}
    (tmp_path / "published-note-version.json").write_text(json.dumps(value), "utf-8")
    return current


@pytest.mark.parametrize("change", ["text", "parent", "lost_write", "local"])
def test_remote_acceptance_refuses_changed_or_unverified_notes(tmp_path, change):
    from pku_sync.panel.summary_acceptance import accept_published
    client = NotesClient()
    current = published_job(tmp_path, client)
    digest = status(current)["notes_sha256"]
    if change == "text":
        client.rows[0]["paragraph"]["rich_text"][0]["text"]["content"] = "已修改"
    elif change == "parent":
        client.parent = "another-lecture"
    elif change == "lost_write":
        client.drop = True
    else:
        (tmp_path / "notes.md").write_text("新本地版本", "utf-8")
    with pytest.raises(ValueError):
        accept_published(current, client=client, notes_sha256=digest, confirmed=True)
    assert not status(current)["accepted"]


def test_remote_acceptance_is_readable_on_other_device_and_invalidated_by_edit(tmp_path):
    from pku_sync.panel.summary_acceptance import accept_published, published_status
    client = NotesClient()
    current = published_job(tmp_path, client)
    result = accept_published(current, client=client, notes_sha256=status(current)["notes_sha256"], confirmed=True)
    assert result["accepted"]
    assert published_status(client, "note", result["source_id"])["accepted"]
    assert not published_status(client, "note", "another-source")["accepted"]
    client.rows[0]["paragraph"]["rich_text"][0]["annotations"] = {"bold": True}
    assert not published_status(client, "note", result["source_id"])["accepted"]


def test_production_quiz_source_rechecks_remote_acceptance_before_reusing_notes(tmp_path, monkeypatch):
    from pku_sync.panel.summary_acceptance import accept_published
    from pku_sync.panel.quiz_contract import OrganizeBlocked
    client = NotesClient()
    current = published_job(tmp_path, client)
    provider, projection, key = current_quiz_sources(current, client, monkeypatch)
    with pytest.raises(OrganizeBlocked): provider.resolve(current.recording.course_id, [key])
    projection['summary_acceptance'] = accept_published(current, client=client,
        notes_sha256=status(current)['notes_sha256'], confirmed=True)
    assert len(provider.resolve(current.recording.course_id, [key])['notes']) == 1
    client.rows[0]['paragraph']['rich_text'][0]['text']['content'] = 'Edited after acceptance'
    with pytest.raises(OrganizeBlocked): provider.resolve(current.recording.course_id, [key])


def test_edit_during_acceptance_readback_does_not_mark_local_summary_accepted(tmp_path):
    from pku_sync.panel.summary_acceptance import accept_published
    class EditingClient(NotesClient):
        def append_blocks(self, page_id, rows, **kwargs):
            super().append_blocks(page_id, rows, **kwargs)
            self.rows[0]["paragraph"]["rich_text"][0]["text"]["content"] = "同时修改"
    client = EditingClient()
    current = published_job(tmp_path, client)
    with pytest.raises(ValueError, match="验证"):
        accept_published(current, client=client, notes_sha256=status(current)["notes_sha256"], confirmed=True)
    assert not status(current)["accepted"]


def test_nested_note_content_and_mention_identity_are_part_of_acceptance_version():
    from pku_sync.panel.summary_acceptance import document_fingerprint
    rows = {"note": [{"id": "toggle", "type": "toggle", "has_children": True, "toggle": {"rich_text": []}}],
            "toggle": [{"type": "paragraph", "paragraph": {"rich_text": [
                {"type": "mention", "mention": {"type": "page", "page": {"id": "original"}}}]}}]}
    client = SimpleNamespace(list_children=lambda page_id: rows[page_id])
    original = document_fingerprint(client, "note")
    rows["note"][0]["last_edited_time"] = "ignored API metadata"
    assert document_fingerprint(client, "note") == original
    rows["toggle"][0]["paragraph"]["rich_text"][0]["mention"]["page"]["id"] = "another"
    assert document_fingerprint(client, "note") != original


def test_internal_file_access_signature_is_not_a_new_note_version():
    from pku_sync.panel.summary_acceptance import content_fingerprint
    rows = [{"type": "image", "image": {"type": "file", "file": {
        "url": "https://secure.notion-static.com/lecture/image.png?signature=old", "expiry_time": "old"}}}]
    original = content_fingerprint(rows)
    rows[0]["image"]["file"]["url"] = "https://secure.notion-static.com/lecture/image.png?signature=new"
    rows[0]["image"]["file"]["expiry_time"] = "new"
    assert content_fingerprint(rows) == original
    rows[0]["image"]["file"]["url"] = "https://secure.notion-static.com/another/image.png?signature=new"
    assert content_fingerprint(rows) != original


def current_quiz_sources(current, client, monkeypatch):
    from contextlib import nullcontext
    from pku_sync.panel import campus_quiz
    key = status(current)['source_id']
    projection = {'id': 'lecture', 'source_id': key, 'summary_acceptance': {}}
    provider = campus_quiz.CampusQuizSources(SimpleNamespace(notion_token='fixture'))
    scope = {'workspace_id': 'home', 'course_id': current.recording.course_id,
             'course_title': 'Course', 'quiz_center_id': 'center',
             'official': {'lessons': [{'source_id': key, 'title': 'Lecture'}]},
             'projection': {'lectures': {'lecture': projection}}}
    monkeypatch.setattr(provider, 'course', lambda _: scope)
    monkeypatch.setattr(campus_quiz, 'NotionClient', lambda _: nullcontext(client))
    return provider, projection, key
