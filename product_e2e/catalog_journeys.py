"""Native identity/catalog/recovery journeys; no HTTP product writes or fake live data.

All mutations originate in the installed window. App GETs only observe the
result, and independent portal/service/OS observations decide the assertions.
Missing OS/third-party capabilities are blockers, never manual pass flags.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from product_e2e.core import ResourceBlocked

# Frozen 0.1.35 global trace e037afd5... observed 396 Notion + 88 account
# requests. Conservatively grant that entire old global service count to each
# selected course, with the existing Notion transport's three physical attempts.
# UI polling is separately bounded by the observation deadline.
_BACKGROUND_SERVICE_REQUESTS_PER_COURSE = (396 + 88) * 3


def _required(run, value, name):
    if value is None:
        raise ResourceBlocked('missing-observation:' + name)
    return value


def _open_account_controls(ui):
    if not ui.visible('.sidebar [data-action="logout"]'):
        ui.click('.sidebar [data-persistent-disclosure="desktop-account"] > summary')
        ui.wait(lambda: ui.visible('.sidebar [data-action="logout"]'))


def _action(ui, name, suffix=""):
    if name in {'logout', 'redeem', 'resend-verification', 'refresh-verification'}:
        _open_account_controls(ui)
    elif name == 'revoke-notion' and not ui.visible('.sidebar [data-action="revoke-notion"]'):
        ui.click('.sidebar [data-persistent-disclosure="desktop-connection"] > summary')
        ui.wait(lambda: ui.visible('.sidebar [data-action="revoke-notion"]'))
    scope = '.sidebar ' if name in {'open-dashboard', 'open-welcome', 'logout', 'connect-notion',
                                   'redeem', 'resend-verification', 'refresh-verification',
                                   'campus-account-settings', 'revoke-notion'} else ''
    ui.click(scope + '[data-action="' + name + '"]' + suffix)


def _task(ui, timeout=90, *, started_monotonic=None, path='/api/recordings/task'):
    started = time.monotonic() if started_monotonic is None else started_monotonic
    deadline = started + timeout
    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise AssertionError('Native single-course task deadline exceeded')
        return budget
    def read():
        remaining()
        result = ui.request_read(path)
        remaining()  # A slow terminal GET is part of the original budget.
        return result
    ui.wait(lambda: read().get('state')
            in {'running', 'done', 'partial', 'failed', 'cancelled'}, timeout=remaining())
    ui.wait(lambda: read().get('state') in {'done', 'partial', 'failed', 'cancelled'},
            timeout=remaining())
    result = read()
    result['observed_elapsed_seconds'] = time.monotonic() - started
    return result


def _start_refresh(ui, *, course_id=None):
    if course_id is None:
        _action(ui, 'open-dashboard')
    elif not re.fullmatch(r'_[0-9]+_[0-9]+', course_id):
        raise ValueError('Expected official selected course identity')
    if ui.page is None:
        raise ResourceBlocked('mac-native-sync-response-observer-required')
    before = len(ui.owner.network)
    from urllib.parse import urlsplit
    with ui.page.expect_response(lambda response:
            urlsplit(response.url).path == '/api/recordings/sync'
            and response.request.method == 'POST', timeout=30000) as started:
        _action(ui, 'recording-sync' if course_id is None else 'recording-sync-course',
                '' if course_id is None else '[data-course="' + course_id + '"]')
    response = started.value
    # Observing the UI-origin POST prevents stale/idle task state passing a click.
    ui.wait(lambda: any(row.get('method') == 'POST' and row.get('path') == '/api/recordings/sync'
                        for row in ui.owner.network[before:]))
    if response.status != 200:
        raise AssertionError('Native catalog action was rejected')
    accepted = response.json()
    if not isinstance(accepted, dict) or accepted.get('kind') != 'sync':
        raise AssertionError('Native catalog response was not this sync action')
    if course_id is not None and accepted.get('course_id') != course_id:
        raise AssertionError('Native sync response changed the selected course identity')
    return accepted


def _refresh(ui, timeout=90):
    started = time.monotonic()
    accepted = _start_refresh(ui)
    result = accepted if accepted.get('state') in {'done', 'partial', 'failed', 'cancelled'} else _task(ui, timeout=timeout, started_monotonic=started)
    return {**result, 'observed_elapsed_seconds': time.monotonic() - started}


def _refresh_course(ui, course_id, timeout=90):
    _open(ui, course_id)
    started = time.monotonic()
    accepted = _start_refresh(ui, course_id=course_id)
    task = (accepted if accepted.get('state') in {'done', 'partial', 'failed', 'cancelled'}
            else _task(ui, timeout=timeout, started_monotonic=started))
    elapsed = time.monotonic() - started
    if elapsed >= timeout or task.get('kind') != 'sync' or task.get('course_id') != course_id:
        raise AssertionError('Native selected course identity or original deadline failed')
    return {**task, 'observed_elapsed_seconds': elapsed}


def _background_result_valid(task, expected_ids, *, cancelled=False):
    """Every course has an explicit bounded result; partial is never all-done."""
    rows = task.get('course_results')
    if (not expected_ids or len(set(expected_ids)) != len(expected_ids)
            or task.get('kind') != 'sync' or task.get('scope') != 'background'
            or not isinstance(task.get('id'), str) or not task['id']
            or not isinstance(rows, list) or len(rows) != len(expected_ids)
            or any(not isinstance(row, dict) for row in rows)
            or {row.get('course_id') for row in rows} != set(expected_ids)):
        return False
    for row in rows:
        if (type(row.get('attempts')) is not int or not 0 <= row['attempts'] <= 3
                or type(row.get('retryable')) is not bool
                or any(type(row.get(key)) is not int or row[key] != 0 for key in ('summary_created', 'summary_existing'))):
            return False
        if row.get('state') == 'done':
            if (row['attempts'] < 1 or row.get('metadata_state') != 'confirmed'
                    or row.get('projection_state') != 'confirmed'):
                return False
        elif row.get('state') in {'failed', 'partial'}:
            if not isinstance(row.get('error'), str) or not row['error'] or row['attempts'] < 1:
                return False
        elif not cancelled or row.get('state') not in {'pending', 'cancelled', 'interrupted'}:
            return False
    return (task.get('state') == 'cancelled' if cancelled else
            task.get('state') == ('done' if all(row['state'] == 'done' for row in rows) else 'partial'))


def _background_network_valid(events, course_count):
    # This is a fixed finite physical request budget, not a success override.
    # It also bounds failed operations whose logical request IDs are absent.
    if sum(row.get('boundary') in {'notion', 'account'} for row in events) > _BACKGROUND_SERVICE_REQUESTS_PER_COURSE * max(course_count, 1):
        return False
    reads = [row for row in events if row.get('boundary') == 'notion']
    for row in reads:
        if row.get('upstream_status') != 429:
            continue
        delay = row.get('retry_after_seconds')
        started, elapsed = row.get('started_monotonic'), row.get('elapsed_seconds')
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (delay, started, elapsed)) or delay < 0 or elapsed < 0):
            return False  # Missing real delay/timing evidence cannot prove compliance.
        responded = started + elapsed
        if any(type(other.get('started_monotonic')) not in (int, float)
               or not math.isfinite(other['started_monotonic'])
               or responded < other['started_monotonic'] < responded + delay
               for other in reads if other is not row):
            return False
    return True


def _background_refresh(run, ui, course_id, expected_ids, *, accepted=None, resume=False, retained=None, observation_before=None):
    """Bounded observation of actual background progress and cached foreground."""
    before_network, before_files, before_effects = (observation_before if observation_before is not None else
        (len(run.network), _file_hashes(run, student_only=True), _effects(run)))
    started = time.monotonic()
    if resume:
        from urllib.parse import urlsplit
        with ui.page.expect_response(lambda response: urlsplit(response.url).path == '/api/recordings/sync'
                and response.request.method == 'POST', timeout=30000) as capture:
            _action(ui, 'recording-resume-background')
        if capture.value.status != 200:
            raise AssertionError('Actual native background resume was rejected')
        accepted = capture.value.json()
    accepted = accepted or _start_refresh(ui)
    if accepted.get('scope') != 'background' or not accepted.get('id'):
        raise AssertionError('Actual native global refresh did not identify an independent background task')
    identity = accepted['id']
    snapshots, done = [], dict(retained or {})
    deadline = started + 30 + 90 * max(len(expected_ids), 1)
    def read():
        if time.monotonic() >= deadline:
            raise AssertionError('Background observation deadline exceeded')
        value = ui.request_read('/api/recordings/task?scope=background')
        if time.monotonic() >= deadline:
            raise AssertionError('Background observation deadline exceeded during a physical GET')
        if value.get('id') != identity or value.get('scope') != 'background':
            raise AssertionError('Background observation switched task identity')
        result_rows = value.get('course_results')
        if not isinstance(result_rows, list) or any(not isinstance(row, dict) for row in result_rows):
            raise AssertionError('Malformed background per-course progress')
        if result_rows and (type(value.get('progress_done')) is not int
                or type(value.get('progress_total')) is not int
                or type(value.get('progress_remaining')) is not int
                or value['progress_total'] != len(result_rows)
                or value['progress_done'] != sum(row.get('state') == 'done' for row in result_rows)
                or value.get('progress_remaining') != len(result_rows) - value['progress_done']):
            raise AssertionError('Background progress claims inconsistent completion')
        if value.get('current_course_id', '') not in {'', *expected_ids}:
            raise AssertionError('Background progress switched official course scope')
        if any(row.get('state') == 'done' and row.get('course_id') in done and done[row['course_id']] != row
               for row in value.get('course_results', [])):
            raise AssertionError('A completed background course result was rewritten')
        for row in value.get('course_results', []):
            if row.get('course_id') in done and row != done[row['course_id']]:
                raise AssertionError('A completed background course was restarted or changed')
            if row.get('state') == 'done':
                done[row['course_id']] = dict(row)
        snapshots.append(value)
        return value
    def initial_progress():
        value = read()
        if time.monotonic() - started >= 30:
            raise AssertionError('Background startup/progress deadline exceeded')
        return bool(value.get('course_results')) and bool(value.get('stage'))
    ui.wait(initial_progress, timeout=30)
    initial = snapshots[-1]
    if {row['course_id'] for row in initial['course_results']} != set(expected_ids):
        raise AssertionError('Background scope differs from independently read official courses')
    ui.wait(lambda: ui.visible('[data-background-sync]') and ui.visible('[data-background-stage]'), timeout=10)
    foreground = _open(ui, course_id)
    if (foreground.get('id') != course_id or foreground.get('read_only') is not False
            or not ui.visible('#course-lessons') or not ui.visible('[data-background-sync]')):
        raise AssertionError('Valid cached selected course became unusable during background refresh')
    button = '[data-action="recording-sync-course"][data-course="' + course_id + '"]'
    current = read()
    if current.get('state') == 'running':
        same_course_busy = current.get('current_course_id') == course_id
        if (not ui.visible(button) or (ui.attr(button, 'disabled') is not None) != same_course_busy):
            raise AssertionError('Background course reservation blocked an unrelated course or allowed a conflicting writer')
    # The per-course single operation remains 90 seconds. This separate total
    # bounds background observation without requiring all courses in one window.
    def terminal():
        if time.monotonic() >= deadline or not _background_network_valid(run.network[before_network:], len(expected_ids)):
            raise AssertionError('Background request/retry/observation budget failed')
        value = read()
        if time.monotonic() >= deadline:
            raise AssertionError('Background observation deadline exceeded during a physical GET')
        return value['state'] in {'done', 'partial', 'failed', 'cancelled'}
    ui.wait(terminal, timeout=max(.001, deadline - time.monotonic()))
    final = snapshots[-1]
    valid = _background_result_valid(final, expected_ids)
    valid = valid and _file_hashes(run, student_only=True) == before_files and _effects(run) == before_effects
    valid = valid and _background_network_valid(run.network[before_network:], len(expected_ids))
    # Actual DOM must expose each final outcome and its error, not just an API.
    for row in final.get('course_results', []):
        selector = '[data-background-sync] [data-sync-course="' + row['course_id'] + '"][data-sync-state="' + row['state'] + '"]'
        ui.wait(lambda selector=selector: ui.visible(selector), timeout=10)
        if row.get('error') and row['error'] not in ui.text(selector):
            text = ui.text(selector)
            mapped = bool(re.search(r'account_unconfirmed|revision|快照.*未确认|快照.*已更新', row['error'], re.I))
            valid = valid and mapped and '账号目录版本核对' in text and '重试' in text
        if row.get('failure_id') and row['failure_id'] not in ui.text(selector):
            valid = False
    run.actions.append({'operation': 'native-background-incremental-observation', 'accepted': accepted,
        'snapshots': snapshots, 'foreground_course_id': course_id,
        'observed_elapsed_seconds': time.monotonic() - started,
        'service_request_budget_per_course': _BACKGROUND_SERVICE_REQUESTS_PER_COURSE,
        'per_course_original_deadline_seconds': 90, 'checks_passed': valid})
    ui.screenshot('background-incremental-final-results-and-cached-course')
    if not valid:
        raise AssertionError('Background final results, student preservation or cost readback failed')
    return final


def _cancel_refresh(ui, accepted, timeout=90):
    """Cancel only the actual running task accepted by this native click."""
    if accepted.get('kind') != 'sync' or accepted.get('state') != 'running':
        raise ResourceBlocked('actual-running-native-sync-required-for-cancel')
    background = accepted.get('scope') == 'background'
    selector = 'recording-cancel-background' if background else 'recording-cancel-sync'
    path = '/api/recordings/task?scope=background' if background else '/api/recordings/task'
    ui.wait(lambda: ui.visible('[data-action="' + selector + '"]'), timeout=10)
    active = ui.request_read(path)
    if background:
        ui.wait(lambda: bool(ui.request_read(path).get('course_results')), timeout=30)
        active = ui.request_read(path)
    if active.get('kind') != 'sync' or active.get('state') != 'running':
        raise ResourceBlocked('native-sync-finished-before-cancel')
    if background and active.get('id') != accepted.get('id'):
        raise AssertionError('Refuse to cancel a different background task')
    from urllib.parse import urlsplit
    with ui.page.expect_response(lambda response: urlsplit(response.url).path == '/api/recordings/cancel-sync'
            and response.request.method == 'POST', timeout=30000) as captured:
        _action(ui, selector)
    response = captured.value
    if response.status != 200 or response.json().get('kind') != 'sync':
        raise AssertionError('Actual native sync cancellation was rejected')
    result = _task(ui, timeout=timeout, path=path)
    if result.get('state') != 'cancelled':
        raise AssertionError('Actual native sync did not reach cancelled state')
    ui.wait(lambda: (ui.visible('[data-background-sync][data-background-state="cancelled"]') if background
                    else '目录更新已取消' in ui.text('body')), timeout=10)
    return result


def _visible_failure(run, ui, task):
    error, failure_id = task.get('error'), task.get('failure_id')
    if not isinstance(error, str) or not error or not isinstance(failure_id, str) or not failure_id:
        return False
    ui.wait(lambda: failure_id in ui.text('body'), timeout=10)
    body = ui.text('body')
    mapped = bool(re.search(r'account_unconfirmed|revision|快照.*未确认|快照.*已更新', error, re.I))
    visible = (error in body if not mapped else
               '账号目录版本核对未完成或已变化' in body and '已有内容保留' in body and '重试' in body)
    return (task.get('state') in {'failed', 'partial'} and visible and failure_id in body
        and failure_id in run.observe()['failure_ids']
        and (mapped or any(word in error for word in ('重试', '检查', '重新', '稍后', '清理'))))


def _notion_catalog_action(run, ui, expected_ids):
    """UI 'save existing course link' initiates the exact catalog-sync route."""
    resource = run.need('catalog.existing_course_link')
    _authorized_home(run, resource['page_id'])
    if not re.fullmatch(r'https://(?:www\.)?notion\.so/[A-Za-z0-9_-]+', resource['url']):
        raise ResourceBlocked('real-existing-test-course-page-url-required')
    if resource['page_id'].replace('-', '').lower() not in resource['url'].replace('-', '').lower():
        raise ResourceBlocked('existing-test-course-url-identity-mismatch')
    _open(ui, resource['course_id'])
    previous = ui.request_read('/api/recordings/task')
    if previous.get('state') == 'running':
        _task(ui)
    if ui.visible('[data-action="campus-course-settings"]'):
        _action(ui, 'campus-course-settings')
    else:
        ui.click('.campus-settings > summary')
    ui.fill('#campus-existing-notion-url', resource['url'])
    observation_before = (len(run.network), _file_hashes(run, student_only=True), _effects(run))
    from urllib.parse import urlsplit
    with ui.page.expect_response(lambda response:
            urlsplit(response.url).path == '/api/campus/notion/sync'
            and response.request.method == 'POST', timeout=30000) as started:
        _action(ui, 'campus-save-existing-notion')
    response = started.value
    if response.status != 200:
        raise AssertionError('Native Notion catalog sync rejected')
    accepted = response.json()
    if not isinstance(accepted, dict) or accepted.get('scope') != 'background':
        raise AssertionError('Native Notion sync response does not identify background task')
    task = _background_refresh(run, ui, resource['course_id'], expected_ids,
                               accepted=accepted, observation_before=observation_before)
    ui.screenshot('exact-native-notion-catalog-sync')
    return task


def _open(ui, cid):
    # Portal IDs must never be interpolated as arbitrary CSS.
    if not re.fullmatch(r'_[0-9]+_[0-9]+', cid):
        raise ValueError('Expected an official teaching-portal course identifier')
    _action(ui, 'open-dashboard')
    _action(ui, 'open-campus-course', '[data-course="' + cid + '"]')
    ui.wait(lambda: ui.visible('.course-hero[data-course-id="' + cid + '"] h1'))
    return ui.request_read('/api/campus/courses/' + quote(cid, safe=''))


def _login(run, ui, account='primary'):
    if ui.visible('.sidebar [data-block="quota"]'):
        _action(ui, 'logout')
        ui.wait(lambda: ui.visible('#auth-email'))
    ui.fill('#auth-email', run.credential(account + '_email'))
    ui.fill('#auth-password', run.credential(account + '_password'))
    _action(ui, 'auth-submit')
    ui.wait(lambda: ui.visible('.sidebar [data-block="quota"]'))
    return ui.request_read('/api/auth/me')


def _authorized_home(run, home_id):
    allowed = str(run.need('learning.notion_test_parent_id')).replace('-', '').lower()
    seen = set()
    current = str(home_id)
    for _ in range(16):
        normalized = current.replace('-', '').lower()
        if normalized == allowed:
            return
        if normalized in seen or not re.fullmatch(r'[0-9a-f]{32}', normalized):
            break
        seen.add(normalized)
        page = run.oracle.get('notion', '/v1/pages/' + quote(current, safe=''))
        parent = page.get('parent', {})
        if page.get('archived') or page.get('in_trash') or parent.get('type') != 'page_id':
            break
        current = parent.get('page_id', '')
    raise ResourceBlocked('shared-notion-home-outside-authorized-test-parent')


def _catalog_destination(run, ui, data):
    """Identify the sync target independently; preserved legacy files are not it."""
    data = Path(data).resolve()
    root = Path(run.need('test_root')).resolve()
    if not data.is_relative_to(root) or data == root:
        raise ResourceBlocked('catalog-destination-data-not-isolated')
    workspace_file = data / 'notion-workspace-v2.json'
    workspace = json.loads(workspace_file.read_text('utf-8')) if workspace_file.exists() else None
    field = 'notion_workspace_v2' if workspace else 'notion_home'
    local = workspace or json.loads((data / 'notion-learning-home.json').read_text('utf-8'))
    canonical = run.oracle.profile()['profile'].get(field)
    keys = ('id', 'url', 'parent_id')
    identity = {key: local[key] for key in keys}
    if not isinstance(canonical, dict) or canonical != identity:
        raise ResourceBlocked('catalog-destination-account-and-local-identity-disagree')
    _authorized_home(run, identity['id'])
    if ui.page is None:
        raise ResourceBlocked('native-catalog-destination-response-observer-required')
    from urllib.parse import urlsplit
    with ui.page.expect_response(lambda response: response.request.method == 'GET'
            and urlsplit(response.url).path == '/api/campus/notion/parents', timeout=180000) as capture:
        _action(ui, 'campus-account-settings')
    response = capture.value
    if response.status != 200:
        raise ResourceBlocked('catalog-destination-native-parent-readback-failed')
    native = response.json()
    selected = native.get('workspace' if workspace else 'home')
    if not isinstance(selected, dict) or any(selected.get(key) != identity[key] for key in keys):
        raise ResourceBlocked('catalog-destination-native-and-account-identity-disagree')
    selector = ('.workspace-controls [data-action="campus-open-summary"]'
                if workspace else '.campus-home-location [data-action="campus-open-home"]')
    expected_label = '打开新版主页' if workspace else '打开主页'
    named = ui.page.locator(selector).filter(has_text=re.compile('^' + re.escape(expected_label) + '$'))
    ui.wait(lambda: named.count() == 1 and named.is_visible(), timeout=180)
    controls = ui.page.locator(selector).evaluate_all('(nodes) => nodes.map(n => ({url: n.dataset.url, label: n.textContent.trim()}))')
    matches = [row for row in controls if row['label'] == expected_label]
    if len(matches) != 1 or matches[0]['url'] != identity['url']:
        raise ResourceBlocked('catalog-destination-native-visible-link-disagrees')
    ui.screenshot('native-catalog-destination-account-identity')
    return identity


def _catalog_scope_preflight(run):
    # Login/bootstrap can auto-sync before our next explicit click. Validate
    # account home targets before launching that native application.
    profile = run.oracle.profile()['profile']
    for key in ('notion_home', 'notion_workspace_v2'):
        home = profile.get(key)
        if isinstance(home, dict) and home.get('id'):
            _authorized_home(run, home['id'])
    test_root = Path(run.need('test_root')).resolve()
    for instance in run.config.get('instances', {}).values():
        app_dir = Path(instance['app_dir']).resolve()
        if app_dir == test_root or not app_dir.is_relative_to(test_root):
            raise ResourceBlocked('instance-data-not-isolated-before-home-preflight')
        for name in ('notion-learning-home.json', 'notion-workspace-v2.json'):
            path = app_dir / 'data' / name
            if path.exists():
                home = json.loads(path.read_text('utf-8'))
                if isinstance(home, dict) and home.get('id'):
                    _authorized_home(run, home['id'])


def _catalog_setup(run, ui, *, selected_only=False):
    ui.wait(lambda: ui.visible('#auth-email') or ui.visible('.sidebar [data-block="quota"]'))
    # The release-check render can show the login form before the initial
    # quota/session load completes. Never re-login an already active session.
    signed_in = ui.request_read('/api/auth/me')
    if signed_in.get('active') is True:
        ui.wait(lambda: ui.visible('.sidebar [data-block="quota"]'))
    else:
        _login(run, ui)
        signed_in = ui.request_read('/api/auth/me')
    if signed_in.get('email_verified') is not True:
        raise ResourceBlocked('verified-product-account-required-for-native-catalog')
    if signed_in.get('email', '').lower() != run.credential('primary_email').lower():
        raise ResourceBlocked('isolated-profile-signed-in-to-wrong-account')
    courses = ui.request_read('/api/campus/courses')
    if not courses.get('campus_configured'):
        _action(ui, 'campus-account-settings')
        ui.fill('#recording-username', run.credential('portal_username'))
        ui.fill('#recording-password', run.credential('portal_password'))
        _action(ui, 'recording-save-credentials')
        ui.wait(lambda: ui.request_read('/api/campus/courses').get('campus_configured') is True)
    if not ui.request_read('/api/connection').get('connected'):
        _action(ui, 'connect-notion')
        _oauth_browser(run)
        ui.wait(lambda: ui.request_read('/api/connection').get('connected') is True)
    # Do not auto-create a replacement home to make a legacy migration pass.
    # Its authentic prior identity must already be present in this isolated
    # profile; first-time creation is covered by the independent notes journey.
    parents = ui.request_read('/api/campus/notion/parents' + ('?selected_only=true' if selected_only else ''))
    if not isinstance(parents.get('home'), dict) or not parents['home'].get('id'):
        raise ResourceBlocked('authentic-existing-notion-home-required-for-legacy-regression')
    _authorized_home(run, parents['home']['id'])
    ui.wait(lambda: ui.visible('.sidebar [data-action="open-dashboard"]'))


def _field_equal(actual, expected, keys):
    return all(actual.get(key) == expected.get(key) for key in keys)


def _source_rows(rows, identity):
    return {str(row.get(identity) or row.get('source_id')): row for row in rows}


def _file_hashes(run, instance='primary', student_only=False):
    rows = run.observe()['local_files']
    return {row['path']: row['sha256'] for row in rows if row['instance'] == instance
            and (not student_only or row['path'].endswith(('.md', '/transcript.json')))}


def _cache_hashes(run):
    root = Path(run.need('instances.primary.app_dir')) / 'data'
    files = _file_hashes(run)
    result = {}
    for metadata in root.glob('*/course.json'):
        value = json.loads(metadata.read_text('utf-8'))
        course_id = value.get('course_id')
        if not isinstance(course_id, str):
            raise AssertionError('Invalid actual cache course identity')
        prefix = 'data/' + metadata.parent.name + '/'
        result[course_id] = {path: digest for path, digest in files.items()
                             if path.startswith(prefix)}
    return result


def _oauth_browser(run, accept=True):
    browser = run.external_page()
    if browser.page is None:
        raise ResourceBlocked('owned-oauth-browser-cdp-required')
    from urllib.parse import parse_qs, urlsplit
    from pku_sync.config import Settings
    client_id = run.config.get('account', {}).get('notion_oauth_client_id') \
        or Settings(_env_file=None).notion_oauth_client_id
    # The app opens its real OAuth URL in the system browser. Attach only to a
    # tab in the independently owned browser, never create a fake redirect.
    def oauth_page():
        from .oauth_provider_observation import matches_authorization_page
        return next((page for page in reversed(browser.page.context.pages)
                     if matches_authorization_page(page.url, client_id)), None)
    try:
        page = browser.wait(oauth_page, timeout=30)
    except AssertionError:
        raise ResourceBlocked('real-oauth-tab-not-in-owned-system-browser') from None
    if not accept:
        configured = run.config.get('account', {}).get('oauth_cancel_selector')
        candidates = [configured] if configured else [
            'button:text-is("Cancel")', '[role="button"]:text-is("Cancel")',
            'button:text-is("取消")', '[role="button"]:text-is("取消")']
        selector = next((s for s in candidates if page.locator(s).count() == 1 and page.locator(s).is_visible()), None)
        if selector is None:
            raise ResourceBlocked('real-oauth-denial-control-unobserved')
    parameters = parse_qs(urlsplit(page.url).query)
    state = parameters.get('state', [''])[0]
    redirect = urlsplit(parameters.get('redirect_uri', [''])[0])
    if (not state or redirect.scheme != 'http' or redirect.hostname != 'localhost'
            or not redirect.port or redirect.path != '/callback' or redirect.query or redirect.fragment
            or redirect.username or redirect.password):
        raise ResourceBlocked('real-oauth-provider-state-and-owned-callback-required')
    from .core import endpoint_owned_by
    endpoint_owned_by('http://127.0.0.1:' + str(redirect.port), run.processes['primary'].pid)
    callbacks = []
    provider_responses = []
    def observe_response(response):
        from .oauth_provider_observation import page_identities
        parts = urlsplit(response.url)
        if parts.hostname == 'app.notion.com' and parts.path.startswith('/api/v3/') and response.status == 200:
            try:
                body = response.body()
                pages = page_identities(json.loads(body))
                if pages:
                    provider_responses.append({'host':parts.hostname, 'path':parts.path, 'status':200,
                        'body_sha256':hashlib.sha256(body).hexdigest(), 'pages':pages})
            except Exception:
                pass  # Missing real identities must fail the scope binding below.
    def observe(request):
        parts = urlsplit(request.url)
        values = parse_qs(parts.query)
        if ((parts.scheme, parts.hostname, parts.port, parts.path)
                == (redirect.scheme, redirect.hostname, redirect.port, redirect.path)):
            browser._request_event(request)
            callbacks.append({'state': values.get('state', [''])[0],
                              'code_present': bool(values.get('code', [''])[0]),
                              'error': values.get('error', [''])[0]})
    browser.page.context.on('request', observe)
    browser.page.context.on('response', observe_response)
    try:
        browser.page = page
        if accept:
            from .oauth_provider_scope import root_only_grant
            parent = run.oracle.get('notion', '/v1/pages/' + run.need('learning.notion_test_parent_id'))
            root_only_grant(run, browser, provider_responses, parent)
        else:
            browser.click(selector)
        browser.wait(lambda: bool(callbacks), timeout=30)
    finally:
        browser.page.context.remove_listener('request', observe)
        browser.page.context.remove_listener('response', observe_response)
    expected = len(callbacks) == 1 and callbacks[0]['state'] == state
    if accept:
        expected = expected and callbacks[0]['code_present'] and not callbacks[0]['error']
    else:
        expected = expected and not callbacks[0]['code_present'] and callbacks[0]['error'] in {
            'access_denied', 'user_cancelled', 'user_canceled'}
    run.actions.append({'actual_provider_oauth_callback': {'passed': bool(expected),
        'accepted': accept, 'callback_count': len(callbacks),
        'state_sha256': hashlib.sha256(state.encode()).hexdigest(), 'callback_fabricated': False}})
    if not expected:
        raise AssertionError('real_oauth_callback_state_or_outcome_mismatch')


def _ledger(run):
    # Require a real cumulative operation history endpoint, not configured
    # finished/pass flags or remaining balance pretending to be a ledger.
    value = run.oracle.get('account', run.need('account.operation_ledger_path'))
    if not isinstance(value, dict):
        raise AssertionError('Operation ledger must be a real service object')
    return value


def _layout_probe(run, ui, course_id):
    _action(ui, 'open-welcome')
    ui.wait(lambda: ui.visible('#welcome-heading'))
    welcome = ui.text('#welcome-heading')
    ui.click('[data-action="welcome-skip"]')
    _action(ui, 'open-dashboard')
    dashboard = ui.visible('[data-action="open-campus-course"][data-course="' + course_id + '"]')
    _open(ui, course_id)
    if ui.page is None:
        raise ResourceBlocked('mac-native-layout-keyboard-observation-required')
    geometry = ui.page.evaluate('''() => {
      const side=document.querySelector('.sidebar').getBoundingClientRect();
      const main=document.querySelector('.main').getBoundingClientRect();
      return {dpi:window.devicePixelRatio*96, sidebar_left:side.right<=main.left+1,
        horizontal_overflow:document.documentElement.scrollWidth>window.innerWidth,
        centers:Array.from(document.querySelectorAll('.campus-centers a'))
          .map(a=>a.getAttribute('href').substring(1))}; }''')
    from .windows_dpi import read_window_dpi, matches_native_dpi
    native_pid = run.runtime(ui.name)['pid']
    os_dpi = read_window_dpi(native_pid)
    geometry.update(os_window_dpi=os_dpi,
                    os_dpi_verified=matches_native_dpi(geometry['dpi'], os_dpi, native_pid))
    if not geometry['os_dpi_verified']:
        diagnostic = run.case_dir / 'native-dpi-mismatch.json'
        diagnostic.write_text(json.dumps(geometry, ensure_ascii=False, indent=2), 'utf-8')
        run.ui_evidence.append(diagnostic)
        ui.screenshot('native-windows-webview-os-dpi-mismatch')
        raise ResourceBlocked('native-webview-dpi-disagrees-with-windows-monitor-or-window')
    supported = run.need('account.supported_dpi')
    if not isinstance(supported, list) or not supported:
        raise ResourceBlocked('supported-native-dpi-list-required')
    if any(type(dpi) not in {int, float} or dpi <= 0 for dpi in supported):
        raise ResourceBlocked('supported-native-dpi-list-invalid')
    declared = run.scenario.get('supported_dpi', [96, 120, 144])
    if supported != declared:
        raise ResourceBlocked('configured-dpi-does-not-match-reviewed-scenario-scope')
    reached = set()
    for _ in range(100):
        run.actions.append({'instance': ui.name, 'operation': 'native-keyboard', 'key': 'Tab'})
        ui.page.keyboard.press('Tab')
        focus = ui.page.evaluate('''() => {
          const a=document.activeElement, r=a.getBoundingClientRect();
          return {href:a.getAttribute('href'), visible:r.top>=0 && r.bottom<=innerHeight};
        }''')
        if focus['visible'] and (focus.get('href') or '').startswith('#course-'):
            reached.add(focus['href'][1:])
        if set(geometry['centers']) <= reached:
            break
    _action(ui, 'campus-open-center', '[data-center="notices"]')
    ui.page.mouse.wheel(0, 5000)
    run.actions.append({'instance': ui.name, 'operation': 'native-wheel', 'delta_y': 5000})
    last = ui.page.evaluate('''() => {
      const node=document.querySelector('#course-notices'), r=node.getBoundingClientRect();
      return r.top<innerHeight && r.bottom>0;
    }''')
    metrics = {**geometry, 'welcome': bool(welcome.strip()) and dashboard,
               'keyboard_reachable': set(geometry['centers']) <= reached,
               'scroll_reachable': last}
    return _aggregate_dpi(run, ui, metrics, supported)


def _dpi_ok(value):
    from .windows_dpi import matches_native_dpi
    os_dpi = value.get('os_window_dpi')
    return value.get('os_dpi_verified') is True and isinstance(os_dpi, dict) \
        and matches_native_dpi(value.get('dpi'), os_dpi, os_dpi.get('pid')) \
        and value.get('welcome') is True and value.get('sidebar_left') is True \
        and value.get('keyboard_reachable') is True and value.get('scroll_reachable') is True \
        and value.get('horizontal_overflow') is False and value.get('centers') == [
            'course-assignments', 'course-notices', 'course-materials']


def matches_os_dpi_identity(metrics, pid):
    from .windows_dpi import matches_native_dpi
    return (isinstance(metrics, dict) and metrics.get('os_dpi_verified') is True
            and matches_native_dpi(metrics.get('dpi'), metrics.get('os_window_dpi'), pid))


def _aggregate_dpi(run, ui, metrics, supported):
    """Aggregate measured native DPI probes, never configured passed flags.

    Prior probes may come from a failed/blocked run (missing other DPI). They
    must be runner evidence bound to this exact source/candidate installation,
    with an intact physical-window screenshot. CI still authenticates runners.
    """
    from scripts.product_e2e_gate import source_digest
    runtime = run.runtime(ui.name)
    if not matches_os_dpi_identity(metrics, runtime['pid']):
        raise ResourceBlocked('native-dpi-observation-missing-owned-os-identity')
    digest = run.context.get('source_digest') or source_digest(Path(run.context['root']))
    screenshot = ui.screenshot('native-dpi-' + str(round(metrics['dpi'])))
    observation = {'kind': 'native-dpi-observation', 'metrics': metrics,
                   'target': run.context['target'], 'version': runtime['version'],
                   'artifact_sha256': run.context['artifact_sha256'], 'source_digest': digest,
                   'installed_sha256': runtime['installed_sha256'],
                   'native_pid': runtime['pid'],
                   'observed_at': datetime.now(timezone.utc).isoformat(),
                   'screenshot': screenshot.name,
                   'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()}
    path = run.case_dir / 'native-dpi-observation.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(observation, stream, ensure_ascii=False)
    run.ui_evidence.append(path)
    observations = [observation]
    for report_file in run.config.get('account', {}).get('dpi_reports', []):
        report_path = Path(report_file).resolve()
        report = json.loads(report_path.read_text('utf-8'))
        if (report.get('entry') != 'installed-native'
                or report.get('artifact_sha256') != observation['artifact_sha256']
                or report.get('source_digest') != digest or report.get('target') != observation['target']):
            raise ResourceBlocked('native-dpi-report-candidate-or-source-mismatch')
        for case in report.get('cases', []):
            if case.get('id') != 'JOURNEY-06':
                continue
            for evidence in case.get('evidence', []):
                evidence_path = (report_path.parent / evidence['path']).resolve()
                if not evidence_path.is_relative_to(report_path.parent):
                    raise ResourceBlocked('native-dpi-evidence-path-outside-report')
                if evidence_path.name != 'native-dpi-observation.json':
                    continue
                if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence.get('sha256'):
                    raise ResourceBlocked('native-dpi-evidence-hash-mismatch')
                prior = json.loads(evidence_path.read_text('utf-8'))
                if any(prior.get(key) != observation[key] for key in (
                        'kind', 'target', 'version', 'artifact_sha256', 'source_digest', 'installed_sha256')):
                    raise ResourceBlocked('native-dpi-observation-identity-mismatch')
                if not matches_os_dpi_identity(prior['metrics'], prior['native_pid']):
                    raise ResourceBlocked('native-dpi-prior-observation-missing-owned-os-identity')
                when = datetime.fromisoformat(prior['observed_at'])
                if when.tzinfo is None or not datetime.now(timezone.utc) - timedelta(hours=24) <= when <= datetime.now(timezone.utc):
                    raise ResourceBlocked('native-dpi-observation-stale')
                image = (evidence_path.parent / prior['screenshot']).resolve()
                if (image.parent != evidence_path.parent
                        or hashlib.sha256(image.read_bytes()).hexdigest() != prior['screenshot_sha256']):
                    raise ResourceBlocked('native-dpi-physical-screenshot-invalid')
                observations.append(prior)
    missing = [dpi for dpi in supported if not any(
        abs(float(row['metrics']['dpi']) - float(dpi)) <= 1 for row in observations)]
    if missing:
        raise ResourceBlocked('native-os-dpi-controller-or-prior-measured-runs-required')
    # Keep a measured failure false; another run at the same DPI cannot erase it.
    return {**metrics, 'dpi_coverage_ok': all(_dpi_ok(row['metrics']) for row in observations),
            'measured_dpi': sorted({row['metrics']['dpi'] for row in observations})}


def _effects(run):
    ledger = _ledger(run)
    for key in ('charged_points', 'charged_seconds', 'submissions'):
        _required(run, ledger.get(key), 'operation-ledger.' + key)
    media = {path: digest for path, digest in _file_hashes(run).items()
             if path.endswith(('.mp4', '.mp3', '.m4a', '.wav', '.webm'))}
    return {key: ledger[key] for key in ('charged_points', 'charged_seconds', 'submissions')} \
        | {'media_hashes': media}


def run_catalog(run):
    _catalog_scope_preflight(run)
    ui = run.session()
    _catalog_setup(run, ui)
    startup = ui.request_read('/api/recordings/task')
    if startup.get('state') == 'running':
        startup = _task(ui)
    if startup.get('state') == 'failed' and '有效课程 revision' in startup.get('error', ''):
        ui.screenshot('startup-real-course-revision-failure')
        run.check('legacy-extra-courses-do-not-break-valid-course-sync', False,
                  {'trigger': 'installed-native-startup', 'failure_id': startup.get('failure_id'),
                   'error': 'invalid-course-revision'})
    before = ui.request_read('/api/campus/courses').get('courses', [])
    before_cache = _cache_hashes(run)
    course_id = run.need('catalog.resilience_course_id')
    before_effects = _effects(run)
    before_student = _file_hashes(run, student_only=True)
    task = _refresh_course(ui, course_id)
    if _effects(run) != before_effects or _file_hashes(run, student_only=True) != before_student:
        raise AssertionError('Metadata refresh changed media, student content, billing or submissions')
    after_selected = _cache_hashes(run)
    if ({cid: sha for cid, sha in before_cache.items() if cid != course_id}
            != {cid: sha for cid, sha in after_selected.items() if cid != course_id}):
        raise AssertionError('Selected refresh modified another course cache')
    if task.get('summary_created', 0) or task.get('summary_existing', 0):
        raise AssertionError('Metadata refresh implicitly generated or scanned course summaries')
    if task.get('state') == 'failed':
        ui.screenshot('native-catalog-refresh-failure')
        # Capture the real failure before a later resource blocker, e.g.
        # independent portal lesson visibility, can interrupt this journey.
        run.check('legacy-extra-courses-do-not-break-valid-course-sync', False,
                  {'trigger': 'native-update-catalog', 'failure_id': task.get('failure_id'),
                   'revision_error': '有效课程 revision' in task.get('error', '')})
    # Explicitly exercise the route that produced the user's legacy error.
    # The normal metadata refresh is also retained, but is not substituted for
    # /api/campus/notion/sync evidence if it exits before reaching Notion.
    portal = run.oracle.portal_courses()
    if isinstance(portal, dict):
        portal = _required(run, portal.get('courses'), 'portal.courses')
    expected_background_ids = [row.get('id', row.get('course_id')) for row in portal]
    if not expected_background_ids or any(not re.fullmatch(r'_[0-9]+_[0-9]+', cid or '') for cid in expected_background_ids):
        raise AssertionError('Independent official background scope missing')
    # Cancellation and same-process resume must preserve every completed result.
    cancelled = _cancel_refresh(ui, _start_refresh(ui))
    if not _background_result_valid(cancelled, expected_background_ids, cancelled=True):
        raise AssertionError('Background cancellation omitted a course or claimed false completion')
    retained_done = {row['course_id']: row for row in cancelled['course_results'] if row['state'] == 'done'}
    background = _background_refresh(run, ui, course_id, expected_background_ids,
                                     resume=True, retained=retained_done)
    if not any(row['course_id'] == course_id and row['state'] == 'done' for row in background['course_results']):
        raise AssertionError('Valid selected course was blocked by an unrelated background failure')
    catalog_task = _notion_catalog_action(run, ui, expected_background_ids)
    run.check('legacy-extra-courses-do-not-break-valid-course-sync',
              catalog_task.get('state') in {'done', 'partial'},
              {'trigger': 'native-course-link-save-to-notion-sync',
               'kind': catalog_task.get('kind'), 'failure_id': catalog_task.get('failure_id'),
               'revision_error': '有效课程 revision' in catalog_task.get('error', '')})
    displayed = ui.request_read('/api/campus/courses').get('courses', [])
    rows = _source_rows(displayed, 'id')
    official = {row.get('id', row.get('course_id')):
                {**row, 'id': row.get('id', row.get('course_id')),
                 'title': row.get('title', row.get('name'))} for row in portal}
    run.check('course-term-and-source-ids-match-independent-portal',
              len(rows) == len(displayed) and set(official) <= set(rows)
              and all(_field_equal(rows[cid], expected, ('title', 'code', 'term'))
                      for cid, expected in official.items()),
              'Native refresh compared to independently authenticated portal read')
    canonical = run.oracle.catalog()
    canonical = canonical.get('courses', []) if isinstance(canonical, dict) else canonical
    envelopes = {row['snapshot']['course_id']: row for row in canonical}
    zero_revision = set(rows) - set(envelopes)
    # A historical positive account snapshot is not proof of this operation's
    # fresh official source. Failed/partial courses remain explicit read-only.
    unconfirmed = {cid for cid, row in rows.items() if row.get('read_only') is True}
    revision_ok = all(type(row.get('revision')) is int and row['revision'] > 0
                      and isinstance(row.get('snapshot'), dict) for row in canonical)
    for cid in zero_revision:
        envelope = run.oracle.get('account', '/v1/catalog/' + quote(cid, safe=''))
        revision_ok = revision_ok and envelope.get('revision') == 0 \
            and type(envelope.get('revision')) is int and envelope.get('snapshot') is None
    run.check('positive-and-zero-revisions-handled-per-course',
              revision_ok and zero_revision <= unconfirmed,
              'Independent per-course envelopes and actual installed read-only state')
    legacy = set(run.need('catalog.legacy_course_ids'))
    completed = {row['course_id'] for row in catalog_task['course_results'] if row['state'] == 'done'}
    run.check('legacy-extra-courses-do-not-break-valid-course-sync',
              bool(legacy) and legacy <= zero_revision and task.get('state') == 'done'
              and course_id in completed and completed <= set(envelopes)
              and all(rows[cid].get('read_only') is False for cid in completed),
              'Mixed legacy directory and verified courses completed native refresh')
    read_only_ok = True
    for cid in sorted(unconfirmed):
        detail = _open(ui, cid)
        text = ui.text('.main')
        read_only_ok = read_only_ok and detail.get('read_only') is True \
            and '只读' in text and ('刷新' in text or '核对' in text)
        # Disabled controls must be measured, not inferred from the GET payload.
        for lesson in detail.get('lessons', []):
            suffix = '[data-recording="' + lesson['id'] + '"]'
            if ui.visible('[data-action="campus-course-home"]'):
                _action(ui, 'campus-course-home')
            if ui.visible('[data-action="campus-open-lesson"]' + suffix):
                _action(ui, 'campus-open-lesson', suffix)
            for action in ('campus-process', 'campus-regenerate'):
                selector = '[data-action="' + action + '"]' + suffix
                if ui.visible(selector):
                    read_only_ok = read_only_ok and ui.attr(selector, 'disabled') is not None
    run.check('unconfirmed-course-read-only-and-actionable',
              bool(unconfirmed) and read_only_ok, 'Native controls and recovery copy checked')
    lessons_ok = assessments_ok = True
    september_seen = False
    for cid, expected in official.items():
        expected = {**expected, **run.oracle.portal_course(cid)}
        detail = _open(ui, cid)
        actual = detail.get('lessons', [])
        ids = [row.get('source_id') for row in actual]
        expected_lessons = _required(run, expected.get('lessons'), 'portal.lessons')
        expected_ids = [row.get('source_id') for row in expected_lessons]
        lessons_ok = lessons_ok and ids == expected_ids and len(ids) == len(set(ids)) \
            and all(_field_equal(a, e, ('title', 'date'))
                    for a, e in zip(actual, expected_lessons))
        september_seen = september_seen or ('发展心理学' in expected['title']
                        and any(row.get('date') == '2026-09-29' for row in actual))
        for section, identity, fields in (
                ('assignments', 'content_id', ('title', 'source', 'due_at', 'instructions')),
                ('announcements', 'source_id', ('title', 'posted_at', 'body_text'))):
            expected_section = _required(run, expected.get(section), 'portal.' + section)
            found = _source_rows(detail.get(section, []), identity)
            source = _source_rows(expected_section, identity)
            assessments_ok = assessments_ok and len(found) == len(detail.get(section, [])) \
                and len(source) == len(expected_section) and set(found) == set(source) \
                and all(_field_equal(found[key], row, fields) for key, row in source.items())
        expected_assessments = _required(run, expected.get('official_assessments'),
                                         'portal.official_assessments')
        actual_assessments = [row for row in detail.get('all_materials', [])
                              if row.get('kind') in {'测试', '考试', '测验', 'Test', 'Quiz'}]
        assessments_ok = assessments_ok and {
            row.get('content_id') for row in actual_assessments} == {
            row.get('content_id') for row in expected_assessments}
        ui.screenshot('course-' + cid)
    run.check('all-lessons-including-september29-not-missing-or-duplicated',
              bool(official) and lessons_ok and september_seen,
              'Every lesson identity/order compared; September 29 explicitly present')
    run.check('assignments-notices-official-quizzes-source-and-deadline-correct',
              bool(official) and assessments_ok, 'Official sources/deadlines and IDs compared')
    # Real renamed/same-name teaching courses are prerequisites, not fabricated data.
    rename = run.need('catalog.renamed_course')
    duplicates = run.need('catalog.same_name_course_ids')
    run.check('same-name-and-renamed-courses-retain-identities',
              len(duplicates) >= 2 and all(cid in rows for cid in duplicates)
              and len({rows[cid]['title'] for cid in duplicates}) == 1
              and rename['id'] in rows and rows[rename['id']]['title'] == rename['current_title']
              and rename['previous_title'] != rename['current_title']
              and rename['id'] in _source_rows(before, 'id'),
              'Real repeated titles and independently observed rename retain source IDs')
    empty_id = run.need('catalog.empty_course_id')
    unreadable_id = run.need('catalog.unreadable_course_id')
    empty = _open(ui, empty_id)
    unreadable = _open(ui, unreadable_id)
    retained = _cache_hashes(run)
    run.check('empty-distinct-from-unreadable-and-cache-retained',
              not empty.get('lessons') and not empty.get('read_only')
              and unreadable.get('read_only') is True
              and unreadable_id in before_cache
              and retained.get(unreadable_id) == before_cache[unreadable_id],
              'Confirmed empty course distinguished from failed source read')
    if run.mode == 'fault':
        _catalog_faults(run, ui)


def _catalog_faults(run, ui):
    from scripts.product_e2e_gate import FAULT_VARIANTS
    hashes = _cache_hashes(run)
    target = run.need('catalog.existing_course_link')['course_id']
    official = run.oracle.portal_courses()
    official = official.get('courses', []) if isinstance(official, dict) else official
    official_ids = {row.get('id', row.get('course_id')) for row in official}
    rows = ui.request_read('/api/campus/courses')['courses']
    alternatives = [row['id'] for row in rows if row['id'] in official_ids
                    and row['id'] != target and row.get('read_only') is False]
    if target not in official_ids or not alternatives:
        raise ResourceBlocked('two-real-official-courses-required-for-catalog-fault-isolation')
    missing = object()
    previous = run.config.get('catalog_fault_course_id', missing)
    run.config['catalog_fault_course_id'] = target
    try:
        _catalog_fault_variants(run, ui, target, alternatives[0], hashes)
    finally:
        if previous is missing:
            run.config.pop('catalog_fault_course_id', None)
        else:
            run.config['catalog_fault_course_id'] = previous


def _catalog_fault_variants(run, ui, target, valid_other, hashes):
    from scripts.product_e2e_gate import FAULT_VARIANTS
    for variant in FAULT_VARIANTS['JOURNEY-02']:
        hashes = _cache_hashes(run)
        with run.fault(variant):
            start = len(run.network)
            student_before = _file_hashes(run, student_only=True)
            task = _refresh_course(ui, target)
            rows = ui.request_read('/api/campus/courses')['courses']
            if variant in {'zero-null', 'legacy-extra-course'}:
                blocked = next(row for row in rows if row['id'] == target)
                ok = (task.get('state') in {'partial', 'failed'} and blocked.get('read_only') is True
                    and bool(task.get('error') or task.get('warnings'))
                    and _file_hashes(run, student_only=True) == student_before
                    and not any(row.get('boundary') == 'notion' and row.get('method') in {'POST','PATCH','DELETE'}
                        and row.get('path') != '/v1/search' and not row.get('path','').endswith('/query')
                        for row in run.network[start:]))
                valid = _refresh_course(ui, valid_other)
                ok = ok and valid.get('state') == 'done' and _open(ui, valid_other).get('read_only') is False
            else:
                ok = task.get('state') in {'failed', 'partial'} \
                    and _cache_hashes(run) == hashes \
                    and bool(task.get('error') or task.get('warnings'))
            run.check('positive-and-zero-revisions-handled-per-course', ok,
                      'Native boundary fault: ' + variant)
            run.fault_check(variant, ok, 'Native refresh and independent cache readback')
            ui.screenshot('catalog-fault-' + variant)


def run_identity(run):
    from .identity_live import (wrong_password, unverified_identity, expired_session,
                               refresh_oracle_identity, natural_errors_complete, verify_oauth_token_readback)
    natural_errors = {}
    registration_deferred = 'existing-verified-account-login-logout-and-oauth' in run.scenario['assertions']
    auth_key = ('existing-verified-account-login-logout-and-oauth' if registration_deferred
                else 'register-verify-login-logout-and-oauth')
    errors_key = ('wrong-expired-session-actionable' if registration_deferred
                  else 'wrong-expired-unverified-session-actionable')
    _catalog_scope_preflight(run)
    ui = run.session()
    runtime = run.runtime('primary')
    run.check('installed-native-version-and-single-instance',
              runtime.get('version') == run.context['version']
              and runtime.get('single_instance') is True
              and Path(runtime.get('process', {}).get('path', '')).resolve()
              == Path(run.need('instances.primary.installed_path')).resolve(),
              'OS executable identity/version and process inventory')
    before = _file_hashes(run, student_only=True)
    run.upgrade()
    run.restart('primary')
    after = _file_hashes(run, student_only=True)
    run.check('upgrade-and-restart-preserve-data',
              bool(before) and before == after
              and run.runtime('primary').get('version') == run.context['version'],
              'Actual installer upgrade and hashed student data before/after')
    ui = run.session()
    # Registration uses a dedicated disposable account and actual email delivery.
    if run.mode == 'live':
        natural_errors['wrong-password'] = wrong_password(run, ui, run.credential('primary_email'))
    _login(run, ui)
    refresh_oracle_identity(run, ui, run.credential('primary_email'))
    if not registration_deferred:
        _action(ui, 'logout')
        _action(ui, 'auth-mode-register')
        ui.fill('#auth-email', run.credential('registration_email'))
        password = run.credential('registration_password')
        ui.fill('#auth-password', password)
        ui.fill('#auth-password-confirm', password)
        _action(ui, 'auth-submit')
        ui.wait(lambda: ui.visible('.sidebar [data-action="refresh-verification"]'))
        refresh_oracle_identity(run, ui, run.credential('registration_email'))
        if run.mode == 'live':
            natural_errors['unverified'] = unverified_identity(run, ui, run.credential('registration_email'))
        if run.mode == 'fault':
            # Exercise the new account before the human verifies its real email.
            # The HTTP boundary observes the actual false flag unchanged; do not
            # require a third account or rewrite an already verified identity.
            with run.fault('unverified'):
                unverified = ui.request_read('/api/auth/me')
                ok = (unverified.get('active') is True
                      and unverified.get('email', '').strip().lower()
                          == run.credential('registration_email').strip().lower()
                      and unverified.get('email_verified') is False
                      and ui.visible('.sidebar [data-action="resend-verification"]'))
                ui.screenshot('identity-unverified-before-real-inbox-verification')
                run.check(errors_key, ok,
                          'Real newly registered unverified identity has visible verification recovery')
                run.fault_check('unverified', ok,
                                'Unchanged real account GET before the real email verification click')
        # Verification needs a real inbox click. It is never replaced by a backend
        # POST or configured success flag. Poll the real account while it happens.
        try:
            ui.wait(lambda: ui.request_read('/api/auth/me').get('email_verified') is True,
                    timeout=30)
        except AssertionError:
            raise ResourceBlocked('real-inbox-verification-click-required') from None
        _action(ui, 'refresh-verification')
        ui.wait(lambda: ui.request_read('/api/auth/me').get('email_verified') is True)
        refresh_oracle_identity(run, ui, run.credential('registration_email'))
    elif ui.request_read('/api/connection').get('connected') is True:
        # Reconnect only this isolated profile through the real provider flow.
        _action(ui, 'revoke-notion')
        ui.wait(lambda: ui.request_read('/api/connection').get('connected') is False)
    _action(ui, 'connect-notion')
    _oauth_browser(run)
    ui.wait(lambda: ui.request_read('/api/connection').get('connected') is True)
    verify_oauth_token_readback(run, ui)
    registered = ui.request_read('/api/auth/me')
    _action(ui, 'logout')
    ui.wait(lambda: ui.visible('#auth-email'))
    logout_visible = ui.visible('#auth-email')
    primary = _login(run, ui)
    refresh_oracle_identity(run, ui, run.credential('primary_email'))
    run.check(auth_key,
              registered.get('email_verified') is True and logout_visible
              and primary.get('email_verified') is True,
              'Real provider OAuth and logout/login; registration follows the reviewed scenario scope')
    second = None
    if 'new-device-pulls-all-supported-shared-settings-without-copying-device-private-data' in run.scenario['assertions']:
        profile = run.oracle.profile()
        second = run.session('secondary')
        _login(run, second)
        if not second.request_read('/api/connection').get('connected'):
            _action(second, 'connect-notion')
            _oauth_browser(run)
            second.wait(lambda: second.request_read('/api/connection').get('connected') is True)
        second_profile = second.request_read('/api/campus/notion/parents')
        shared = profile.get('profile', {})
        shared_home = shared.get('notion_workspace_v2', shared.get('notion_home', {}))
        from dotenv import dotenv_values
        primary_env = dotenv_values(Path(run.need('instances.primary.app_dir')) / '.env')
        secondary_env = dotenv_values(Path(run.need('instances.secondary.app_dir')) / '.env')
        secondary_private = [path for path in _file_hashes(run, 'secondary')
                             if path.endswith(('.mp4', '.mp3', '.m4a', '.wav', '/transcript.json'))]
        shared_ok = all(second_profile.get('workspace' if key == 'notion_workspace_v2' else
                                          'legacy_home', {}).get('id') == value.get('id')
                        for key, value in shared.items()
                        if key in {'notion_home', 'notion_workspace_v2'})
        run.check('new-device-pulls-all-supported-shared-settings-without-copying-device-private-data',
                  bool(shared_home.get('id'))
                  and second_profile.get('home', {}).get('id') == shared_home['id']
                  and shared_ok and not secondary_private
                  and bool(secondary_env.get('NOTION_TOKEN'))
                  and primary_env.get('NOTION_TOKEN') != secondary_env.get('NOTION_TOKEN')
                  and not secondary_env.get('PKU_PASSWORD'),
                  'Clean secondary installation reads shared identity; OS private inventory checked')
    before_quota = run.oracle.quota()
    previous_redemptions = _required(run, _ledger(run).get('redemptions'),
                                    'operation-ledger.redemptions')
    prior_ids = {row.get('id') for row in previous_redemptions}
    if None in prior_ids:
        raise ResourceBlocked('stable-operation-ledger-redemption-identities-required')
    _open_account_controls(ui)
    ui.fill('#code', run.credential('designated_redemption_code'))
    _action(ui, 'redeem')
    ui.wait(lambda: run.oracle.quota() != before_quota)
    quota = run.oracle.quota()
    ledger = _ledger(run)
    redemptions = _required(run, ledger.get('redemptions'), 'operation-ledger.redemptions')
    current_ids = {row.get('id') for row in redemptions}
    run.check('quota-and-designated-test-redemption-match-ledger',
              all(ui.request_read('/api/platform/quota').get(key) == quota.get(key)
                  for key in ('llm_points_remaining', 'transcribe_seconds_remaining'))
              and None not in current_ids and len(current_ids) == len(redemptions)
              and prior_ids <= current_ids and len(current_ids - prior_ids) == 1,
              'Designated redemption UI and independent account ledger readback')
    account2 = _login(run, ui, 'alternate')
    refresh_oracle_identity(run, ui, run.credential('alternate_email'))
    run.check('account-switch-no-cross-account-content',
              account2.get('email') == run.credential('alternate_email').lower()
              and ui.request_read('/api/campus/courses').get('courses') == []
              and not ui.visible('.recording-row'),
              'Alternate isolated account sees no previous account content')
    variants = ('wrong-password', 'expired-session', 'oauth-cancel') \
        if run.mode == 'fault' else ()
    for variant in variants:
        if variant != 'wrong-password':
            _login(run, ui)
            refresh_oracle_identity(run, ui, run.credential('primary_email'))
        if variant == 'oauth-cancel' and ui.request_read('/api/connection').get('connected') is True:
            # Production disconnect clears only this owned profile's local token
            # and memory cache. It does not revoke the provider grant or write Notion.
            _action(ui, 'revoke-notion')
            ui.wait(lambda: ui.request_read('/api/connection').get('connected') is False)
        with run.fault(variant):
            if variant == 'wrong-password':
                if ui.visible('.sidebar [data-block="quota"]'):
                    _action(ui, 'logout')
                ui.fill('#auth-email', run.credential('primary_email'))
                ui.fill('#auth-password', 'E2E-deliberately-wrong-password')
                _action(ui, 'auth-submit')
                ui.wait(lambda: ui.visible('#auth-help'))
                ok = ui.visible('#auth-email') and bool(ui.text('#auth-help'))
            elif variant == 'expired-session':
                run.restart('primary')
                ui = run.session()
                ui.wait(lambda: ui.visible('#auth-email'))
                ok = ui.visible('#auth-email') and not ui.visible('.recording-row')
            else:
                if not ui.visible('.sidebar [data-action="connect-notion"]'):
                    raise ResourceBlocked('dedicated-disconnected-oauth-profile-required')
                _action(ui, 'connect-notion')
                _oauth_browser(run, accept=False)
                ui.wait(lambda: ui.request_read('/api/connection').get('state') != 'connecting')
                ok = ui.request_read('/api/connection').get('connected') is False \
                    and ui.visible('.sidebar [data-action="connect-notion"]')
            ui.screenshot('identity-' + variant)
            run.check(errors_key, ok,
                      'Real request/OS fault has visible recovery: ' + variant)
            run.fault_check(variant, ok, 'Native visible error and recovery action')
    if run.mode == 'fault':
        for variant in ('switch-account', 'upgrade-restart'):
            with run.fault(variant):
                preserved = _file_hashes(run, student_only=True)
                if not preserved:
                    raise ResourceBlocked('identity_behavior_nonempty_student_files_required')
                if variant == 'switch-account':
                    _login(run, ui, 'alternate')
                    alternate = refresh_oracle_identity(run, ui, run.credential('alternate_email'))
                    isolated = (ui.request_read('/api/campus/courses').get('courses') == []
                                and not ui.visible('.recording-row'))
                    ui.screenshot('identity-real-alternate-account-empty-content')
                    _login(run, ui)
                    primary = refresh_oracle_identity(run, ui, run.credential('primary_email'))
                    ok = (isolated and alternate.get('email') != primary.get('email')
                          and preserved == _file_hashes(run, student_only=True))
                    run.actions.append({'actual_native_account_switch':{'passed':ok}})
                    assertion = 'account-switch-no-cross-account-content'
                else:
                    run.upgrade()
                    ui = run.session()
                    refresh_oracle_identity(run, ui, run.credential('primary_email'))
                    ok = (preserved == _file_hashes(run, student_only=True)
                          and run.runtime('primary').get('version') == run.context['version'])
                    run.actions.append({'actual_native_upgrade_restart':{'passed':ok}})
                    assertion = 'upgrade-and-restart-preserve-data'
                run.check(assertion, ok, 'Actual native behavior: ' + variant)
                run.fault_check(variant, ok, 'Native restart and account recovery')
    if run.mode == 'live':
        _login(run, ui)
        refresh_oracle_identity(run, ui, run.credential('primary_email'))
        ui, natural_errors['expired-session'] = expired_session(run, ui, run.credential('primary_email'))
        run.check(errors_key, natural_errors_complete(natural_errors, include_unverified=not registration_deferred),
                  'Actual unchanged wrong-password/unverified responses and independently proven natural session expiry')
    if second is not None:
        run.quit('secondary', require_graceful=True)
    run.quit('primary', require_graceful=True)
    if 'external' in run.processes:
        run.quit('external', require_graceful=True)
    inventory = run.observe()
    import httpx
    origins = [session.origin for session in (ui, second) if session is not None]
    ports_closed = True
    for origin in origins:
        try:
            httpx.get(origin + '/api/recordings/task', trust_env=False, timeout=1)
            ports_closed = False
        except httpx.TransportError:
            pass
    run.check('quit-releases-owned-processes-and-ports',
              all(not row.get('alive') for row in inventory['processes']) and ports_closed,
              'OS inventory after graceful native quit')


def _resilience_faults(run, variants, execute):
    for variant in variants:
        try:
            execute(variant)
        except ResourceBlocked as exc:
            # A real boundary guard remains a failed fault and a blocker.
            # Continue other required variants; never substitute a success.
            run.fault_check(variant, False, exc.code)
            run.blockers.append(exc.code)


def _same_official_assignment(before, after):
    # The HTTP page includes session/CSRF values, so its raw byte hash is a
    # transport receipt, not the assignment revision. Compare every parsed
    # authoritative field, retaining unknown/new fields in this comparison.
    for value in (before, after):
        if (not isinstance(value, dict) or value.get('authority') != 'course.pku.edu.cn'
                or value.get('source') != 'content-tree'
                or not re.fullmatch(r'_\d+_\d+', value.get('course_id', ''))
                or not re.fullmatch(r'_\d+_\d+', value.get('content_id', ''))
                or value.get('source_id') != 'pku:' + value['course_id'] + ':assignments:' + value['content_id']
                or type(value.get('identity_confirmed')) is not bool
                or type(value.get('form_present')) is not bool
                or not re.fullmatch(r'[0-9a-f]{64}', value.get('page_sha256', ''))):
            return False
    # Submission form availability remains an exact observed field. AI cost
    # readback/packaging does not require creating a new submission attempt.
    return {k: v for k, v in before.items() if k != 'page_sha256'} == {
        k: v for k, v in after.items() if k != 'page_sha256'}


def _resilience_cost_receipt(run):
    """Two actual selected-source UI operations, exact ledger/UI costs and reuse."""
    from .learning_journeys import (_prepare, _preview, _assignment_requirements_verified, _new_rows)
    cfg = run.need('resilience.assignment_cost')
    from urllib.parse import urlsplit
    note = urlsplit(cfg['notion_url'])
    if note.scheme != 'https' or note.hostname != 'www.notion.so':
        raise ResourceBlocked('cost-fixture-must-be-authorized-notion-page')
    _authorized_home(run, note.path.rsplit('/', 1)[-1].replace('-', ''))
    ui = run.session()
    course = _open(ui, cfg['course_id'])
    official = run.oracle.portal_assignment(cfg['course_id'], cfg['content_id'], require_history=False)
    target = next(row for row in course['assignments'] if row['content_id'] == cfg['content_id'])
    fields = ('course_id', 'content_id', 'source_id', 'due_at', 'instructions')
    if any({**target, 'course_id': course['id']}.get(key) != official.get(key) for key in fields):
        raise AssertionError('Cost operation target differs from independent official source')
    _action(ui, 'campus-open-center', '[data-center="assignments"]')
    before = _ledger(run)
    observations = []
    official_snapshots = [official]
    verification_inputs = []
    for index in range(2):
        # Recheck the official target before the retry; changed instructions
        # are a new operation and cannot count as receipt reuse.
        current_official = run.oracle.portal_assignment(cfg['course_id'], cfg['content_id'], require_history=False)
        official_snapshots.append(current_official)
        run.actions.append({'official_assignment_state_readback': {
            'changed_parsed_fields': sorted(key for key in set(official) | set(current_official)
                if key != 'page_sha256' and official.get(key) != current_official.get(key)),
            'authority': current_official.get('authority'),
            'identity_confirmed': current_official.get('identity_confirmed'),
            'form_present': current_official.get('form_present'),
            'original_page_sha256': official['page_sha256'], 'current_page_sha256': current_official['page_sha256']}})
        if not _same_official_assignment(official, current_official):
            raise AssertionError('Official source changed before cost receipt reuse')
        draft = _prepare(run, ui, cfg)
        preview = _preview(run, draft, official, cfg)
        after = _ledger(run)
        text = ui.text('[data-assignment-requirements]')
        current = re.search(r'本次用量：\s*([0-9]+(?:\.[0-9]+)?)\s*AI 点', text)
        original = re.search(r'原任务用量：\s*([0-9]+(?:\.[0-9]+)?)\s*AI 点', text)
        receipt = draft['native_requirements_response']
        verified = _assignment_requirements_verified(draft, official, receipt, before, after,
            current.group(1) if current else None, original.group(1) if original else None,
            ui.page.locator('[data-assignment-requirements] li').all_text_contents())
        verified = verified and preview['source_requirements_match'] and preview['content_match'] \
            and preview['sha256'] == draft['sha256']
        if index:
            verified = verified and receipt.get('reused') is True \
                and receipt.get('operation_id') == observations[0]['operation_id'] \
                and not _new_rows(before, after, 'settlements')
        if not verified:
            raise AssertionError('Selected native source, exact UI charge or reused settlement differs')
        receipt_fields = ('schema', 'source_grounding_verified', 'course_id', 'content_id',
            'requirements_sha256', 'official_instructions', 'due_at', 'operation_id', 'analysis',
            'points_charged', 'receipt_points_charged', 'reused')
        verification_inputs.append({'draft': {key: draft[key] for key in
            ('course_id', 'content_id', 'requirements_sha256')},
            'receipt': {key: receipt[key] for key in receipt_fields},
            'displayed_current': current.group(1) if current else None,
            'displayed_original': original.group(1) if original else None,
            'visible_quotes': ui.page.locator('[data-assignment-requirements] li').all_text_contents()})
        observations.append({'operation_id': receipt['operation_id'],
            'current_points': receipt['points_charged'], 'original_points': receipt['receipt_points_charged'],
            'reused': receipt['reused'], 'source_sha256': draft['requirements_sha256'],
            'archive_sha256': draft['sha256'], 'draft_id': draft['draft_id'],
            'archive_path': Path(draft['actual_path']).resolve().relative_to(
                Path(run.need('instances.primary.app_dir')).resolve() / 'data').as_posix()})
        ui.screenshot('resilience-selected-source-cost-' + str(index + 1))
        before = after
    run.check('ui-cost-and-reused-receipt-match-real-ledger', True,
              {'observations': observations, 'exact_selected_source_job_and_single_settlement_verified': True})
    # Preserve the actual selected operation receipts for independent auditing;
    # check() intentionally stores only a detail hash. No answer text or token
    # is included here, and an earlier record is never overwritten.
    evidence = run.case_dir / 'native-selected-source-cost-reuse.json'
    with evidence.open('x', encoding='utf-8') as stream:
        json.dump({'observations': observations, 'official_snapshots': official_snapshots,
                   'verification_inputs': verification_inputs, 'official_source_id': official['source_id'],
                   'official_course_id': official['course_id'],
                   'official_content_id': official['content_id']}, stream, ensure_ascii=False, indent=2)
    run.ui_evidence.append(evidence)
    return observations


def _verified_permit_receipt(local, actual, expected):
    """Independent GET must identify this exact durable request, never a sibling."""
    return (isinstance(actual, dict)
        and set(actual) == {'client_request_id', 'revision', 'permit_id', 'state'}
        and type(local.get('revision')) is int and local['revision'] > 0
        and isinstance(local.get('client_request_id'), str) and bool(local['client_request_id'])
        and actual['client_request_id'] == local['client_request_id']
        and type(actual['revision']) is int and actual['revision'] == local['revision']
        and isinstance(actual['permit_id'], str) and bool(actual['permit_id'])
        and ('permit_id' not in local or actual['permit_id'] == local['permit_id'])
        and expected in {'acquired', 'released'} and actual['state'] == expected)


def _permit_tree_state(tree):
    """Compare stored content, excluding audit and internal-file access metadata."""
    from copy import deepcopy
    from urllib.parse import unquote_plus, urlsplit, urlunsplit

    signing_fields = {'x-amz-algorithm', 'x-amz-credential', 'x-amz-date',
                      'x-amz-expires', 'x-amz-signedheaders', 'x-amz-signature',
                      'x-amz-security-token', 'expires', 'signature',
                      'key-pair-id', 'awsaccesskeyid'}

    def normalize_internal_files(item):
        if isinstance(item, list):
            for child in item:
                normalize_internal_files(child)
        elif isinstance(item, dict):
            if item.get('type') == 'file' and isinstance(item.get('file'), dict):
                file = item['file']
                file.pop('expiry_time', None)
                if isinstance(file.get('url'), str):
                    url = urlsplit(file['url'])
                    # Preserve asset path, origin and non-signing query bytes.
                    # External URLs are content and must never be normalized.
                    query = '&'.join(part for part in url.query.split('&')
                                     if unquote_plus(part.split('=', 1)[0]).lower()
                                     not in signing_fields)
                    file['url'] = urlunsplit(url._replace(query=query))
            for child in item.values():
                normalize_internal_files(child)

    value = deepcopy(tree)
    nodes = value['pages'] + [row['block'] for row in value['blocks']]
    for node in nodes:
        for key in ('request_id', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by'):
            node.pop(key, None)
    normalize_internal_files(value)
    return value


def _projection_states(data):
    return [json.loads(path.read_text('utf-8'))
            for path in sorted((data / '.projection-receipts').rglob('*.json'))]


def _update_workspace(ui):
    with ui.page.expect_response(lambda response: response.request.method == 'GET'
            and response.url.endswith('/api/campus/notion/parents'), timeout=180000) as parents:
        ui.click('.sidebar [data-action="campus-account-settings"]')
    assert parents.value.status == 200
    ui.wait(lambda: ui.visible('[data-action="campus-create-workspace"]'), timeout=180)
    with ui.page.expect_response(lambda response: response.request.method == 'POST'
            and response.url.endswith('/api/campus/notion/workspace'), timeout=900000) as capture:
        ui.click('[data-action="campus-create-workspace"]')
    return capture.value


def _resilience_permit_recovery(run, ui, variant):
    """Native workspace action, real lost response, own GET recovery and full tree."""
    from collections import Counter
    states, update = _projection_states, _update_workspace
    data = Path(run.need('instances.primary.app_dir')) / 'data'
    if states(data):
        raise ResourceBlocked('existing-owned-writer-requires-independent-review')
    home = run.oracle.profile()['profile']['notion_workspace_v2']
    _authorized_home(run, home['id'])
    writable = [row['id'] for row in ui.request_read('/api/campus/courses')['courses'] if not row['read_only']]
    if len(writable) < 2:
        raise ResourceBlocked('two-confirmed-courses-required-for-permit-unwind')
    # The byte-cloned local fixture can predate a legitimately published note.
    # Establish a current projection before freezing the no-change baseline;
    # otherwise normal status/link reconciliation is mistaken for data loss.
    if not getattr(run, '_permit_projection_fixture_ready', False):
        prepared = update(ui)
        assert prepared.status == 200 and prepared.json().get('active') is True
        assert prepared.json()['id'] == home['id'] and not states(data)
        run.actions.append({'operation': 'permit-recovery-fixture-projection-prepared',
                            'workspace_id': home['id']})
        run._permit_projection_fixture_ready = True
    run.config['projection_fault_course_id'] = writable[1]
    preserved = _file_hashes(run, student_only=True)
    before = _permit_tree_state(run.oracle.notion_tree(home['id']))
    directory = run.oracle.notion_course_directory(home['id'])
    network_start = len(run.network)
    with run.fault(variant):
        response = update(ui)
        assert response.status in {409, 502}, 'Native action must expose lost permit response'
        ui.screenshot('resilience-' + variant + '-before-restart')
    lost = [row for row in run.network[network_start:] if row.get('fault') == variant]
    assert lost and all(row.get('status') in {200, 201, 202}
        and row.get('upstream_body_sha256') for row in lost), 'Real successful permit response required'
    pending = states(data)
    phase = 'acquiring' if 'acquire' in variant else 'releasing'
    assert len(pending) == 1 and pending[0]['phase'] == phase and pending[0]['course_id'] == writable[1]
    local = pending[0]
    path = '/v1/catalog/' + quote(local['course_id'], safe='') + '/projection/requests/' + quote(local['client_request_id'], safe='')
    assert _verified_permit_receipt(local, run.oracle.get('account', path),
        'acquired' if phase == 'acquiring' else 'released')
    if phase == 'acquiring':
        assert not any(row.get('boundary') == 'notion' and row.get('method') in {'POST', 'PATCH', 'DELETE'}
            and row.get('path') != '/v1/search' and not row.get('path', '').endswith('/query')
            for row in run.network[network_start:]), 'Notion write before all permits prepared'
        assert _permit_tree_state(run.oracle.notion_tree(home['id'])) == before
        assert any(row.get('method') == 'POST' and row.get('path') == '/v1/catalog/' + writable[0] + '/projection/release'
            and row.get('status') == 200 and not row.get('fault') for row in run.network[network_start:])
    ui = run.restart('primary')
    ui.wait(lambda: ui.visible('.sidebar [data-block="quota"]'))
    recovered = update(ui)
    assert recovered.status == 200 and recovered.json().get('active') is True
    assert recovered.json()['id'] == home['id'] and not states(data)
    assert _verified_permit_receipt(local, run.oracle.get('account', path), 'released')
    assert any(row.get('method') == 'GET' and row.get('path') == path
        and row.get('status') == 200 and not row.get('fault') for row in run.network[network_start:])
    assert run.oracle.notion_course_directory(home['id']) == directory
    assert _permit_tree_state(run.oracle.notion_tree(home['id'])) == before
    writes = [row for row in run.network[network_start:] if row.get('boundary') == 'notion'
        and row.get('method') in {'POST', 'PATCH', 'DELETE'} and row.get('path') != '/v1/search'
        and not row.get('path', '').endswith('/query')]
    # A fully current projection may need no Notion mutation. The successful
    # lost permit response and independent own-request GET above remain required.
    run.actions.append({'operation': 'permit-recovery-notion-write-count',
                        'variant': variant, 'count': len(writes)})
    assert all(re.fullmatch(r'[0-9a-f]{64}', row.get('request_body_sha256', '')) for row in writes)
    attempts = Counter((row['method'], row['path'], row['request_body_sha256']) for row in writes)
    assert all(count <= 1 for count in attempts.values()), 'Unknown Notion intent replayed'
    assert preserved == _file_hashes(run, student_only=True)
    _open(ui, writable[0])
    ui.screenshot('resilience-' + variant + '-independently-recovered')
    run.check('unknown-write-not-replayed-and-permits-reconciled', True,
        {'own_request_get_released': True, 'full_notion_tree_unchanged': True,
         'write_attempt_counts': list(attempts.values()), 'successful_responses_dropped': len(lost)})
    run.fault_check(variant, True)
    return ui


def _valid_double_refresh(observation, responses, task):
    """Require two actual clicks and exactly one native task acceptance."""
    clicks = [row for row in observation.get('events', [])
              if row.get('type') == 'click' and row.get('trusted') is True]
    accepted = [row for row in responses if row.get('status') == 200
                and row.get('body', {}).get('kind') == 'sync']
    return (observation.get('boundary') == 'owned-installed-webview-input'
            and observation.get('observed') is True and len(clicks) == 2
            and 1 <= len(responses) <= 2 and len(accepted) == 1
            and all(row in accepted or (row.get('status') == 409
                and row.get('body', {}).get('detail') == '已有录音任务正在进行，请等待完成。')
                for row in responses)
            and task.get('kind') == 'sync'
            and task.get('state') in {'done', 'partial', 'failed', 'cancelled'})


def _resilience_double_refresh(run, ui, course_id):
    """Observe real installed input and responses; no app mutation via HTTP."""
    from urllib.parse import urlsplit
    if ui.page is None or ui.name == 'external':
        raise ResourceBlocked('owned-installed-double-click-observer-required')
    _open(ui, course_id)
    selector = '[data-action="recording-sync-course"][data-course="' + course_id + '"]'
    ui.wait(lambda: ui.visible(selector) and ui.attr(selector, 'disabled') is None)
    origin = urlsplit(ui.page.url)
    responses, action_start = [], len(run.actions)

    def observed(response):
        parsed = urlsplit(response.url)
        if ((parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
                or parsed.path != '/api/recordings/sync' or response.request.method != 'POST'):
            return
        body = response.json()
        record = {'status': response.status,
                  'body': {key: body[key] for key in ('kind', 'state', 'detail', 'course_id') if key in body}}
        responses.append(record)
        # Persist the actual responses in the final raw action evidence even
        # when waiting for task completion subsequently fails.
        run.actions.append({'instance': ui.name, 'operation': 'native-double-refresh-response',
                            'response': record})

    ui.page.on('response', observed)
    try:
        clicked = time.monotonic()
        ui.double_click(selector)
        ui.wait(lambda: any(row['status'] == 200 for row in responses)
                or len(responses) >= 2, timeout=30)
        accepted = [row['body'] for row in responses if row['status'] == 200
                    and row['body'].get('kind') == 'sync']
        if len(accepted) != 1:
            raise AssertionError('Actual double click did not accept exactly one sync task')
        if accepted[0].get('course_id') != course_id:
            raise AssertionError('Double click accepted another course task')
        current = ui.request_read('/api/recordings/task')
        # This compound assertion tests task deduplication and preservation.
        # Cancel that exact accepted running task via the actual native button,
        # before this course's Notion projection. Network restoration remains
        # independently required to finish its un-cancelled refresh below.
        task = (_cancel_refresh(ui, accepted[0], timeout=90)
                if accepted[0].get('state') == current.get('state') == 'running'
                else _task(ui, timeout=90, started_monotonic=clicked))
        # Wait for the installed frontend to render the terminal task as well.
        ui.wait(lambda: ui.visible(selector) and ui.attr(selector, 'disabled') is None)
    finally:
        ui.page.remove_listener('response', observed)
    inputs = [row['actual_native_double_click'] for row in run.actions[action_start:]
              if row.get('operation') == 'observed-native-double-click'
              and row.get('instance') == ui.name and row.get('selector') == selector]
    passed = len(inputs) == 1 and _valid_double_refresh(inputs[0], responses, task)
    run.actions.append({'instance': ui.name, 'operation': 'native-double-refresh-receipt',
                        'actual_input_count': len(inputs), 'responses': responses,
                        'task_kind': task.get('kind'), 'task_state': task.get('state'),
                        'single_task_accepted': passed})
    ui.screenshot('resilience-real-double-click-terminal')
    return {'verified': passed, 'task': task}


def _cached_source_content(value):
    """Keep all source/user content; compare coordination diagnostics separately."""
    volatile = {'synced_at', 'synced_at_label', 'catalog_revision', 'read_only',
                'read_only_message', 'section_states', 'announcements_status', 'assignments_status'}
    return {key: item for key, item in value.items() if key not in volatile}


def run_resilience(run):
    from scripts.product_e2e_gate import FAULT_VARIANTS
    _catalog_scope_preflight(run)
    ui = run.session()
    baseline = _effects(run)
    _action(ui, 'open-dashboard')
    rows = ui.request_read('/api/campus/courses')['courses']
    course_id = run.need('catalog.resilience_course_id')
    cached_source = _cached_source_content(_open(ui, course_id))
    cached_list = [_cached_source_content(row) for row in rows]
    _cancel_refresh(ui, _start_refresh(ui))
    effects = _effects(run)
    run.check('browse-and-cancel-no-media-model-charge-or-submit',
              effects == baseline, 'Ledger/network/official history unchanged by browse/cancel')
    from scripts.product_e2e_gate import required_assertions
    layout_key = 'welcome-course-list-left-account-and-course-centers-work-at-supported-dpi-with-keyboard-scroll'
    if layout_key in required_assertions(run.scenario, run.mode):
        try:
            metrics = _layout_probe(run, ui, course_id)
        except ResourceBlocked as exc:
            if exc.code != 'native-os-dpi-controller-or-prior-measured-runs-required':
                # Invalid window/source identity or tampered evidence is an
                # integrity failure: never continue charge-producing actions.
                raise
            # Missing additional physical scales cannot pass the layout gate,
            # but it need not prevent independent UI/ledger cost acceptance.
            run.check(layout_key, False, exc.code)
            run.blockers.append(exc.code)
        else:
            run.check(layout_key,
                      metrics['dpi_coverage_ok'] and metrics['welcome'] and metrics['sidebar_left'] and metrics['keyboard_reachable']
                      and metrics['scroll_reachable'] and not metrics['horizontal_overflow']
                      and metrics['centers'] == ['course-assignments', 'course-notices', 'course-materials'],
                      'Measured native window layout and keyboard navigation at configured DPI')
    failures = []
    before = _file_hashes(run, student_only=True)
    run.config['network_fault_course_id'] = course_id
    variants = FAULT_VARIANTS['JOURNEY-06'] if run.mode == 'fault' else ()
    def execute_fault(variant):
        nonlocal ui
        if variant in {'permit-acquire-lost-response', 'permit-release-lost-response'}:
            ui = _resilience_permit_recovery(run, ui, variant)
            return
        with run.fault(variant):
            if variant == 'kill-process-and-restart':
                ui = run.crash_and_restart('primary')
                double_refresh = _resilience_double_refresh(run, ui, course_id)
                task = double_refresh['task']
            else:
                task = _refresh_course(ui, course_id)
            current = ui.request_read('/api/campus/courses')['courses']
            if variant in {'offline', 'dns', 'proxy', 'timeout', 'network-restored'}:
                retained = ui.request_read('/api/campus/courses/' + quote(course_id, safe=''))
                run.check('network-failure-bounded-and-cache-remains-usable',
                          task['observed_elapsed_seconds'] < 90
                          and [_cached_source_content(row) for row in current] == cached_list
                          and _cached_source_content(retained) == cached_source
                          and _file_hashes(run, student_only=True) == before
                          and ui.visible('#course-lessons'),
                          'Fault ' + variant + ': bounded failure retains browsable cache')
            elif variant in {'403', '409', '429', '5xx', 'low-balance', 'disk-full', 'malformed-json'}:
                failures.append(task)
                run.check('403-409-429-5xx-low-balance-disk-full-and-malformed-response-actionable',
                          _visible_failure(run, ui, task),
                          'Fault ' + variant + ': visible reason and next action')
            elif variant == 'kill-process-and-restart':
                run.check('interruption-restart-and-double-click-no-duplicate-effect',
                          double_refresh['verified'] and _effects(run) == baseline
                          and _file_hashes(run, student_only=True) == before,
                          'Actual OS kill/restart and two trusted clicks accept one task without new side effects')
            ui.screenshot('resilience-' + variant)
            if variant != 'network-restored':
                run.fault_check(variant, task.get('state') != 'running',
                                'Native terminal state; journey assertions remain separately mandatory')
        if variant == 'network-restored':
            recovered = _refresh_course(ui, course_id)
            if recovered.get('failure_id'):
                ui.wait(lambda: recovered['failure_id'] in ui.text('body'), timeout=10)
            ui.screenshot('resilience-network-restored-retry')
            run.check('network-failure-bounded-and-cache-remains-usable',
                      recovered.get('state') in {'done', 'partial'}, 'Actual restored network retry')
            run.fault_check(variant, recovered.get('state') in {'done', 'partial'},
                            'Real offline request observed, boundary removed, actual native retry completed')
    _resilience_faults(run, variants, execute_fault)
    cost_key = 'ui-cost-and-reused-receipt-match-real-ledger'
    if cost_key in required_assertions(run.scenario, run.mode):
        _resilience_cost_receipt(run)
    if run.mode == 'live':
        # The five exception-recovery assertions are mandatory in fault mode,
        # not invented HTTP failures in the normal real-service run.
        return
    log_ids = run.observe()['failure_ids']
    run.check('failure-id-matches-redacted-log-and-next-action',
              bool(failures) and all(task.get('failure_id')
                  and task['failure_id'] in log_ids
                  and (any(word in task.get('error', '') for word in ('重试', '检查', '重新', '稍后'))
                       or re.search(r'account_unconfirmed|revision|快照.*未确认|快照.*已更新', task.get('error', ''), re.I))
                  for task in failures),
              'Actual failure IDs match independently read sanitized runtime logs')
