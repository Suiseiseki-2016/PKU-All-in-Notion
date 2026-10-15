"""Developer coverage regression, never a native E2E verdict."""
from contextlib import nullcontext
from types import SimpleNamespace
from copy import deepcopy
import pytest

from product_e2e import catalog_journeys as journeys


def test_restart_alone_cannot_pass_compound_double_click_assertion(monkeypatch):
    from scripts import product_e2e_gate
    monkeypatch.setitem(product_e2e_gate.FAULT_VARIANTS,'JOURNEY-06',['kill-process-and-restart'])
    checks=[]
    ui=SimpleNamespace(request_read=lambda path:{'courses':[{'id':'_123_1'}]},screenshot=lambda *a:None)
    run=SimpleNamespace(mode='fault',session=lambda:ui,need=lambda key:'_123_1',config={},
        scenario={'id':'JOURNEY-06','assertions':[],'assertions_by_mode':{'fault':[]}},
        check=lambda key,passed,*args:checks.append((key,passed)),
        fault=lambda variant:nullcontext(),crash_and_restart=lambda name:ui,
        fault_check=lambda *a:None,observe=lambda:{'failure_ids':[]})
    monkeypatch.setattr(journeys,'_catalog_scope_preflight',lambda *a:None)
    monkeypatch.setattr(journeys,'_effects',lambda *a:{'no-change':True})
    monkeypatch.setattr(journeys,'_file_hashes',lambda *a,**kw:{'student.md':'developer-fixture'})
    monkeypatch.setattr(journeys,'_action',lambda *a:None)
    monkeypatch.setattr(journeys,'_open',lambda *a:{'id':'_123_1','lessons':[]})
    monkeypatch.setattr(journeys,'_cancel_refresh',lambda *a:None)
    monkeypatch.setattr(journeys,'_start_refresh',lambda *a:{})
    monkeypatch.setattr(journeys,'_refresh',lambda *a:{'state':'done'})
    monkeypatch.setattr(journeys,'_resilience_double_refresh',
        lambda *a:{'verified':False,'task':{'state':'done'}},raising=False)
    journeys.run_resilience(run)
    assert ('interruption-restart-and-double-click-no-duplicate-effect',False) in checks


def observed_input():
    return {'boundary':'owned-installed-webview-input','observed':True,
            'events':[{'type':'click','trusted':True},{'type':'click','trusted':True}]}


@pytest.mark.parametrize('responses', [
    [{'status':200,'body':{'kind':'sync'}}],
    [{'status':200,'body':{'kind':'sync'}},
     {'status':409,'body':{'detail':'已有录音任务正在进行，请等待完成。'}}],
])
def test_only_one_accepted_task_with_two_trusted_clicks_is_valid(responses):
    assert journeys._valid_double_refresh(observed_input(), responses, {'kind':'sync','state':'done'})


@pytest.mark.parametrize('change', ['one-click','untrusted','external','unobserved',
    'two-accepted','zero-accepted','wrong-kind','other-409','three-responses','running','other-task'])
def test_double_click_evidence_rejects_missing_input_or_duplicate_acceptance(change):
    observation = observed_input()
    responses = [{'status':200,'body':{'kind':'sync'}}]
    task = {'kind':'sync','state':'done'}
    if change == 'one-click': observation['events'].pop()
    elif change == 'untrusted': observation['events'][1]['trusted'] = False
    elif change == 'external': observation['boundary'] = 'external-browser'
    elif change == 'unobserved': observation['observed'] = False
    elif change == 'two-accepted': responses.append(deepcopy(responses[0]))
    elif change == 'zero-accepted': responses.clear()
    elif change == 'wrong-kind': responses[0]['body']['kind'] = 'other'
    elif change == 'other-409': responses.append({'status':409,'body':{'detail':'unrelated failure'}})
    elif change == 'three-responses': responses.extend([{'status':409,'body':{'detail':'已有录音任务正在进行，请等待完成。'}}]*2)
    elif change == 'running': task['state'] = 'running'
    elif change == 'other-task': task['kind'] = 'other'
    assert not journeys._valid_double_refresh(observation, responses, task)


def test_single_course_deadline_includes_slow_terminal_read(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(journeys.time, 'monotonic', lambda: clock[0])
    def read(path):
        clock[0] += 91
        return {'kind': 'sync', 'course_id': '_123_1', 'state': 'done'}
    ui = SimpleNamespace(request_read=read, wait=lambda predicate, **kwargs: predicate())
    with pytest.raises(AssertionError, match='deadline'):
        journeys._task(ui, timeout=90)


def background():
    return {'kind': 'sync', 'scope': 'background', 'id': 'actual-task', 'state': 'done',
        'course_results': [{'course_id': '_123_1', 'state': 'done', 'attempts': 1,
            'retryable': False, 'metadata_state': 'confirmed', 'projection_state': 'confirmed',
            'summary_created': 0, 'summary_existing': 0, 'error': '', 'failure_id': ''}]}


def test_background_completed_metadata_has_independent_per_course_states():
    assert journeys._background_result_valid(background(), ['_123_1'])


@pytest.mark.parametrize('change', ['empty', 'wrong-course', 'duplicate', 'pending',
    'unconfirmed', 'missing-projection', 'four-attempts', 'zero-attempts', 'implicit-summary', 'false-done'])
def test_background_rejects_omitted_unconfirmed_unbounded_or_false_completion(change):
    value = background()
    row = value['course_results'][0]
    if change == 'empty': value['course_results'] = []
    elif change == 'wrong-course': row['course_id'] = '_999_1'
    elif change == 'duplicate': value['course_results'].append(deepcopy(row))
    elif change == 'pending': row['state'] = 'pending'
    elif change == 'unconfirmed': row['metadata_state'] = 'unconfirmed'
    elif change == 'missing-projection': row['projection_state'] = 'not_read'
    elif change == 'four-attempts': row['attempts'] = 4
    elif change == 'zero-attempts': row['attempts'] = 0
    elif change == 'implicit-summary': row['summary_created'] = 1
    elif change == 'false-done': row.update(state='failed', error='检查连接后重试')
    assert not journeys._background_result_valid(value, ['_123_1'])


def test_background_partial_is_explicit_failure_and_never_claimed_complete():
    value = background()
    value['state'] = 'partial'
    value['course_results'][0].update(state='failed', metadata_state='unconfirmed',
        projection_state='not_read', error='检查连接后重试')
    assert journeys._background_result_valid(value, ['_123_1'])
    value['course_results'][0]['error'] = ''
    assert not journeys._background_result_valid(value, ['_123_1'])


@pytest.mark.parametrize('invalid', ['early', 'missing-delay', 'nan-delay', 'inf-start', 'unbounded'])
def test_background_rejects_missing_real_throttle_or_infinite_request_evidence(invalid):
    events = [{'boundary': 'notion', 'upstream_status': 429, 'retry_after_seconds': 39,
               'started_monotonic': 10, 'elapsed_seconds': 1},
              {'boundary': 'notion', 'upstream_status': 200, 'started_monotonic': 50}]
    assert journeys._background_network_valid(events, 1)
    if invalid == 'early': events[1]['started_monotonic'] = 49
    elif invalid == 'missing-delay': events[0].pop('retry_after_seconds')
    elif invalid == 'nan-delay': events[0]['retry_after_seconds'] = float('nan')
    elif invalid == 'inf-start': events[1]['started_monotonic'] = float('inf')
    elif invalid == 'unbounded': events.extend({'boundary':'account'} for _ in range(journeys._BACKGROUND_SERVICE_REQUESTS_PER_COURSE))
    assert not journeys._background_network_valid(events, 1)
