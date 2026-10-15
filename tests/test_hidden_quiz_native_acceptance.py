"""Exercise the current driver checks, including negative evidence."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from product_e2e import learning_journeys as journeys
from product_e2e import core
from product_e2e.catalog_journeys import _cached_source_content


@pytest.mark.parametrize('fault', ['none', 'visible', 'request', 'job', 'charge'])
@pytest.mark.parametrize('mode', ['live', 'fault'])
def test_hidden_quiz_driver_rejects_visible_requests_and_charges(monkeypatch, fault, mode):
    checks, variants, events = {}, {}, []
    run = SimpleNamespace(
        scenario={'assertions': ['unfinished-quiz-entry-hidden']}, mode=mode,
        network=[], ui_evidence=[], need=lambda key: '_123_1',
        check=lambda key, passed: checks.update({key: passed}),
        fault_check=lambda key, passed: variants.update({key: passed}),
        restart=lambda name: events.append('restart'),
        crash_and_restart=lambda name: events.append('crash'))
    @contextmanager
    def boundary(variant):
        events.append(variant)
        yield {}
    run.fault = boundary
    locator = SimpleNamespace(count=lambda: int(fault == 'visible'))
    state = {'course_open': False}
    def reload():
        state['course_open'] = False
        events.append('reload')
    def wait(predicate):
        assert predicate(), 'Reload resets the SPA to its course list'
    page = SimpleNamespace(reload=reload, locator=lambda selector: locator)
    ui = SimpleNamespace(page=page, wait=wait, visible=lambda selector: state['course_open'],
                         screenshot=lambda name: run.ui_evidence.append(name))
    def opened(*args, **kwargs):
        assert kwargs['center'] == ''
        state['course_open'] = True
        if fault == 'request':
            run.network.append({'path': '/api/campus/courses/_123_1/quizzes'})
        return ui, {}
    monkeypatch.setattr(journeys, '_open_course', opened)
    baseline = {'jobs': [], 'settlements': [], 'charged_points': 0, 'charged_seconds': 0}
    after = {**baseline, 'jobs': [{'operation_id': 'unexpected'}] if fault == 'job' else [],
             'charged_points': int(fault == 'charge')}
    ledgers = iter([baseline, after])
    monkeypatch.setattr(journeys, '_ledger', lambda run: next(ledgers))
    journeys.run_quiz(run)
    assert all(checks.values()) is (fault == 'none')
    assert 'restart' in events
    assert len(run.ui_evidence) == (3 if mode == 'fault' else 2)
    assert variants == ({'kill-process-and-restart': fault != 'visible'} if mode == 'fault' else {})
    if mode == 'fault':
        assert events.count('crash') == 1


@pytest.mark.parametrize('fresh', [False, True])
def test_native_guide_uses_real_cta_only_when_present(fresh):
    state = {'welcome': fresh, 'events': []}
    def visible(selector):
        if selector == '[data-action="welcome-start"]':
            return state['welcome']
        return not state['welcome'] and selector == '.sidebar [data-action="open-dashboard"]'
    def click(selector):
        assert selector == '[data-action="welcome-start"]'
        state['welcome'] = False
        state['events'].append(selector)
    def wait(predicate):
        assert predicate()
    ui = SimpleNamespace(visible=visible, wait=wait, click=click,
                         screenshot=lambda name: state['events'].append(name))
    core.enter_welcome(ui)
    assert state['events'] == (['first-run-welcome', '[data-action="welcome-start"]'] if fresh else [])


def test_seed_copy_pins_only_its_own_data_directory(tmp_path):
    seed = tmp_path / 'seed'
    seed.mkdir()
    original = b'DATA_DIR="some-other-profile/data"\nNOTION_TOKEN=synthetic-private-token\n'
    (seed / '.env').write_bytes(original)
    (seed / 'data').mkdir()
    (seed / 'data/student.md').write_text('actual fixture student content', 'utf-8')
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    run = core.NativeRun({'id': 'JOURNEY-05', 'assertions': []}, 'live', {
        'run_dir': run_dir, 'config': {'test_root': str(tmp_path), 'instances': {
            'primary': {'seed_dir': str(seed), 'app_dir': str(tmp_path / 'unused')}}}})
    destination = Path(run.config['instances']['primary']['app_dir'])
    from dotenv import dotenv_values
    values = dotenv_values(destination / '.env')
    assert Path(values['DATA_DIR']) == destination / 'data'
    assert values['NOTION_TOKEN'] == 'synthetic-private-token'
    assert (destination / 'data/student.md').read_text('utf-8') == 'actual fixture student content'
    assert (seed / '.env').read_bytes() == original


@pytest.mark.parametrize('change', ['coordination', 'missing-lesson', 'changed-content', 'new-field'])
def test_cache_comparison_preserves_all_content_while_status_can_change(change):
    original = {'id': '_123_1', 'read_only': False, 'catalog_revision': 4,
                'lessons': [{'id': 'lesson', 'source_id': 'official', 'title': 'Lecture'}],
                'announcements': [{'id': 'notice', 'body_text': 'official instruction'}]}
    current = deepcopy(original)
    current.update(read_only=True, catalog_revision=None, read_only_message='请稍后重试')
    if change == 'missing-lesson':
        current['lessons'] = []
    if change == 'changed-content':
        current['announcements'][0]['body_text'] = 'changed instruction'
    if change == 'new-field':
        current['unknown-source-content'] = 'must not be ignored'
    assert (_cached_source_content(original) == _cached_source_content(current)) is (change == 'coordination')
