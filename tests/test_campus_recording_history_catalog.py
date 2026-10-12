"""Read-only history is metadata, never current jobs or publication authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket

import pytest

from pku_sync import catalog_snapshots, recording_history
from pku_sync.panel import campus_catalog
from pku_sync.panel import notion_workspace
from pku_sync.models import Recording
from pku_sync.pipeline import RecordingJob


SOURCE = "pku:recording:" + "a" * 32
ORIGINAL = {"source_id": SOURCE, "title": "原历史课",
            "recorded_at": "2026-09-01 08:00:00", "teacher": "原教师",
            "duration_seconds": 0}


@pytest.fixture
def catalog_view(tmp_path, monkeypatch):
    metadata = {"read_only": False, "catalog_revision": 8}
    envelope = {"revision": 8, "snapshot": {"course_id": "_101508_1"}}
    history = {"current_revision": 8, "display_read_only": True,
               "projection_fresh": False, "recordings": [{
                   "source_id": SOURCE, "source": deepcopy(ORIGINAL),
                   "original_revision": 7, "accepted_revision": 8}]}
    calls = []
    monkeypatch.setattr(campus_catalog, '_courses', lambda root: [(tmp_path, '_101508_1', '课程', metadata)])
    monkeypatch.setattr(campus_catalog, 'collect_jobs', lambda *args: [])
    monkeypatch.setattr(campus_catalog, '_official_index', lambda *args: args[-1])
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: envelope)
    def read(root, course_id, current):
        calls.append((course_id, current))
        return deepcopy(history)
    monkeypatch.setattr(recording_history, 'read_cache', read)
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: pytest.fail('Catalog history must not make network requests'))
    return tmp_path, metadata, envelope, history, calls


def test_verified_history_preserves_original_metadata_without_current_lessons_or_links(catalog_view):
    root, _, envelope, _, calls = catalog_view
    course = campus_catalog.campus_course(root, '_101508_1')
    assert course['lessons'] == []
    row, = course['historical_lessons']
    assert row['source'] == ORIGINAL
    assert row['title'] == ORIGINAL['title'] and row['recorded_at'] == ORIGINAL['recorded_at']
    assert row['course_id'] == '_101508_1' and row['source_id'] == SOURCE
    assert row['historical'] is True and row['read_only'] is True
    assert row['processing_allowed'] is False and row['history_current_revision'] == 8
    assert row['notion_lecture_url'] == ''
    assert not any(key in row for key in ('play_url', 'publish_ready', 'summary_acceptance', 'id', 'video_url'))
    assert calls == [('_101508_1', envelope)]
    assert campus_catalog.campus_courses(root)['courses'][0]['recordings'] == 0


@pytest.mark.parametrize('result', [None, {"recordings": [], "display_read_only": True}])
def test_missing_invalid_or_empty_history_preserves_original_response_layout(catalog_view, monkeypatch, result):
    root, _, _, _, _ = catalog_view
    monkeypatch.setattr(recording_history, 'read_cache', lambda *args: result)
    assert 'historical_lessons' not in campus_catalog.campus_course(root, '_101508_1')


def test_no_canonical_snapshot_does_not_consume_history_cache(catalog_view, monkeypatch):
    root, _, _, _, calls = catalog_view
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: None)
    assert 'historical_lessons' not in campus_catalog.campus_course(root, '_101508_1')
    assert calls == []


def test_changed_current_revision_during_detail_read_does_not_trust_history(catalog_view):
    root, metadata, _, _, calls = catalog_view
    metadata['catalog_revision'] = 7
    assert 'historical_lessons' not in campus_catalog.campus_course(root, '_101508_1')
    assert calls == []


def test_read_only_current_does_not_offer_a_trusted_history_link(catalog_view):
    root, metadata, _, _, _ = catalog_view
    metadata['read_only'] = True
    row, = campus_catalog.campus_course(root, '_101508_1')['historical_lessons']
    assert row['notion_lecture_url'] == ''
    assert row['processing_allowed'] is False


def test_only_strong_lookup_id_becomes_history_navigation(catalog_view, monkeypatch):
    root, metadata, _, history, _ = catalog_view
    metadata['synced_at'] = datetime.now(timezone.utc).isoformat()
    calls = []
    def lookup(folder, course_id, record, cached):
        calls.append((course_id, record, cached))
        return {'id': '12345678-1234-4234-8234-123456789abc', 'url': 'https://untrusted.example/'}
    monkeypatch.setattr(notion_workspace, 'historical_lecture', lookup)
    row, = campus_catalog.campus_course(root, '_101508_1')['historical_lessons']
    assert row['notion_lecture_url'] == 'https://www.notion.so/12345678123442348234123456789abc'
    assert calls == [('_101508_1', history['recordings'][0], history)]


@pytest.mark.parametrize('age', [None, timedelta(days=2), -timedelta(days=2)])
def test_unknown_or_expired_current_keeps_only_inert_history_text(catalog_view, monkeypatch, age):
    root, metadata, _, _, _ = catalog_view
    if age is not None:
        metadata['synced_at'] = (datetime.now(timezone.utc) - age).isoformat()
    monkeypatch.setattr(notion_workspace, 'historical_lecture', lambda *args: pytest.fail('Invalid current must not offer a trusted lookup'))
    row, = campus_catalog.campus_course(root, '_101508_1')['historical_lessons']
    assert row['source'] == ORIGINAL and row['notion_lecture_url'] == ''
    assert row['processing_allowed'] is False


def test_history_does_not_replace_or_increment_one_current_lesson(catalog_view, monkeypatch):
    root, _, _, _, _ = catalog_view
    recording = Recording(course_id='_101508_1', title='当前讲次', recorded_at='2026-09-10 08:00:00')
    job = RecordingJob(course_name='课程', course_dir=root, recording=recording)
    monkeypatch.setattr(campus_catalog, 'collect_jobs', lambda *args: [job])
    course = campus_catalog.campus_course(root, '_101508_1')
    assert len(course['lessons']) == 1 and course['lessons'][0]['title'] == '当前讲次'
    assert course['lessons'][0]['source_id'] != SOURCE
    assert len(course['historical_lessons']) == 1
    assert campus_catalog.campus_courses(root)['courses'][0]['recordings'] == 1
