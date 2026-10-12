"""Client-side submission fence failures, simulated account service only."""
from copy import deepcopy
import hashlib

import pytest

from pku_sync import catalog_snapshots as catalog
from pku_sync.config import Settings
from pku_sync.panel.assignment_workflow import _requirements, PreparationError


ASSIGNMENT = {'content_id': '_2_1', 'source_id': 'pku:assignment:_2_1',
              'title': '正式作业', 'source': 'content-tree'}


def setup(monkeypatch, tmp_path):
    from pku_sync.panel.submission_permit import SubmissionPermit
    settings = Settings(_env_file=None, data_dir=tmp_path, pku_username='student')
    assignment = deepcopy(ASSIGNMENT)
    metadata = {'course_id': '_1_1', 'content_id': '_2_1', 'draft_id': 'one', 'state': 'prepared',
                'portal_account_sha256': 'a' * 64, 'sha256': 'b' * 64,
                'requirements_sha256': _requirements(assignment)}
    states, saved = {}, []
    monkeypatch.setattr(catalog, 'current', lambda *args: {'revision': 1, 'snapshot': {
        'sections': {'assignments': {'items': [assignment]}}}})
    def call(settings, method, course, suffix='', body=None):
        if method == 'GET':
            from urllib.parse import urlsplit
            operation = urlsplit(suffix).path.rsplit('/', 1)[1]
            return deepcopy(states[operation])
        op = body['operation_id']
        if suffix.endswith('/submission'):
            if op not in states:
                if any(row['state'] in ('acquired', 'verification_pending') for row in states.values()):
                    raise catalog.CatalogUnavailable('other device pending')
                states[op] = {'operation_id': op, 'revision': body['revision'], 'file_sha256': body['file_sha256'],
                              'permit_id': 'permit', 'state': 'acquired'}
            return deepcopy(states[op])
        if suffix.endswith('/arm'):
            assert states[op]['state'] == 'acquired'
            states[op]['state'] = 'verification_pending'
        elif suffix.endswith('/complete'):
            states[op].update(state='submitted', receipt=body['receipt'])
        elif suffix.endswith('/release'):
            assert states[op]['state'] == 'acquired'
            states[op]['state'] = 'aborted'
            return {'released': True}
        return deepcopy(states[op])
    monkeypatch.setattr(catalog, '_call', call)
    def permit(meta=metadata):
        return SubmissionPermit(settings, meta, assignment, lambda: saved.append(deepcopy(meta)))
    return permit, metadata, states, saved, call


def test_other_device_cannot_post_while_result_is_unknown(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    with permit() as fence:
        fence.arm()
    other = {**metadata, 'draft_id': 'two'}
    other.pop('submission_fence')
    with pytest.raises(PreparationError):
        with permit(other):
            pytest.fail('other device must not reach portal POST')
    assert states['one']['state'] == 'verification_pending'


def test_lost_arm_reply_stops_post_and_keeps_shared_fence(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    def lost_reply(*args, **kwargs):
        result = call(*args, **kwargs)
        if args[3].endswith('/arm'):
            raise catalog.CatalogUnavailable('lost reply after commit')
        return result
    monkeypatch.setattr(catalog, '_call', lost_reply)
    with pytest.raises(PreparationError):
        with permit() as fence:
            fence.arm()
            pytest.fail('a lost arm reply must never authorize POST')
    assert states['one']['state'] == 'verification_pending'
    with pytest.raises(PreparationError):
        with permit():
            pytest.fail('restart must not re-arm a pending request')


def test_prepost_failure_releases_only_unsent_operation(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        with permit():
            raise ValueError('file failed preview hash before POST')
    assert states['one']['state'] == 'aborted'


def test_verified_local_receipt_recovers_lost_complete_reply_without_post(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    html = '<div>复查提交历史记录</div>'
    receipt = {'course_id': '_1_1', 'content_id': '_2_1', 'portal_verified': True,
               'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': html,
               'review_history_sha256': hashlib.sha256(html.encode()).hexdigest()}
    with permit() as fence:
        fence.arm()
        metadata.update(state='receipt_verified', receipt=receipt)
        fence.complete(receipt)
    with permit() as recovered:
        assert recovered.recovered_receipt == receipt
        assert states['one']['state'] == 'submitted'


def test_old_local_assignment_requirements_cannot_obtain_a_permit(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    metadata['requirements_sha256'] = 'c' * 64
    with pytest.raises(PreparationError):
        with permit():
            pytest.fail('old requirements must never reach portal POST')
    assert not states


def panel_setup(monkeypatch, tmp_path):
    import json
    from pku_sync.panel.submission_permit import SubmissionPermit
    from tests.test_panel_assignment_workflow import _client, _file_draft
    _, _, states, _, call = setup(monkeypatch, tmp_path)
    clients = [_client(tmp_path / name, monkeypatch, fence_factory=SubmissionPermit) for name in ('windows', 'other-device')]
    rows = json.loads((tmp_path / 'windows/course/assignments/index.json').read_text('utf-8'))
    monkeypatch.setattr(catalog, 'current', lambda *args: {'revision': 1, 'snapshot': {
        'sections': {'assignments': {'items': rows}}}})
    drafts = [_file_draft(client) for client in clients]
    return clients, drafts, states, call


def test_two_panel_devices_send_one_post_then_block_unknown_result(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pku_sync.panel import assignment_workflow
    from pku_sync.submit import SubmissionForm
    clients, drafts, states, call = panel_setup(monkeypatch, tmp_path)
    posts, logins = [], []
    class Campus:
        def post(self, *args, **kwargs):
            assert any(row['state'] == 'verification_pending' for row in states.values())
            posts.append('POST')
            return SimpleNamespace(status_code=200, json=lambda: {})
        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, text='<div>unknown submission</div>',
                url='https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_103987_1&content_id=_1714169_1')
        def close(self): pass
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: logins.append('login') or Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    for client, (base, draft, body) in zip(clients, drafts):
        response = client.post(base + '/submit', json=body)
        assert response.status_code == 400
        if not posts and not states:
            pytest.fail(response.json()['detail'])
    assert posts == ['POST'] and logins == ['login']
    assert list(states.values())[0]['state'] == 'verification_pending'


def test_panel_recovers_verified_receipt_after_lost_account_reply_without_resubmission(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from pku_sync.panel import assignment_workflow
    from pku_sync.submit import SubmissionForm
    clients, drafts, states, call = panel_setup(monkeypatch, tmp_path)
    posts = []
    class Campus:
        def post(self, *args, **kwargs):
            posts.append('POST')
            return SimpleNamespace(status_code=200, json=lambda: {})
        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, text='<div>复查提交历史记录</div>',
                url='https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_103987_1&content_id=_1714169_1')
        def close(self): pass
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    fail_once = [True]
    def lost_complete(*args, **kwargs):
        result = call(*args, **kwargs)
        if args[3].endswith('/complete') and fail_once[0]:
            fail_once[0] = False
            raise catalog.CatalogUnavailable('lost account reply after saving receipt')
        return result
    monkeypatch.setattr(catalog, '_call', lost_complete)
    client, (base, draft, body) = clients[0], drafts[0]
    assert client.post(base + '/submit', json=body).status_code == 400
    metadata_path = tmp_path / 'windows/submission-drafts' / draft['draft_id'] / 'draft.json'
    assert json.loads(metadata_path.read_text('utf-8'))['state'] == 'receipt_verified'
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: pytest.fail('recovery must not open a new attempt'))
    assert client.post(base + '/submit', json=body).status_code == 200
    assert json.loads(metadata_path.read_text('utf-8'))['state'] == 'submitted'
    assert posts == ['POST']


def test_account_switch_during_shared_arm_stops_actual_post(monkeypatch, tmp_path):
    from pku_sync.panel import assignment_workflow
    from pku_sync.submit import SubmissionForm
    clients, drafts, states, call = panel_setup(monkeypatch, tmp_path)
    class Campus:
        def post(self, *args, **kwargs):
            pytest.fail('account changed during arm must never POST')
        def close(self): pass
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    def switch_account(*args, **kwargs):
        result = call(*args, **kwargs)
        if args[3].endswith('/arm'):
            clients[0].app.state.recording_manager.settings.pku_username = 'another-student'
        return result
    monkeypatch.setattr(catalog, '_call', switch_account)
    base, _, body = drafts[0]
    result = clients[0].post(base + '/submit', json=body)
    assert result.status_code == 400 and '账号' in result.json()['detail']
    assert list(states.values())[0]['state'] == 'verification_pending'


def test_reconcile_proves_unarmed_crash_can_abort_without_post(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    fence = permit().__enter__()  # Simulate a process ending before arm/exit.
    metadata['state'] = 'verification_pending'
    assert permit().reconcile()['state'] == 'aborted'
    assert states['one']['state'] == 'aborted'
    assert metadata['state'] == 'aborted'


def test_reconcile_unknown_post_cannot_clear_shared_fence(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    with permit() as fence:
        fence.arm()
    metadata['state'] = 'verification_pending'
    with pytest.raises(PreparationError):
        permit().reconcile()
    assert states['one']['state'] == metadata['state'] == 'verification_pending'


def test_recovery_route_finds_receipt_after_restart_and_never_opens_portal(monkeypatch, tmp_path):
    import json
    from pku_sync.panel import assignment_workflow
    clients, drafts, states, call = panel_setup(monkeypatch, tmp_path)
    client, (_, draft, _) = clients[0], drafts[0]
    path = tmp_path / 'windows/submission-drafts' / draft['draft_id'] / 'draft.json'
    metadata = json.loads(path.read_text('utf-8'))
    html = '<div>复查提交历史记录</div>'
    receipt = {'portal_verified': True, 'course_id': metadata['course_id'], 'content_id': metadata['content_id'],
               'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': html,
               'review_history_sha256': hashlib.sha256(html.encode()).hexdigest()}
    metadata.update(state='receipt_verified', receipt=receipt,
                    submission_fence={'protocol': 1, 'revision': 1, 'permit_id': 'permit', 'state': 'verification_pending'})
    path.write_text(json.dumps(metadata), 'utf-8')
    states[draft['draft_id']] = {'operation_id': draft['draft_id'], 'revision': 1, 'file_sha256': draft['sha256'],
                               'permit_id': 'permit', 'state': 'verification_pending'}
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: pytest.fail('recovery must not open portal'))
    listing = client.get('/api/campus/courses/_103987_1/submission-receipts').json()
    assert listing['items'][0]['state'] == 'receipt_verified'
    recovery = listing['items'][0]['recovery_url']
    assert client.post(recovery).status_code == 200
    assert json.loads(path.read_text('utf-8'))['state'] == 'submitted'
    assert client.get('/api/campus/submission-drafts/' + draft['draft_id'] + '/receipt').status_code == 200


def test_recovery_does_not_create_operation_when_original_record_is_missing(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    metadata['submission_fence'] = {'protocol': 1, 'revision': 1}
    calls = []
    def missing(settings, method, course, suffix='', body=None):
        calls.append(method)
        raise catalog.CatalogUnavailable('no original record')
    monkeypatch.setattr(catalog, '_call', missing)
    with pytest.raises(PreparationError):
        permit().reconcile()
    assert calls == ['GET'] and states == {}


def test_recovery_route_rejects_other_account_before_service_read(monkeypatch, tmp_path):
    clients, drafts, states, call = panel_setup(monkeypatch, tmp_path)
    client = clients[0]
    client.app.state.recording_manager.settings.pku_username = 'another-student'
    monkeypatch.setattr(catalog, '_call', lambda *a, **kw: pytest.fail('wrong account must not query original operation'))
    response = client.post('/api/campus/submission-drafts/' + drafts[0][1]['draft_id'] + '/recover')
    assert response.status_code == 409 and '账号' in response.json()['detail']


def test_altered_local_receipt_does_not_complete_pending_operation(monkeypatch, tmp_path):
    permit, metadata, states, saved, call = setup(monkeypatch, tmp_path)
    with permit() as fence:
        fence.arm()
    metadata.update(state='receipt_verified', receipt={
        'portal_verified': True, 'course_id': '_1_1', 'content_id': '_2_1',
        'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': 'changed',
        'review_history_sha256': 'd' * 64})
    with pytest.raises(PreparationError):
        permit().reconcile()
    assert states['one']['state'] == 'verification_pending'
