"""Developer contracts for natural live identity failures, never native proof."""
from types import SimpleNamespace

import pytest

from product_e2e.core import ResourceBlocked


def test_live_request_observer_cannot_enable_any_fault():
    from product_e2e.identity_live import AccountRequestObserver
    owner = SimpleNamespace(network=[], mode='live', config={})
    observer = AccountRequestObserver(owner)
    try:
        with pytest.raises(ResourceBlocked, match='forbidden_in_live'):
            with observer.inject('expired-session'):
                pytest.fail('live observer corrupted a response')
        assert observer.variant is None and observer.receipt is None and owner.network == []
    finally:
        observer.close()


@pytest.mark.parametrize('change', ['future', 'wrong-hash', 'wrong-owner', 'missing', 'list', 'string', 'bool-changes'])
def test_expiry_requires_matching_real_owner_and_server_expiry(change):
    from product_e2e.identity_live import validate_expired_session
    expected = 'a' * 64
    value = {'authority': 'server-own-session-read-only-sqlite', 'token_sha256': expected,
        'email_sha256': 'b' * 64, 'issued_at': 10, 'expires_at': 20,
        'server_observed_at': 30, 'account_id': 6, 'sessions_changed': 0}
    if change == 'future': value['expires_at'] = 40
    if change == 'wrong-hash': value['token_sha256'] = 'c' * 64
    if change == 'wrong-owner': value['email_sha256'] = 'c' * 64
    if change == 'missing': value = None
    if change == 'list': value = []
    if change == 'string': value = 'metadata missing'
    if change == 'bool-changes': value['sessions_changed'] = False
    with pytest.raises(ResourceBlocked, match='real_owned_expired_session'):
        validate_expired_session(value, expected, 'b' * 64)


def test_matching_server_natural_expiry_metadata_is_required_not_a_pass_flag():
    from product_e2e.identity_live import validate_expired_session
    value = {'authority': 'server-own-session-read-only-sqlite', 'token_sha256': 'a' * 64,
        'email_sha256': 'b' * 64, 'issued_at': 10, 'expires_at': 20,
        'server_observed_at': 30, 'account_id': 6, 'sessions_changed': 0}
    assert validate_expired_session(value, 'a' * 64, 'b' * 64) == value


def test_incomplete_natural_error_set_cannot_satisfy_full_live_assertion():
    from product_e2e.identity_live import natural_errors_complete
    assert not natural_errors_complete({'wrong-password': True, 'unverified': True})
    assert not natural_errors_complete({'wrong-password': True, 'unverified': True, 'expired-session': False})
    assert natural_errors_complete({'wrong-password': True, 'unverified': True, 'expired-session': True})
    assert not natural_errors_complete({'wrong-password': True}, include_unverified=False)
    assert natural_errors_complete({'wrong-password': True, 'expired-session': True}, include_unverified=False)


@pytest.mark.parametrize('upstream,passed', [
    ([], False),
    ([{'status': 401, 'upstream_status': 200, 'fault': None}], False),
    ([{'status': 401, 'upstream_status': 401, 'fault': 'expired-session'}], False),
    ([{'status': 401, 'upstream_status': 401, 'fault': None}] * 2, False),
    ([{'status': 401, 'upstream_status': 401, 'fault': None}], True),
])
def test_wrong_password_requires_one_unchanged_actual_service_rejection(tmp_path, monkeypatch, upstream, passed):
    from product_e2e import identity_live
    from contextlib import contextmanager
    app = tmp_path / 'primary'
    app.mkdir()
    (app / '.env').write_text('PLATFORM_TOKEN=\n', 'utf8')
    run = SimpleNamespace(network=[], actions=[], need=lambda key:
        str(tmp_path if key == 'test_root' else app))
    monkeypatch.setattr(identity_live, '_runtime', lambda run: None)  # Developer only.
    response = SimpleNamespace(status=400, finished=lambda: None)
    @contextmanager
    def capture(predicate, timeout):
        yield SimpleNamespace(value=response)
    def visible(selector):
        return selector not in {'.sidebar [data-block="quota"]', '.recording-row'}
    def click(selector):
        for row in upstream:
            run.network.append({'boundary': 'account', 'method': 'POST', 'path': '/v1/auth/login',
                                'upstream_body_sha256': 'developer-not-native', **row})
    ui = SimpleNamespace(page=SimpleNamespace(expect_response=capture), visible=visible,
        fill=lambda *args: None, click=click, wait=lambda fn: fn(), text=lambda sel: 'Wrong password; retry',
        screenshot=lambda name: None)
    assert identity_live.wrong_password(run, ui, 'owner@example.test') is passed


def test_owned_external_browser_uses_normal_windows_close_before_pass(tmp_path, monkeypatch):
    from product_e2e import core
    run = core.NativeRun({'id': 'JOURNEY-01', 'assertions': []}, 'live', {
        'config': {}, 'run_dir': tmp_path, 'target': 'windows'})
    observed = []
    run.processes['external'] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    run.config['external_browser'] = {'executable_path':str(tmp_path / 'msedge.exe')}
    monkeypatch.setattr(core, 'close_owned_windows', lambda pid, **kwargs: observed.append(pid))
    monkeypatch.setattr(core.subprocess, 'run', lambda *args, **kwargs: pytest.fail('forced cleanup counted as normal close'))
    receipt = run.quit('external', require_graceful=True)
    assert receipt['graceful'] is True and receipt['method'] == 'owned-windows-WM_CLOSE' and observed == [12345]
