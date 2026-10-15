"""Natural identity failures on the installed app, with unchanged real responses.

Expiry needs a matching existing server session. No clock, session row, token
or verification flag is manufactured. Private temporary env changes are owned
and restored only when the product has not concurrently changed their fields.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values

from .core import ResourceBlocked, sha256
from .fault_boundary import AccountFaultBoundary


class AccountRequestObserver(AccountFaultBoundary):
    """Only transparent forwarding; live runs cannot arm any fault or hold."""
    @contextmanager
    def inject(self, variant):
        raise ResourceBlocked('fault_injection_forbidden_in_live')
        yield  # Preserve the context-manager interface, without entering it.

    @contextmanager
    def hold_owned_profile_read(self, ready):
        raise ResourceBlocked('fault_injection_forbidden_in_live')
        yield


def _owned_profile(run):
    root = Path(run.need('test_root')).resolve()
    app = Path(run.need('instances.primary.app_dir')).resolve()
    path = app / '.env'
    if (app == root or not app.is_relative_to(root) or path.is_symlink()
            or not path.is_file() or path.stat().st_nlink != 1):
        raise ResourceBlocked('live_identity_owned_private_profile_required')
    return app, path


def _runtime(run):
    runtime = run.runtime('primary')
    installed = Path(runtime.get('installed_path', '')).resolve()
    process = runtime.get('process') or {}
    if (run.mode != 'live' or run.context.get('target') != 'windows'
            or runtime.get('alive') is not True or runtime.get('target') != 'windows'
            or runtime.get('artifact_sha256') != run.context['artifact_sha256']
            or not installed.is_relative_to(Path(run.need('test_root')).resolve())
            or sha256(installed) != runtime.get('installed_sha256')
            or Path(process.get('path') or '').resolve() != installed
            or type(process.get('session_id')) is not int or process['session_id'] <= 0
            or run._fault_boundary is not None
            or not isinstance(run._live_account_observer, AccountRequestObserver)):
        raise ResourceBlocked('live_identity_real_interactive_unmodified_runtime_required')


def refresh_oracle_identity(run, ui, expected_email):
    """Bind independent reads to the session actually saved by this UI login."""
    _, env_path = _owned_profile(run)
    token = dotenv_values(env_path).get('PLATFORM_TOKEN') or ''
    reference = run.config.get('credentials', {}).get('account_token')
    if (not token or '\n' in token or '\r' in token or not isinstance(reference, dict)
            or set(reference) != {'env'} or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reference['env'])):
        raise ResourceBlocked('live_identity_saved_session_reference_required')
    os.environ[reference['env']] = token
    native = ui.request_read('/api/auth/me')
    real = run.oracle.get('account', '/v1/auth/me')
    expected = expected_email.strip().lower()
    if (native.get('active') is not True or native.get('email', '').lower() != expected
            or real.get('email', '').lower() != expected
            or type(real.get('email_verified')) is not bool
            or native.get('email_verified') is not real['email_verified']):
        raise ResourceBlocked('live_identity_saved_native_and_independent_account_disagree')
    run.actions.append({'actual_current_account_identity': {
        'email_sha256': hashlib.sha256(expected.encode()).hexdigest(),
        'email_verified': real['email_verified'], 'token_sha256': hashlib.sha256(token.encode()).hexdigest()}})
    return real


def wrong_password(run, ui, email):
    _runtime(run)
    _, env_path = _owned_profile(run)
    if ui.visible('.sidebar [data-block="quota"]'):
        from product_e2e.catalog_journeys import _open_account_controls
        _open_account_controls(ui)
        ui.click('.sidebar [data-action="logout"]')
        ui.wait(lambda: ui.visible('#auth-email'))
    ui.fill('#auth-email', email)
    ui.fill('#auth-password', 'E2E-deliberately-wrong-' + secrets.token_hex(32))
    start = len(run.network)
    with ui.page.expect_response(lambda response: response.request.method == 'POST'
            and urlsplit(response.url).path == '/api/auth/login', timeout=60000) as captured:
        ui.click('[data-action="auth-submit"]')
    response = captured.value
    response.finished()
    ui.wait(lambda: ui.visible('#auth-help') and bool(ui.text('#auth-help').strip()))
    upstream = [r for r in run.network[start:] if r.get('boundary') == 'account'
                and r.get('method') == 'POST' and r.get('path') == '/v1/auth/login']
    ok = (len(upstream) == 1 and upstream[0].get('upstream_status') == upstream[0].get('status') == 401
          and upstream[0].get('fault') is None and bool(upstream[0].get('upstream_body_sha256'))
          and response.status in {400, 401} and ui.visible('#auth-email')
          and ui.visible('[data-action="auth-submit"]')
          and not ui.visible('.sidebar [data-block="quota"]')
          and not ui.visible('.recording-row')
          and not (dotenv_values(env_path).get('PLATFORM_TOKEN') or ''))
    ui.screenshot('live-real-wrong-password-actionable-rejection')
    run.actions.append({'actual_live_wrong_password': {'passed': ok, 'native_status': response.status,
        'actual_upstream_status': upstream[0]['upstream_status'] if len(upstream) == 1 else None,
        'response_substituted': False}})
    return ok


def unverified_identity(run, ui, email):
    _runtime(run)
    real = refresh_oracle_identity(run, ui, email)
    ok = real['email_verified'] is False and ui.visible('.sidebar [data-action="resend-verification"]')
    ui.screenshot('live-real-unverified-before-human-email-verification')
    run.actions.append({'actual_live_unverified_identity': {'passed': ok, 'verification_flag_modified': False}})
    return ok


def verify_oauth_token_readback(run, ui):
    _, env_path = _owned_profile(run)
    token = dotenv_values(env_path).get('NOTION_TOKEN') or ''
    reference = run.config.get('credentials', {}).get('notion_token')
    if (not token or '\n' in token or '\r' in token or not isinstance(reference, dict)
            or set(reference) != {'env'} or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reference['env'])):
        raise ResourceBlocked('actual_oauth_saved_token_environment_reference_required')
    os.environ[reference['env']] = token
    identity = run.oracle.get('notion', '/v1/users/me')
    parent_id = str(run.need('learning.notion_test_parent_id')).replace('-', '').lower()
    parent = run.oracle.get('notion', '/v1/pages/' + parent_id)
    ok = (identity.get('type') == 'bot' and parent.get('id', '').replace('-', '').lower() == parent_id
          and not parent.get('archived') and not parent.get('in_trash')
          and ui.request_read('/api/connection').get('connected') is True)
    run.actions.append({'actual_oauth_saved_token_independent_readback': {
        'passed': ok, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'test_parent_id': parent_id}})
    if not ok:
        raise AssertionError('actual_oauth_token_or_test_parent_readback_failed')
    return True


def validate_expired_session(value, token_sha256, email_sha256):
    if not isinstance(value, dict):
        raise ResourceBlocked('real_owned_expired_session_independent_metadata_required')
    times = (value or {}).get('issued_at'), (value or {}).get('expires_at'), (value or {}).get('server_observed_at')
    if (not isinstance(value, dict) or value.get('authority') != 'server-own-session-read-only-sqlite'
            or value.get('token_sha256') != token_sha256 or value.get('email_sha256') != email_sha256
            or type(value.get('account_id')) is not int or value['account_id'] <= 0
            or type(value.get('sessions_changed')) is not int or value['sessions_changed'] != 0
            or any(type(t) not in (int, float) or not math.isfinite(t) for t in times)
            or not 0 <= times[0] < times[1] <= times[2]):
        raise ResourceBlocked('real_owned_expired_session_independent_metadata_required')
    return value


def expired_session_metadata(run, token, email):
    """Read only the matching current owner's session; export no credentials."""
    if run.mode != 'live' or run.context.get('target') != 'windows':
        raise ResourceBlocked('real_owned_expired_session_live_windows_required')
    expected_email = email.strip().lower()
    if expected_email != 'suiseiseki@stu.pku.edu.cn':
        raise ResourceBlocked('real_owned_expired_session_current_test_owner_required')
    digest = hashlib.sha256(token.encode()).hexdigest()
    email_digest = hashlib.sha256(expected_email.encode()).hexdigest()
    params = json.dumps({'token_sha256': digest, 'email': expected_email}, ensure_ascii=False)
    program = '''import hashlib,json,sqlite3,time
p=json.loads(PARAMS)
with sqlite3.connect('file:/opt/pku-server/data/accounts.db?mode=ro',uri=True,timeout=5) as db:
 owner=db.execute('SELECT id FROM accounts WHERE email=?',(p['email'],)).fetchone()
 assert owner and owner[0]==6
 matches=[(issued,expires) for token,issued,expires in db.execute('SELECT token,issued_at,expires_at FROM sessions WHERE account_id=?',(owner[0],)) if hashlib.sha256(token.encode()).hexdigest()==p['token_sha256']]
 now=time.time()
 v=None
 if len(matches)==1:
  issued,expires=matches[0]
  v={'authority':'server-own-session-read-only-sqlite','token_sha256':p['token_sha256'],'email_sha256':hashlib.sha256(p['email'].encode()).hexdigest(),'issued_at':issued,'expires_at':expires,'server_observed_at':now,'account_id':owner[0],'sessions_changed':0}
print(json.dumps(v))
'''.replace('PARAMS', repr(params))
    result = subprocess.run(['C:/Windows/System32/OpenSSH/ssh.exe', '-F', 'C:/Users/A/.ssh/config',
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
        'server', 'python3 -'], input=program, capture_output=True, text=True, encoding='utf8', timeout=45)
    if result.returncode != 0:
        raise ResourceBlocked('real_owned_expired_session_independent_read_unavailable')
    value = json.loads(result.stdout)
    target = run.case_dir / ('oracle-real-session-expiry-' + uuid4().hex + '.json')
    target.write_text(json.dumps(value, indent=2) + '\n', 'utf8')
    return validate_expired_session(value, digest, email_digest)


def expired_session(run, ui, email):
    from .catalog_journeys import _file_hashes
    from .oracles import Oracle, OracleError
    _runtime(run)
    app, env_path = _owned_profile(run)
    token = run.credential('expired_account_token')
    metadata = expired_session_metadata(run, token, email)  # Before any env mutation.
    auth_sha = hashlib.sha256(('Bearer ' + token).encode()).hexdigest()
    private_env = 'PKU_E2E_ACTUAL_EXPIRED_ACCOUNT_TOKEN'
    os.environ[private_env] = token
    oracle = Oracle({'credentials': {'account_token': {'env': private_env}}}, run.case_dir / ('independent-expired-' + uuid4().hex))
    try:
        oracle.get('account', '/v1/auth/me')
    except OracleError as exc:
        if exc.status != 401:
            raise ResourceBlocked('real_owned_expired_session_actual_401_required') from None
    else:
        raise ResourceBlocked('real_owned_expired_session_actual_401_required')
    # Keep the independent HTTP 401 in the formal root evidence inventory;
    # nested oracle files would otherwise be omitted by NativeRun.finish().
    import shutil
    for evidence in oracle.evidence_dir.glob('oracle*.json'):
        target = run.case_dir / ('oracle-expired-http-' + uuid4().hex + '.json')
        shutil.copyfile(evidence, target)
        if sha256(target) != sha256(evidence):
            raise ResourceBlocked('real_owned_expired_session_evidence_copy_changed')
    original = env_path.read_bytes()
    values = dict(dotenv_values(env_path))
    if not values.get('PLATFORM_TOKEN') or values['PLATFORM_TOKEN'] == token:
        raise ResourceBlocked('live_expiry_valid_original_session_required')
    preserved = _file_hashes(run, student_only=True)
    if not preserved:
        raise ResourceBlocked('live_expiry_nonempty_student_files_required')
    run.quit('primary', require_graceful=True)
    backup = app / ('private-expiry-backup-' + uuid4().hex + '.env')
    with backup.open('xb') as out:
        out.write(original)
    changed = {**{k: str(v or '') for k, v in values.items()},
               'PLATFORM_TOKEN': token, 'NOTION_TOKEN': '', 'PKU_USERNAME': '', 'PKU_PASSWORD': ''}
    # dotenv's quoted representation keeps special characters literal.
    def env_bytes(mapping):
        return ''.join(k + "='" + str(v or '').replace('\\', '\\\\').replace("'", "\\'") + "'\n"
                       for k, v in mapping.items()).encode('utf8')
    stage = app / ('private-expiry-stage-' + uuid4().hex + '.env')
    stage.write_bytes(env_bytes(changed))
    stage.replace(env_path)
    start = len(run.network)
    restored = False
    try:
        expired_ui = run.session()
        expired_ui.wait(lambda: expired_ui.visible('#auth-email'))
        actual = expired_ui.request_read('/api/auth/me')
        requests = [r for r in run.network[start:] if r.get('boundary') == 'account'
            and r.get('method') == 'GET' and r.get('path') in {'/v1/auth/me', '/v1/profile', '/v1/quota'}
            and r.get('authorization_sha256') == auth_sha]
        ok = (actual.get('active') is False and expired_ui.visible('[data-action="auth-submit"]')
              and not expired_ui.visible('.recording-row')
              and any(r.get('upstream_status') == r.get('status') == 401 and r.get('fault') is None for r in requests)
              and preserved == _file_hashes(run, student_only=True))
        expired_ui.screenshot('live-real-server-expired-session-login-recovery')
    finally:
        run.quit('primary', require_graceful=True)
        current = dict(dotenv_values(env_path))
        # The product can clear its invalid token. Preserve any other concurrent
        # edits and the private recovery backup rather than overwrite them.
        expected = {**changed, 'PLATFORM_TOKEN': current.get('PLATFORM_TOKEN')}
        if current.get('PLATFORM_TOKEN') not in {'', token} or current != expected:
            raise ResourceBlocked('live_expiry_concurrent_private_edit_preserved')
        if sha256(backup) != hashlib.sha256(original).hexdigest():
            raise ResourceBlocked('live_expiry_original_private_backup_changed')
        restore = app / ('private-expiry-restore-' + uuid4().hex + '.env')
        restore.write_bytes(original)
        restore.replace(env_path)
        restored = True
    active = run.session()
    refresh_oracle_identity(run, active, email)
    ok = ok and restored and preserved == _file_hashes(run, student_only=True)
    run.actions.append({'actual_live_expired_session': {'passed': ok, 'natural_server_expiry_metadata': metadata,
        'native_real_401_observed': bool(requests), 'original_private_env_restored': restored,
        'server_session_changed': False, 'clock_changed': False, 'response_substituted': False}})
    return active, ok


def natural_errors_complete(observed, *, include_unverified=True):
    required = ('wrong-password', 'expired-session') + (('unverified',) if include_unverified else ())
    return all(observed.get(key) is True for key in required)
