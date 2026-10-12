"""Native course IDs, never refreshed titles, choose a local write target."""
import json

import pytest

from pku_sync import sync, catalog_snapshots as catalog
from pku_sync.models import Course
from pku_sync.panel import recordings_api
from tests.test_account_catalog import snapshot, settings
from tests.test_catalog_authority_e2e import account_service, fake_portal, settings as desktop_settings


CID = '_101545_1'


def local_course(root, folder='old-folder', cid=CID):
    path = root / folder
    (path / 'recordings').mkdir(parents=True)
    (path / 'course.json').write_text(json.dumps({'course_id': cid, 'name': '原课程名称'}), 'utf-8')
    (path / 'recordings/index.json').write_text('[]', 'utf-8')
    (path / 'recordings/video.mp4').write_bytes(b'local-fixture')
    return path


def fake_sync(monkeypatch):
    monkeypatch.setattr(sync, 'fetch_announcements', lambda *a: [])
    monkeypatch.setattr(sync, 'walk_materials', lambda *a: ([], []))
    monkeypatch.setattr(sync, 'list_recordings', lambda *a: [])
    monkeypatch.setattr(sync, 'resolve_media', lambda *a: pytest.fail('no media resolution'))


def test_cli_refresh_reuses_course_id_directory_after_title_change(tmp_path, monkeypatch):
    old = local_course(tmp_path)
    fake_sync(monkeypatch)
    course = Course(course_id=CID, name='新课程名称')
    result = sync.sync_course(object(), course, tmp_path, [], resolve_recordings=False)
    assert result.ok
    assert json.loads((old / 'course.json').read_text('utf-8'))['name'] == course.name
    assert (old / 'recordings/video.mp4').read_bytes() == b'local-fixture'
    assert len(list(tmp_path.glob('*/course.json'))) == 1


def test_cli_same_title_does_not_overwrite_other_native_course(tmp_path, monkeypatch):
    old = local_course(tmp_path, folder='同名课程', cid='_other_1')
    original = (old / 'course.json').read_bytes()
    fake_sync(monkeypatch)
    result = sync.sync_course(object(), Course(course_id=CID, name='同名课程'), tmp_path, [], resolve_recordings=False)
    assert result.ok
    assert (old / 'course.json').read_bytes() == original
    assert len(list(tmp_path.glob('*/course.json'))) == 2


def test_cli_duplicate_native_course_directories_preserve_both(tmp_path, monkeypatch):
    originals = [local_course(tmp_path, folder=name) / 'course.json' for name in ['first', 'second']]
    before = [path.read_bytes() for path in originals]
    fake_sync(monkeypatch)
    result = sync.sync_course(object(), Course(course_id=CID, name='新课程名称'), tmp_path, [], resolve_recordings=False)
    assert not result.ok
    assert [path.read_bytes() for path in originals] == before
    assert len(list(tmp_path.glob('*/course.json'))) == 2


def test_cli_batch_conflict_prevents_any_other_course_write(tmp_path, monkeypatch):
    local_course(tmp_path, 'first')
    local_course(tmp_path, 'second')
    fake_sync(monkeypatch)
    monkeypatch.setattr(sync, 'fetch_deadlines', lambda *a: {})
    courses = [Course(course_id='_another_1', name='另一门课'), Course(course_id=CID, name='新课程名称')]
    reports = sync.sync_all(object(), courses, tmp_path, resolve_recordings=False)
    assert all(not report.ok for report in reports)
    assert len(list(tmp_path.glob('*/course.json'))) == 2


def test_long_same_title_courses_keep_distinct_native_directory_targets(tmp_path):
    from pku_sync.course_storage import plan_course_directories
    title = 'A' * 150
    targets = plan_course_directories(tmp_path, [Course(course_id=CID, name=title),
                                              Course(course_id='_another_1', name=title)])
    assert targets[CID] != targets['_another_1']


def test_unknown_existing_directory_is_preserved_and_not_adopted(tmp_path, monkeypatch):
    unknown = tmp_path / '新课程名称'
    unknown.mkdir()
    notes = unknown / 'notes.md'
    notes.write_text('未核对来源的原笔记', 'utf-8')
    fake_sync(monkeypatch)
    result = sync.sync_course(object(), Course(course_id=CID, name=unknown.name), tmp_path, [], resolve_recordings=False)
    assert result.ok
    assert notes.read_text('utf-8') == '未核对来源的原笔记'
    assert not (unknown / 'course.json').exists()


def fake_pull(monkeypatch, envelopes):
    monkeypatch.setattr(catalog, 'activate_account', lambda settings: None)
    monkeypatch.setattr(catalog, '_call', lambda *a, **kw: {'courses': envelopes})


def test_account_pull_rejects_duplicate_local_course_before_advancing_cache(tmp_path, monkeypatch):
    local_course(tmp_path, 'first')
    local_course(tmp_path, 'second')
    initial = {'revision': 1, 'snapshot': snapshot(count=0)}
    catalog.save_cache(tmp_path, initial)
    fake_pull(monkeypatch, [{'revision': 2, 'snapshot': snapshot(count=0)}])
    with pytest.raises(ValueError, match='目录'):
        catalog.pull(settings(tmp_path))
    assert catalog.cached(tmp_path, CID) == initial


def test_account_pull_preflights_all_directories_before_mutating_any_cache(tmp_path, monkeypatch):
    local_course(tmp_path, 'first')
    local_course(tmp_path, 'second')
    other = snapshot(count=0)
    other['course_id'] = other['course']['course_id'] = '_another_1'
    fake_pull(monkeypatch, [{'revision': 1, 'snapshot': other}, {'revision': 2, 'snapshot': snapshot(count=0)}])
    with pytest.raises(ValueError, match='目录'):
        catalog.pull(settings(tmp_path))
    assert catalog.cached(tmp_path, '_another_1') is None
    assert len(list(tmp_path.glob('*/course.json'))) == 2


def test_desktop_refresh_course_rename_preserves_single_directory(tmp_path, monkeypatch, account_service):
    from pku_sync import discover
    fake_portal(monkeypatch)
    s = desktop_settings(tmp_path)
    recordings_api.sync_recording_index(s, lambda _: None)
    old = next(tmp_path.glob('*/course.json')).parent
    monkeypatch.setattr(discover, 'discover_courses', lambda *a: [Course(course_id=CID, name='新课程名称')])
    recordings_api.sync_recording_index(s, lambda _: None)
    assert len(list(tmp_path.glob('*/course.json'))) == 1
    assert json.loads((old / 'course.json').read_text('utf-8'))['name'] == '新课程名称'
    assert catalog.current(s, CID)['revision'] == 2


def test_desktop_duplicate_course_directory_does_not_publish_new_shared_revision(tmp_path, monkeypatch, account_service):
    fake_portal(monkeypatch)
    s = desktop_settings(tmp_path)
    recordings_api.sync_recording_index(s, lambda _: None)
    initial = catalog.current(s, CID)
    local_course(tmp_path, 'duplicate')
    with pytest.raises(ValueError, match='目录'):
        recordings_api.sync_recording_index(s, lambda _: None)
    assert catalog.current(s, CID) == initial
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path)
