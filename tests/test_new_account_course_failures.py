"""New accounts must survive unavailable or nonstandard individual courses."""
import json
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import auth, catalog_snapshots as catalog, discover, materials, course_data
from pku_sync.config import Settings
from pku_sync.models import Course
from pku_sync.panel import recordings_api
from pku_sync.panel.campus_catalog import campus_courses
from test_selected_course_refresh import configuration, wait_background


def test_nonstandard_rest_entry_does_not_discard_other_courses():
    payload = {'results': [
        {'course': {'id': '_10001_1', 'name': '正常课程', 'courseId': '20261-A'}},
        {'course': {'id': '_10002_1', 'name': '不同格式课程', 'courseId': None, 'term': '2026 秋'}},
        {'course': 'invalid'},
        {'course': {'id': '_10003_1', 'name': '另一门课程', 'courseId': 'MED-A'}},
    ]}
    with httpx.Client(base_url='https://fixture.invalid', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload) if '/learn/api/' in request.url.path else httpx.Response(503)
    )) as client:
        result = discover.discover_courses(client)
    assert {row.course_id for row in result} == {'_10001_1', '_10002_1', '_10003_1'}


def test_automatic_term_filter_keeps_unrecognized_course_codes():
    courses = [Course(course_id='_10001_1', name='本学期', code='20261-A'),
               Course(course_id='_10002_1', name='非标准编号', code='MED-A'),
               Course(course_id='_10003_1', name='旧学期', code='20251-A')]
    settings = Settings(_env_file=None)
    assert [row.course_id for row in discover.select_courses(courses, settings)] == ['_10001_1', '_10002_1']


def test_unexpected_course_error_does_not_abort_other_courses(tmp_path, monkeypatch):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    monkeypatch.setattr(manager, '_background_course_ids', lambda stage: ['broken', 'healthy'])
    calls = []
    def run(identifier):
        calls.append(identifier)
        if identifier == 'broken':
            raise TypeError('private upstream response')
        return {'state': 'done', 'catalog_updated': 1}
    monkeypatch.setattr(manager, '_run_background_course', run)
    manager.start_background_sync()
    result = wait_background(manager)
    assert calls == ['broken', 'healthy']
    assert result['state'] == 'partial'
    assert [row['state'] for row in result['course_results']] == ['failed', 'done']
    assert result['course_results'][0]['failure_id']
    assert 'private' not in json.dumps(result)
    assert manager.settings.pku_username == 'fixture'


def test_first_failed_course_remains_visible_without_claiming_synced(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    course = Course(course_id='_10001_1', name='录像接口不可用的课程')
    monkeypatch.setattr(catalog, 'activate_account', lambda _: None)
    monkeypatch.setattr(catalog, 'begin_observation', lambda *args: None)
    monkeypatch.setattr(auth, 'get_session', lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(discover, 'discover_courses', lambda client: [course])
    monkeypatch.setattr(discover, 'select_courses', lambda rows, _: rows)
    monkeypatch.setattr(materials, 'fetch_deadlines', lambda client: {})
    monkeypatch.setattr(course_data, 'read_course_metadata', lambda *a, **kw: {
        course.course_id: SimpleNamespace(recordings_error=RuntimeError('recording API unavailable'))})
    recordings_api.sync_recording_index(settings, lambda stage: None)
    rows = campus_courses(tmp_path)['courses']
    assert len(rows) == 1 and rows[0]['id'] == course.course_id
    assert rows[0]['read_only'] is True and rows[0]['synced_at'] == ''
    assert catalog.cached(tmp_path, course.course_id) is None
