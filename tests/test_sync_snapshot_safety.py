"""The legacy CLI must also preserve full snapshots on partial portal reads."""

import pytest

from pku_sync import sync
from pku_sync.models import Course, Recording


@pytest.mark.parametrize("failed_section", ["materials", "recordings", "announcements", "calendar"])
def test_cli_partial_read_does_not_touch_complete_snapshot(tmp_path, monkeypatch, failed_section):
    course = Course(course_id="_101545_1", name="发展心理学")
    folder = tmp_path / course.directory
    (folder / "recordings").mkdir(parents=True)
    originals = {
        folder / "course.json": '{"course_id":"_101545_1","name":"发展心理学","synced_at":"old"}',
        folder / "assignments.md": "原有正式作业要求",
        folder / "recordings" / "index.json": '[{"recorded_at":"2026-09-29 13:00:00"}]',
    }
    for path, text in originals.items():
        path.write_text(text, "utf-8")
    def read(section, result):
        if section == failed_section:
            raise ValueError("mock incomplete portal read")
        return result
    monkeypatch.setattr(sync, "fetch_announcements", lambda *a: read("announcements", []))
    monkeypatch.setattr(sync, "walk_materials", lambda *a: read("materials", ([], [])))
    monkeypatch.setattr(sync, "list_recordings", lambda *a: read("recordings", [
        Recording(course_id=course.course_id, recorded_at="2026-09-22 13:00:00", title="旧列表")]))
    monkeypatch.setattr(sync, "resolve_media", lambda *a: pytest.fail("must not resolve or download media"))
    result = sync.sync_course(object(), course, tmp_path, [], deadlines_complete=failed_section != "calendar")
    assert not result.ok
    for path, text in originals.items():
        assert path.read_text("utf-8") == text
    assert not list(tmp_path.rglob("video.mp4"))
