"""Offline safety checks for the driver, never a native E2E verdict."""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from product_e2e import core, native_driver
from product_e2e.oracles import Oracle, OracleError


@pytest.fixture
def run(tmp_path):
    manifest = json.loads((Path(__file__).resolve().parents[1] / "docs/PRODUCT_E2E_SCENARIOS.json").read_text("utf-8"))
    return core.NativeRun(manifest["scenarios"][0], "live", {
        "config": {}, "run_dir": tmp_path, "target": "windows", "version": "0.1.23",
        "artifact_sha256": "not-a-real-artifact", "installed_path": "not-an-install",
    })


def test_missing_checks_and_measurements_never_become_pass_or_zero(run):
    result = run.finish()
    assert not any(result["assertions"].values())
    assert all(value is None for value in result["metrics"].values())
    assert result["entry"] == "installed-native"
    assert all((run.context["run_dir"] / row["path"]).is_file() for row in result["evidence"])


def test_missing_full_journey_resources_block_before_measurements_and_actions(run, monkeypatch):
    monkeypatch.setattr(native_driver, 'NativeRun', lambda *args: run)
    def forbidden(*args, **kwargs):
        pytest.fail('Missing resources must fail before remote reads or actions')
    monkeypatch.setattr(run.oracle, 'snapshot', forbidden)
    monkeypatch.setitem(native_driver.JOURNEYS, run.scenario['id'], forbidden)
    result = native_driver.run_case(deepcopy(run.scenario), 'live', run.context)
    assert result['blockers'] == ['missing_external_browser']
    assert not any(result['assertions'].values())
    assert all(value is None for value in result['metrics'].values())


def test_upgrade_preserves_registry_and_requires_normal_close(run, tmp_path, monkeypatch):
    from product_e2e import windows_install
    artifact = tmp_path / 'candidate.exe'
    artifact.write_bytes(b'candidate')
    installed = tmp_path / 'installed/pku-desktop.exe'
    installed.parent.mkdir()
    installed.write_bytes(b'installed')
    run.config.update(installation={'approved': True}, test_root=str(tmp_path),
        instances={'primary': {'installed_path': str(installed)}})
    run.context.update(artifact=artifact, artifact_sha256=core.sha256(artifact))
    run.processes['primary'] = SimpleNamespace(poll=lambda: None)
    actions = []
    monkeypatch.setattr(run, 'quit', lambda name, **kwargs: actions.append(('quit', name, kwargs)))

    def install(package, target, root):
        actions.append(('install', package, target, root))
        return {'artifact_sha256': core.sha256(package), 'installed_sha256': core.sha256(target),
            'version': '0.1.23', 'student_product_registry_values_restored': True}

    monkeypatch.setattr(windows_install, 'install_isolated', install)
    receipt = run.upgrade()
    assert actions[0] == ('quit', 'primary', {'require_graceful': True})
    assert actions[1] == ('install', artifact, installed.resolve(), tmp_path.resolve())
    assert receipt['student_product_registry_values_restored'] is True


def test_later_success_cannot_overwrite_failed_assertion(run):
    key = run.scenario["assertions"][0]
    run.check(key, False)
    run.check(key, True)
    assert run.assertions[key] is False
    with pytest.raises(AssertionError):
        run.check(key, "true")
    with pytest.raises(AssertionError):
        run.check("not-in-policy", True)


@pytest.mark.parametrize("value", ["https://127.0.0.1:123", "http://example.com", "http://user:secret@localhost:12", "http://localhost:12/?token=x"])
def test_untrusted_native_endpoint_is_rejected(value):
    with pytest.raises(core.ResourceBlocked):
        core.loopback_url(value)


def test_plaintext_credential_config_is_rejected(monkeypatch):
    with pytest.raises(core.ResourceBlocked):
        core.credential({"credentials": {"account_token": "secret"}}, "account_token")
    monkeypatch.setenv("PKU_TEST_TOKEN", "not-serialized")
    assert core.credential({"credentials": {"account_token": {"env": "PKU_TEST_TOKEN"}}}, "account_token") == "not-serialized"


def test_budget_reservation_is_whole_run_not_per_case(run):
    run.config["budget"] = {"approved": True, "max_points": 10, "max_seconds": 60}
    run.authorize_cost(points=7, seconds=30)
    second = core.NativeRun({**run.scenario, "id": "ANOTHER-CASE"}, "live", run.context)
    with pytest.raises(core.ResourceBlocked, match="total_run_budget_exceeded"):
        second.authorize_cost(points=4)
    assert json.loads((run.context["run_dir"] / "cost-reservations.json").read_text())["points"] == 7


def test_unsupported_fault_is_software_gap_not_a_pass_or_account_problem(run):
    run.mode = "fault"
    with pytest.raises(core.ResourceBlocked, match="software_capability_missing_fault_disk-full"):
        with run.fault("disk-full"):
            pytest.fail("unimplemented injection must never enter the body")


def test_live_run_never_injects_an_error(run):
    with pytest.raises(core.ResourceBlocked, match="fault_injection_forbidden_in_live"):
        with run.fault("offline"):
            pytest.fail("live cannot enter a fault boundary")


def test_screenshot_masks_editable_controls_and_does_not_store_values(run):
    observed = {}
    page = SimpleNamespace(url="http://127.0.0.1:8791/app", on=lambda *args: None,
                           locator=lambda selector: selector,
                           screenshot=lambda **kwargs: observed.update(kwargs))
    ui = core.NativeUI(run, "primary", page=page)
    ui.screenshot("login")
    assert "input" in observed["mask"][0] and "contenteditable" in observed["mask"][0]
    ui._action("fill", "#auth-password")
    assert "value" not in run.actions[0]
    with pytest.raises(core.ResourceBlocked):
        ui.attr("#auth-password", "value")


def test_later_same_named_screenshot_cannot_replace_existing_evidence(run):
    page = SimpleNamespace(url='http://127.0.0.1:8791/app', on=lambda *args: None,
        locator=lambda selector: selector,
        screenshot=lambda **kwargs: Path(kwargs['path']).write_bytes(b'first' if
            not run.ui_evidence else b'second'))
    ui = core.NativeUI(run, 'primary', page=page)
    first = ui.screenshot('confirmation')
    second = ui.screenshot('confirmation')
    assert first != second and first.read_bytes() == b'first' and second.read_bytes() == b'second'


def test_explicit_dialog_is_armed_then_handled_inside_click_callback(run):
    actions = []
    page = SimpleNamespace(url="http://127.0.0.1:8791/app", on=lambda *args: None)
    ui = core.NativeUI(run, "primary", page=page)
    dialog = SimpleNamespace(accept=lambda: actions.append("accepted"), dismiss=lambda: actions.append("dismissed"))
    ui.prepare_dialog(False)
    ui._on_dialog(dialog)
    ui.confirm(False)
    assert actions == ["dismissed"]


def test_unapproved_submission_interlock_blocks_before_the_product_endpoint(run):
    callbacks, decisions = {}, []
    page = SimpleNamespace(url="http://127.0.0.1:8791/app", on=lambda *args: None,
                           route=lambda pattern, callback: callbacks.update({pattern: callback}),
                           unroute=lambda pattern, callback: callbacks.pop(pattern))
    ui = core.NativeUI(run, "primary", page=page)
    with ui.guard_submission() as attempts:
        request = SimpleNamespace(method="POST", url="http://127.0.0.1:8791/api/campus/courses/_1_1/assignments/_2_1/submit")
        route = SimpleNamespace(request=request, abort=lambda code: decisions.append(code),
                                continue_=lambda: pytest.fail("forbidden POST cannot reach panel"))
        callbacks["**/*"](route)
        assert attempts and decisions == ["blockedbyclient"]
    assert not callbacks
    assert run.network[0]["safety_interlock"] == "blocked-unapproved-submission"


def test_native_endpoint_must_belong_to_launched_process_tree(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="42"))
    monkeypatch.setattr(core, "process_inventory", lambda: [{"pid": 42, "parent_pid": 2}])
    with pytest.raises(core.ResourceBlocked, match="owned_by_another_process"):
        core.endpoint_owned_by("http://127.0.0.1:9000", 1)
    monkeypatch.setattr(core, "process_inventory", lambda: [{"pid": 42, "parent_pid": 1}])
    assert core.endpoint_owned_by("http://127.0.0.1:9000", 1) == [42]


def test_single_machine_journey_cannot_launch_secondary_profile(run):
    run.scenario = {**run.scenario, 'device_scope': 'single-machine'}
    with pytest.raises(core.ResourceBlocked, match='single_machine_scope_forbids_secondary_native_instance'):
        run.session('secondary')
    assert not run.processes


def test_native_restart_rejects_disabled_graceful_validation(run, monkeypatch):
    monkeypatch.setattr(run, 'quit', lambda *args, **kwargs: pytest.fail('unsafe restart reached quit'))
    with pytest.raises(ValueError, match='requires_graceful_quit'):
        run.restart(require_graceful=False)


def test_double_click_cannot_fall_back_to_a_programmatic_post(run):
    ui = core.NativeUI(run, 'primary')
    with pytest.raises(core.ResourceBlocked, match='native_double_click_adapter_unavailable'):
        ui.double_click('#generate')
    assert run.actions[-1]['operation'] == 'double_click'
    assert not run.network


@pytest.mark.parametrize('events,passed', [
    ([], False),
    ([{'type': 'click', 'trusted': True}], False),
    ([{'type': 'click', 'trusted': False}] * 2, False),
    ([{'type': 'click', 'trusted': True}] * 2, True),
])
def test_double_click_fault_requires_two_observed_trusted_native_clicks(run, events, passed):
    run.mode = 'fault'
    def exercise():
        with run.fault('double-click') as receipt:
            run.actions.append({'operation': 'double_click', 'instance': 'primary',
                'actual_native_double_click': {'boundary': 'owned-installed-webview-input',
                    'events': events, 'observed': passed}})
        assert receipt['observed'] is True and receipt['trusted_click_count'] == 2
    if passed:
        exercise()
    else:
        with pytest.raises(core.ResourceBlocked, match='actual_native_double_click_fault_not_exercised'):
            exercise()


def test_zero_revision_is_valid_only_when_snapshot_is_null():
    assert Oracle._catalog_envelope({"revision": 0, "snapshot": None})["revision"] == 0
    for value in [False, -1, "0", None]:
        with pytest.raises(OracleError):
            Oracle._catalog_envelope({"revision": value, "snapshot": None})
    with pytest.raises(OracleError):
        Oracle._catalog_envelope({"revision": 0, "snapshot": {}})


def test_native_execution_failure_is_retained_without_secret_exception_text(run, monkeypatch):
    monkeypatch.setattr(native_driver, '_preflight_resources', lambda value: None)
    monkeypatch.setattr(native_driver, "NativeRun", lambda *args: run)
    # Isolate action failure from independently tested accounting failures.
    monkeypatch.setattr(run.oracle, 'snapshot', lambda **kwargs: None)
    def action(context):
        raise AssertionError("secret-url-token")
    monkeypatch.setitem(native_driver.JOURNEYS, run.scenario["id"], action)
    result = native_driver.run_case(deepcopy(run.scenario), "live", run.context)
    assert result["blockers"] == ["execution_failed_AssertionError"]
    assert not any(result["assertions"].values())
    assert "secret-url-token" not in json.dumps(result)
    failures = [row["execution_failure"] for row in run.actions if "execution_failure" in row]
    assert len(failures) == 1
    assert failures[0]["type"] == "AssertionError"
    assert failures[0]["locations"][-1]["function"] == "action"
    for location in failures[0]["locations"]:
        assert set(location) == {"file", "function", "line"}
        assert "/" not in location["file"] and "\\" not in location["file"]
        assert type(location["line"]) is int and location["line"] > 0
    assert "secret-url-token" not in json.dumps(run.actions)


@pytest.mark.parametrize('active,email,login_expected,blocked', [
    (True, 'selected@example.test', False, False),
    (False, '', True, False),
    (True, 'other@example.test', False, True),
])
def test_catalog_startup_waits_for_real_session_not_transient_login_form(
        monkeypatch, active, email, login_expected, blocked):
    from product_e2e import catalog_journeys

    session = {'active': active, 'email': email, 'email_verified': True}
    state = {'hydrated': False, 'logins': 0, 'authorized': False}

    def visible(selector):
        return (not state['hydrated']) if selector == '#auth-email' else state['hydrated']

    def wait(predicate):
        if not predicate():
            state['hydrated'] = True
            assert predicate(), 'expected bounded hydration wait'

    def read(path):
        return {'/api/auth/me': session, '/api/campus/courses': {'campus_configured': True},
                '/api/connection': {'connected': True},
                '/api/campus/notion/parents': {'home': {'id': 'home'}}}[path]

    def login(*args):
        assert login_expected, 'never re-login an already active seeded session'
        state['logins'] += 1
        state['hydrated'] = True
        session.update(active=True, email='selected@example.test')

    def authorize(*args):
        state['authorized'] = True

    monkeypatch.setattr(catalog_journeys, '_login', login)
    monkeypatch.setattr(catalog_journeys, '_authorized_home', authorize)
    ui = SimpleNamespace(visible=visible, wait=wait, request_read=read)
    run = SimpleNamespace(credential=lambda key: 'selected@example.test')
    if blocked:
        with pytest.raises(core.ResourceBlocked, match='signed-in-to-wrong-account'):
            catalog_journeys._catalog_setup(run, ui)
    else:
        catalog_journeys._catalog_setup(run, ui)
    assert state['logins'] == int(login_expected)
    assert state['authorized'] is not blocked


@pytest.mark.parametrize('journey_blocked', [False, True])
def test_full_driver_closes_native_before_sealing_runtime_evidence(run, monkeypatch, journey_blocked):
    monkeypatch.setattr(native_driver, '_preflight_resources', lambda value: None)
    events = []
    alive = {'primary': True}
    monkeypatch.setattr(native_driver, 'NativeRun', lambda *args: run)
    monkeypatch.setattr(run.oracle, 'snapshot', lambda **kwargs: None)
    def action(context):
        run.processes['primary'] = SimpleNamespace(poll=lambda: None)
        events.append('journey')
        if journey_blocked:
            raise core.ResourceBlocked('real-native-dpi-coverage-missing')
    def quit(name='primary', *, require_graceful=False):
        assert require_graceful is True
        events.append('graceful-quit')
        alive[name] = False
    def close():
        events.append('cleanup')
        alive['primary'] = False
    finish = run.finish
    def seal():
        events.append('seal')
        assert not alive['primary']
        return finish()
    monkeypatch.setitem(native_driver.JOURNEYS, run.scenario['id'], action)
    monkeypatch.setattr(run, 'quit', quit)
    monkeypatch.setattr(run, 'close', close)
    monkeypatch.setattr(run, 'finish', seal)
    monkeypatch.setattr(run, '_safe_observe', lambda: {'processes': [{'alive': alive['primary']}]})
    result = native_driver.run_case(deepcopy(run.scenario), 'live', run.context)
    assert events == ['journey', 'graceful-quit', 'cleanup', 'seal']
    assert result['blockers'] == (['real-native-dpi-coverage-missing'] if journey_blocked else [])
    raw = next(run.context['run_dir'] / row['path'] for row in result['evidence'] if row['path'].endswith('runtime.json'))
    assert json.loads(raw.read_text('utf-8'))['processes'] == [{'alive': False}]


def test_full_driver_retains_failed_graceful_quit_even_after_cleanup(run, monkeypatch):
    monkeypatch.setattr(native_driver, '_preflight_resources', lambda value: None)
    monkeypatch.setattr(native_driver, 'NativeRun', lambda *args: run)
    monkeypatch.setattr(run.oracle, 'snapshot', lambda **kwargs: None)
    def action(context):
        run.processes['primary'] = SimpleNamespace(poll=lambda: None)
    def quit(name, *, require_graceful):
        assert require_graceful is True
        raise AssertionError('private-url-must-not-escape')
    monkeypatch.setitem(native_driver.JOURNEYS, run.scenario['id'], action)
    monkeypatch.setattr(run, 'quit', quit)
    monkeypatch.setattr(run, 'close', lambda: None)
    monkeypatch.setattr(run, '_safe_observe', lambda: {'processes': [{'alive': False}]})
    result = native_driver.run_case(deepcopy(run.scenario), 'live', run.context)
    assert result['blockers'] == ['native_graceful_quit_failed_AssertionError']
    assert 'private-url' not in json.dumps(result)


@pytest.mark.parametrize('normal_exit', [True, False])
def test_forced_cleanup_never_passes_normal_native_quit(run, monkeypatch, normal_exit):
    calls = []
    attempts = []
    def wait(timeout):
        attempts.append(timeout)
        if not normal_exit and len(attempts) == 1:
            raise core.subprocess.TimeoutExpired('owned process', timeout)
        return 0
    process = SimpleNamespace(pid=12345, poll=lambda: None, wait=wait)
    run.processes['primary'] = process
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    monkeypatch.setattr(core, 'close_owned_windows', lambda pid: calls.append(('window-close', pid)))
    monkeypatch.setattr(core.subprocess, 'run', lambda command, **kwargs: calls.append(('cleanup', command)))
    if normal_exit:
        assert run.quit('primary', require_graceful=True)['graceful'] is True
        assert calls == [('window-close', process.pid)]
    else:
        with pytest.raises(AssertionError, match='cleanup_is_not_evidence'):
            run.quit('primary', require_graceful=True)
        assert run.actions[-1]['native_quit']['graceful'] is False
        assert calls[1][1] == ['taskkill.exe', '/PID', str(process.pid), '/T', '/F']


def test_normal_quit_waits_for_owned_catalog_writer_before_window_close(run, monkeypatch):
    actions = []
    task = {"kind": "sync", "state": "running"}
    def wait(predicate, timeout):
        assert actions == [], "Window was closed before catalog write completed"
        assert timeout == 900 and not predicate()
        task["state"] = "done"
        assert predicate()
        actions.append("catalog-terminal")
    ui = SimpleNamespace(origin="http://127.0.0.1:1234",
        request_read=lambda path: task, wait=wait)
    run.sessions["primary"] = ui
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: actions.append("window-close"))
    assert run.quit("primary", require_graceful=True)["graceful"]
    assert actions == ["catalog-terminal", "window-close"]


def test_idle_task_does_not_hide_pending_synchronous_workspace_write(run, monkeypatch):
    events = []
    pending = {1: {'host': '127.0.0.1', 'method': 'POST', 'path': '/api/campus/notion/workspace'}}
    def wait(predicate, timeout):
        assert not events and not predicate()
        pending.clear()  # Real adapter removes this only on a request terminal event.
        assert predicate()
        events.append('workspace-response-finished')
    run.sessions['primary'] = SimpleNamespace(origin='http://127.0.0.1:1234',
        _pending_requests=pending, request_read=lambda path: {'state': 'idle'}, wait=wait)
    run.processes['primary'] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    monkeypatch.setattr(core, 'close_owned_windows', lambda pid: events.append('window-close'))
    assert run.quit('primary', require_graceful=True)['graceful']
    assert events == ['workspace-response-finished', 'window-close']


def test_unknown_workspace_response_requires_review_before_normal_close(run, monkeypatch):
    closed = []
    run.sessions['primary'] = SimpleNamespace(origin='http://127.0.0.1:1234',
        _uncertain_workspace_requests=['/api/campus/notion/workspace'],
        request_read=lambda path: {'state': 'idle'})
    run.processes['primary'] = SimpleNamespace(pid=12345, poll=lambda: None)
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    monkeypatch.setattr(core, 'close_owned_windows', lambda pid: closed.append(pid))
    with pytest.raises(core.ResourceBlocked, match='workspace_write_response_unknown'):
        run.quit('primary', require_graceful=True)
    assert not closed and 'primary' in run.sessions


def test_unknown_catalog_state_does_not_close_as_normal_quit(run, monkeypatch):
    closed = []
    def unavailable(path):
        raise OSError("Local task observation unavailable")
    run.sessions["primary"] = SimpleNamespace(origin="http://127.0.0.1:1234",
        request_read=unavailable)
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: closed.append(pid))
    with pytest.raises(core.ResourceBlocked, match="catalog_state_unavailable"):
        run.quit("primary", require_graceful=True)
    assert closed == [] and "primary" in run.sessions


def test_background_writer_is_not_interrupted_by_native_quit(run, monkeypatch):
    actions = []
    background = {"kind": "sync", "scope": "background", "state": "running"}
    def wait(predicate, timeout):
        assert not actions and timeout == 900 and not predicate()
        background["state"] = "partial"
        assert predicate()
        actions.append("background-terminal")
    run.sessions["primary"] = SimpleNamespace(origin="http://127.0.0.1:1234",
        request_read=lambda path: {"state": "idle", "background_sync": background}, wait=wait)
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: actions.append("window-close"))
    assert run.quit("primary", require_graceful=True)["graceful"]
    assert actions == ["background-terminal", "window-close"]


@pytest.mark.parametrize("kind", ["process", "download", "summary"])
def test_paid_or_media_foreground_waits_before_native_close(run, monkeypatch, kind):
    actions = []
    task = {"state": "running", "kind": kind}
    def wait(predicate, timeout):
        assert not actions and timeout == 900 and not predicate()
        task["state"] = "failed"
        assert predicate()
        actions.append("foreground-terminal")
    run.sessions["primary"] = SimpleNamespace(origin="http://127.0.0.1:1234", request_read=lambda path: task, wait=wait)
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: actions.append("window-close"))
    assert run.quit("primary", require_graceful=True)["graceful"]
    assert actions == ["foreground-terminal", "window-close"]


def test_main_chain_quiz_request_gate_must_finish_before_close(run, monkeypatch):
    actions = []
    run.scenario["id"] = "MAIN-CHAIN"
    exercise = {"state": "running", "kind": "campus-grade"}
    def read(path):
        return {"state": "idle", "exercise_job": exercise}
    def wait(predicate, timeout):
        assert not actions and timeout == 900 and not predicate()
        exercise.update(state="idle", kind="")
        assert predicate()
        actions.append("grade-request-finished")
    run.sessions["primary"] = SimpleNamespace(origin="http://127.0.0.1:1234", request_read=read, wait=wait)
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: actions.append("window-close"))
    assert run.quit("primary", require_graceful=True)["graceful"]
    assert actions == ["grade-request-finished", "window-close"]


def test_existing_status_endpoint_observes_actual_quiz_grade_gate(tmp_path):
    from fastapi.testclient import TestClient
    from pku_sync.panel.connection import build_fake_connection_service

    from pku_sync.panel.jobs import ExerciseJobGate
    from pku_sync.panel.webapi import create_app
    gate = ExerciseJobGate()
    directory = None
    app = create_app(settings=SimpleNamespace(data_dir=tmp_path, platform_token=''), connection_service=build_fake_connection_service(directory_service=directory), exercise_job_gate=gate)
    client = TestClient(app)
    assert client.get("/api/recordings/task").json()["exercise_job"] == {"state": "idle", "kind": ""}
    assert gate.acquire("campus-grade")
    assert client.get("/api/recordings/task").json()["exercise_job"] == {"state": "running", "kind": "campus-grade"}
    gate.release("campus-grade")
    assert client.get("/api/recordings/task").json()["exercise_job"] == {"state": "idle", "kind": ""}


def test_unknown_background_observation_cannot_fall_through_cleanup(run, monkeypatch):
    closed = []
    run.sessions["primary"] = SimpleNamespace(origin="http://127.0.0.1:1234",
        request_read=lambda path: {"state": "idle", "background_sync": {"state": "unverified"}})
    run.processes["primary"] = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "close_owned_windows", lambda pid: closed.append(pid))
    with pytest.raises(core.ResourceBlocked):
        run.quit("primary", require_graceful=True)
    with pytest.raises(core.ResourceBlocked):
        run.close()
    assert closed == [] and "primary" in run.sessions


def test_main_chain_cannot_be_dispatched_as_release_case(run):
    assert "MAIN-CHAIN" not in native_driver.JOURNEYS
    with pytest.raises(ValueError, match="unsupported_native_journey"):
        native_driver.run_case({"id": "MAIN-CHAIN"}, "live", run.context)
    with pytest.raises(ValueError, match="windows_scope"):
        native_driver.run_main_chain({"target": "macos"})


def test_main_chain_different_source_is_rejected_before_any_native_or_paid_work():
    config = {"learning": {"course_id": "_1_1", "source_id": "real-source"},
              "quiz": {"course_id": "_2_1", "source_ids": ["real-source"]}}
    run = SimpleNamespace(need=lambda key: core.need(config, key))
    with pytest.raises(AssertionError, match="selected source"):
        native_driver._fresh_main_chain(run)


@pytest.mark.parametrize('marker_kind,valid_parent,accepted', [
    ('plain', True, True),
    ('encoded', True, True),
    ('wrong-source', True, False),
    ('missing', True, False),
    ('encoded', False, False),
])
def test_main_chain_reads_encoded_source_without_weakening_parent_or_source_guards(
        monkeypatch, tmp_path, marker_kind, valid_parent, accepted):
    from urllib.parse import quote
    from product_e2e import catalog_journeys, learning_journeys

    source = 'pku:recording:' + 'a' * 32
    config = {'learning': {'course_id': '_1_1', 'source_id': source,
                          'short_recording_id': 'lesson', 'notion_test_parent_id': 'parent'},
              'quiz': {'course_id': '_1_1', 'source_ids': [source]}}
    lesson = {'id': 'lesson', 'source_id': source}
    centers = {name: {'id': name} for name in ['每节课', '作业中心', '通知中心', '小测中心']}
    workspace = {'id': 'home', 'courses': {'_1_1': {'id': 'course', 'centers': centers,
                 'lectures': {'lesson': {'id': 'lecture', 'source_id': source}}}}}
    app=tmp_path/'primary';(app/'data').mkdir(parents=True)
    (app/'data/notion-workspace-v2.json').write_text(json.dumps(workspace),'utf-8')
    config.update(test_root=str(tmp_path),instances={'primary':{'app_dir':str(app)}})
    marker = source if marker_kind == 'plain' else quote(source, safe='')
    if marker_kind == 'wrong-source':
        marker = quote('pku:recording:' + 'b' * 32, safe='')
    blocks = [] if marker_kind == 'missing' else [{'type': 'paragraph', 'paragraph': {
        'rich_text': [{'type': 'text', 'text': {'content': '查看教学网讲次来源',
            'link': {'url': 'https://course.pku.edu.cn/webapps/'
                     'bb-streammedia-hqy-BBLEARN/videoList.action#' + marker}}}]}}]
    checks = {}
    run = SimpleNamespace(need=lambda key: core.need(config, key), session=lambda *args: object(),
                          check=lambda key, value: checks.update({key: value}),actions=[])
    monkeypatch.setattr(catalog_journeys, '_catalog_setup', lambda *args, **kwargs: None)
    monkeypatch.setattr(catalog_journeys, '_refresh_course', lambda *args: {
        'state': 'done', 'observed_elapsed_seconds': 1})
    monkeypatch.setattr(learning_journeys, '_open_course', lambda *args: (object(), {'lessons': [lesson]}))
    monkeypatch.setattr(learning_journeys, '_workspace', lambda *args, **kwargs: workspace)
    monkeypatch.setattr(learning_journeys, '_page', lambda run, page, parent: (
        {}, valid_parent if page == 'lecture' else True))
    monkeypatch.setattr(learning_journeys, '_blocks', lambda *args: blocks)
    def stop_before_media_or_paid_work(*args):
        raise core.ResourceBlocked('verified_projection_stop_before_media')
    monkeypatch.setattr(learning_journeys, '_lesson_page', stop_before_media_or_paid_work)

    if accepted:
        with pytest.raises(core.ResourceBlocked, match='verified_projection_stop_before_media'):
            native_driver._fresh_main_chain(run)
    else:
        with pytest.raises(AssertionError, match='projection is unverified'):
            native_driver._fresh_main_chain(run)
    assert checks[native_driver.MAIN_CHECKS[1]] is accepted


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_nonfinite_cost_never_creates_paid_reservation(run, invalid):
    run.config["budget"] = {"approved": True, "max_points": 10, "max_seconds": 100}
    with pytest.raises(ValueError, match="invalid_cost"):
        run.authorize_cost(seconds=invalid)
    assert not (run.context["run_dir"] / "cost-reservations.json").exists()


def repair_driver_fixture(tmp_path):
    private=tmp_path/'private'; private.mkdir()
    repo=tmp_path/'repo'; repo.mkdir()
    before_binary=private/'installed37/pku-desktop.exe';after_binary=private/'installed38/pku-desktop.exe'
    before_root=before_binary.parent/'resources/runtime/Lib/site-packages'
    after_root=after_binary.parent/'resources/runtime/Lib/site-packages'
    files=['pku_sync/media.py','pku_sync/lecture_source_download.py','pku_sync/panel/notion_workspace.py','pku_sync/panel/static/app.js']
    manifests=[]
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    for directory,artifact in [(before_root,'a'*64),(after_root,'b'*64)]:
        mapping={}
        for name in files:
            path=directory/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('new' if directory==after_root and name.endswith('source_download.py') else 'original','utf-8')
            mapping[name]=hashlib.sha256(path.read_bytes()).hexdigest()
            if directory==after_root:
                copied=repo/name;copied.parent.mkdir(parents=True,exist_ok=True);copied.write_bytes(path.read_bytes())
        manifests.append(dump(directory/'manifest.json',{'artifact_sha256':artifact,'product_root':str(directory),'files':mapping}))
    app=private/'primary'; recording=app/'data/course/recordings/one';recording.mkdir(parents=True)
    dump(recording/'recording.json',{'source_id':'source','course_id':'course'})
    (recording/'video.mp4').write_bytes(b'original verified source video')
    (recording/'transcript.json').write_bytes(b'original paid verified transcript')
    case=tmp_path/'r8';case.mkdir()
    ledger={'schema':1,'complete':True,'source':'account_cumulative_usage_rows','jobs':[],
            'settlements':[],'charged_seconds':0,'charged_points':0,'observed_at':'old'}
    before=dump(case/'oracle-before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':ledger})
    post=dump(case/'oracle-post.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':ledger})
    prior_artifact=tmp_path/'prior.exe';prior_artifact.write_bytes(b'actual prior fixture package')
    artifact_sha=hashlib.sha256(prior_artifact.read_bytes()).hexdigest()
    before_binary.write_bytes(b'actual prior installed fixture binary')
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.37','alive':False,
        'artifact_sha256':artifact_sha,'installed_path':str(before_binary),
        'installed_sha256':hashlib.sha256(before_binary.read_bytes()).hexdigest()}]})
    m=json.loads(Path(manifests[0]['path']).read_text('utf-8'));m['artifact_sha256']=artifact_sha
    manifests[0]=dump(Path(manifests[0]['path']),m)
    report=dump(case/'main-chain-report.json',{'kind':'installed-native-main-chain-not-release-verdict','target':'windows',
        'version':'0.1.37','artifact_sha256':artifact_sha,'complete_main_chains_passed':0,'complete_cases_passed':0,
        'complete_journeys_passed':0,'full_release_gate_passed':False,'result':{'entry':'installed-native','mocked':False,
        'injection':None,'assertions':{key:False for key in native_driver.MAIN_CHECKS},
        'blockers':['generated_summary_source_version_missing'],
        'evidence':[{'path':Path(item['path']).name,'sha256':item['sha256'],
                     'role':'runtime' if item is runtime else 'oracle'} for item in [before,post,runtime]]}})
    proof={'schema':1,'kind':'same-goal-main-chain-repair-continuation','prior_version':'0.1.37','current_version':'0.1.38',
        'course_id':'course','recording_id':'recording','source_id':'source','current_artifact_sha256':'b'*64,
        'original_media_sha256':hashlib.sha256((recording/'video.mp4').read_bytes()).hexdigest(),
        'transcript_sha256':hashlib.sha256((recording/'transcript.json').read_bytes()).hexdigest(),
        'notion_test_parent_id':'parent','test_root':str(private),'app_dir':str(app),
        'owner_email_sha256':hashlib.sha256(b'owner@example.org').hexdigest(),
        'prior_report_path':report['path'],'prior_report_sha256':report['sha256'],
        'prior_artifact_path':str(prior_artifact),'prior_artifact_sha256':artifact_sha,
        'prior_before_ledger':before,'prior_processed_ledger':post,
        'prior_product_manifest':manifests[0],'current_product_manifest':manifests[1],
        'allowed_changed_file':'pku_sync/lecture_source_download.py'}
    bound=dump(tmp_path/'repair-proof.json',proof)
    config={'main_chain_resume':{'proof_path':bound['path'],'proof_sha256':bound['sha256']},'learning':{
        'course_id':'course','short_recording_id':'recording','source_id':'source','notion_test_parent_id':'parent',
        'original_media_sha256':proof['original_media_sha256']},'test_root':str(private),'instances':{'primary':{'app_dir':str(app)}}}
    run=SimpleNamespace(config=config,context={'root':repo,'target':'windows','version':'0.1.38','artifact_sha256':'b'*64,
        'installed_path':str(after_binary)},
        need=lambda key:core.need(config,key),credential=lambda key:'owner@example.org')
    return run,proof,recording


@pytest.mark.parametrize('fault',['none','transcript','media','report','asr-module','app-js','owner','cross-source'])
def test_main_repair_resume_requires_original_real_inputs_and_only_reviewed_product_change(tmp_path,fault):
    run,proof,recording=repair_driver_fixture(tmp_path)
    if fault=='transcript':(recording/'transcript.json').write_bytes(b'changed')
    if fault=='media':(recording/'video.mp4').write_bytes(b'changed')
    if fault=='report':Path(proof['prior_report_path']).write_text('{}','utf-8')
    if fault in {'asr-module','app-js'}:
        manifest=json.loads(Path(proof['current_product_manifest']['path']).read_text('utf-8'))
        name='pku_sync/media.py' if fault=='asr-module' else 'pku_sync/panel/static/app.js'
        (Path(manifest['product_root'])/name).write_bytes(b'changed')
    if fault=='owner':run.credential=lambda key:'other@example.org'
    if fault=='cross-source':run.config['learning']['source_id']='other-source'
    if fault=='none':
        resume=native_driver._load_main_chain_resume(run)
        native_driver._assert_main_repair_unchanged(resume)
        assert resume['proof']['source_id']=='source'
        (recording/'transcript.json').write_bytes(b'changed later')
        with pytest.raises(core.ResourceBlocked):native_driver._assert_main_repair_unchanged(resume)
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def test_main_repair_fresh_ledger_cannot_mask_changed_business_fields_or_boolean_schema():
    baseline={'schema':1,'complete':True,'source':'account_cumulative_usage_rows','observed_at':'before',
              'jobs':[{'id':'old','status':'done'}],'charged_seconds':1}
    fresh=deepcopy(baseline);fresh['observed_at']='fresh'
    assert native_driver._repair_ledger_equal(baseline,fresh)
    fresh['jobs'][0]['status']='failed'
    assert not native_driver._repair_ledger_equal(baseline,fresh)
    fresh=deepcopy(baseline);fresh['schema']=True
    assert not native_driver._repair_ledger_equal(baseline,fresh)
    fresh=deepcopy(baseline);fresh['charged_seconds']=True
    assert not native_driver._repair_ledger_equal(baseline,fresh)


def settled_note_baseline():
    old = {'schema': 1, 'complete': True, 'source': 'account_cumulative_usage_rows',
           'observed_at': 100, 'charged_seconds': 600, 'charged_points': 1.25,
           'jobs': [{'id': 'original-asr', 'operation_id': 'asr:original', 'status': 'succeeded'}],
           'settlements': [{'id': 'original-fee', 'operation_id': 'asr:original',
                            'charged_seconds': 600}], 'oss_history': [], 'submissions': []}
    current = deepcopy(old)
    current['observed_at'] = 200
    current['charged_points'] = 1.375
    current['jobs'].append({'id': 'note', 'operation_id': 'note-operation',
        'status': 'succeeded', 'source_id': 'source', 'course_id': 'course',
        'purpose': 'lecture-notes', 'points_charged': .125})
    current['settlements'].append({'id': 'note-fee', 'operation_id': 'note-operation',
                                  'charged_seconds': None, 'charged_points': .125})
    return old, current


def test_current_note_baseline_preserves_original_asr_and_exact_single_fees():
    old, current = settled_note_baseline()
    frozen = deepcopy((old, current))
    assert native_driver._repair_verified_ledger_extension(old, current, 'source', 'course')
    assert (old, current) == frozen
    assert not native_driver._repair_ledger_equal(old, current)


@pytest.mark.parametrize('fault', ['historic-job', 'historic-fee', 'asr', 'source',
    'course', 'purpose', 'pending', 'duplicate-job', 'duplicate-fee', 'missing-fee',
    'fee-operation', 'points', 'seconds', 'job-points', 'fee-seconds', 'negative',
    'nan', 'new-oss', 'new-submission', 'unknown-field', 'boolean-schema'])
def test_current_note_baseline_rejects_unexplained_work_or_history_changes(fault):
    old, current = settled_note_baseline()
    job, fee = current['jobs'][-1], current['settlements'][-1]
    if fault == 'historic-job': current['jobs'][0]['status'] = 'failed'
    elif fault == 'historic-fee': current['settlements'][0]['charged_seconds'] = 601
    elif fault == 'asr': job['operation_id'] = fee['operation_id'] = 'asr:new'
    elif fault in ('source', 'course'): job[fault + '_id'] = 'other'
    elif fault == 'purpose': job['purpose'] = 'quiz'
    elif fault == 'pending': job['status'] = 'running'
    elif fault == 'duplicate-job': current['jobs'].append(deepcopy(job))
    elif fault == 'duplicate-fee': current['settlements'].append(deepcopy(fee))
    elif fault == 'missing-fee': current['settlements'].pop()
    elif fault == 'fee-operation': fee['operation_id'] = 'other'
    elif fault == 'points': current['charged_points'] += .01
    elif fault == 'seconds': current['charged_seconds'] += 1
    elif fault == 'job-points': job['points_charged'] = .126
    elif fault == 'fee-seconds': fee['charged_seconds'] = 0
    elif fault in ('negative', 'nan'): fee['charged_points'] = -1 if fault == 'negative' else 'NaN'
    elif fault == 'new-oss': current['oss_history'].append({'id': 'new'})
    elif fault == 'new-submission': current['submissions'].append({'id': 'new'})
    elif fault == 'unknown-field': current['unexpected'] = 'change'
    else: current['schema'] = True
    assert not native_driver._repair_verified_ledger_extension(old, current, 'source', 'course')


def test_main_repair_regeneration_cannot_repeat_or_hide_asr_charges():
    previous={'jobs':[{'id':'one','operation_id':'asr:one','status':'done'}],
              'settlements':[{'id':'charge','operation_id':'asr:one','charged_seconds':600}],'charged_seconds':600}
    current=deepcopy(previous)
    assert native_driver._repair_no_new_asr(previous,current)
    current['jobs'].append({'id':'two','operation_id':'asr:two','status':'done'})
    assert not native_driver._repair_no_new_asr(previous,current)
    current=deepcopy(previous);current['jobs'].append(deepcopy(current['jobs'][0]))
    assert not native_driver._repair_no_new_asr(previous,current)
    current=deepcopy(previous);current['settlements'][0]['charged_seconds']=601
    assert not native_driver._repair_no_new_asr(previous,current)


def test_main_repair_fresh_head_timestamp_may_update_but_status_identity_and_freshness_may_not():
    previous={'schema':1,'complete':True,'source':'account_cumulative_usage_rows','observed_at':100,
              'oss_history':[{'id':1,'object_key_sha256':'a'*64,'observed_at':99,
                              'current_head':{'exists':False,'status':404,'bytes':None,'checked_at':100}}]}
    current=deepcopy(previous);current['observed_at']=200;current['oss_history'][0]['current_head']['checked_at']=200
    assert native_driver._repair_ledger_equal(previous,current)
    assert native_driver._repair_fresh_head_times(current,199,201)
    assert not native_driver._repair_fresh_head_times(current,230,231)
    current['oss_history'][0]['current_head']['status']=200
    assert not native_driver._repair_ledger_equal(previous,current)
    current=deepcopy(previous);current['oss_history'][0]['object_key_sha256']='b'*64
    assert not native_driver._repair_ledger_equal(previous,current)


@pytest.mark.parametrize('fault',['none','missing-workspace-change','extra-asr-change','arbitrary-allowlist','38-extra-change'])
def test_main_repair39_accepts_only_two_exact_reviewed_module_changes(tmp_path,fault):
    run,proof,recording=repair_driver_fixture(tmp_path)
    proof['current_version']='0.1.39';run.context['version']='0.1.39'
    proof['allowed_changed_files']=['pku_sync/lecture_source_download.py','pku_sync/panel/notion_workspace.py']
    manifest_path=Path(proof['current_product_manifest']['path'])
    manifest=json.loads(manifest_path.read_text('utf-8'))
    names=['pku_sync/panel/notion_workspace.py']
    if fault=='missing-workspace-change':names=[]
    if fault=='extra-asr-change':names.append('pku_sync/media.py')
    if fault=='arbitrary-allowlist':proof['allowed_changed_files'].append('pku_sync/media.py')
    if fault=='38-extra-change':proof['current_version']='0.1.38';run.context['version']='0.1.38'
    for name in names:
        path=Path(manifest['product_root'])/name;path.write_bytes(b'new reviewed workspace fix')
        manifest['files'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest),'utf-8')
    proof['current_product_manifest']['sha256']=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    proof_path=Path(run.config['main_chain_resume']['proof_path']);proof_path.write_text(json.dumps(proof),'utf-8')
    run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if fault=='none':
        assert native_driver._load_main_chain_resume(run)['proof']['current_version']=='0.1.39'
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def test_main_repair_real_historical_none_operation_ids_preserve_all_existing_asr_rows():
    # Minimal schema extracted from the real r8/r9 account readbacks. Actual
    # immutable snapshot hashes and offline results remain in the handoff;
    # repository checks must not depend on private, ignored account evidence.
    before={'schema':1,'complete':True,'source':'account_cumulative_usage_rows',
        'jobs':[{'id':'asr-job','operation_id':'asr:one','status':'done'}],
        'settlements':[{'id':'asr-charge','operation_id':'asr:one','charged_seconds':600},
                       {'id':'legacy-charge','operation_id':None,'charged_seconds':None,'charged_points':1.0}],
        'charged_seconds':600,'observed_at':1000}
    current=deepcopy(before)
    current['observed_at']=1001
    assert any(row['operation_id'] is None for row in before['settlements'])
    assert native_driver._repair_ledger_equal(before,current)
    assert native_driver._repair_no_new_asr(before,current)


@pytest.mark.parametrize('fault',['none','new-none-seconds','new-asr','deleted-old-asr','old-none-edit','old-asr-fee','total-seconds','new-missing-id'])
def test_main_repair_unknown_or_changed_asr_effect_cannot_hide_behind_historical_none(fault):
    before={'jobs':[{'id':'asr-job','operation_id':'asr:one','status':'done'}],
        'settlements':[{'id':'old-charge','operation_id':'asr:one','charged_seconds':600},
                       {'id':'legacy','operation_id':None,'charged_seconds':None,'charged_points':1.0}],
        'charged_seconds':600}
    current=deepcopy(before)
    current['jobs'].append({'id':'new-llm','operation_id':'e'*32,'purpose':'lecture-notes','seconds_charged':None})
    current['settlements'].append({'id':'llm-charge','operation_id':'e'*32,'charged_seconds':None,'charged_points':0.01})
    if fault=='new-none-seconds':current['settlements'].append({'id':'unknown-charge','operation_id':None,'charged_seconds':600})
    if fault=='new-asr':current['jobs'].append({'id':'second-asr','operation_id':'asr:two','seconds_charged':600})
    if fault=='deleted-old-asr':current['jobs'].pop(0)
    if fault=='old-none-edit':current['settlements'][1]['charged_points']=2.0
    if fault=='old-asr-fee':current['settlements'][0]['charged_seconds']=601
    if fault=='total-seconds':current['charged_seconds']=601
    if fault=='new-missing-id':current['jobs'][1]['operation_id']=None
    assert native_driver._repair_no_new_asr(before,current) is (fault=='none')


def test_main_workspace_binding_records_actual_bytes_without_secret_or_student_body(tmp_path):
    app=tmp_path/'primary';(app/'data').mkdir(parents=True)
    raw=json.dumps({'schema':2,'id':'a'*32,'parent_id':'b'*32,'account_namespace':'private namespace',
                    'token':'secret-token','student_body':'private student text',
                    'courses':{'course':{'id':'c'*32,'lectures':{'one':{}},'historical_lectures':{}}}}).encode()
    (app/'data/notion-workspace-v2.json').write_bytes(raw)
    config={'test_root':str(tmp_path),'instances':{'primary':{'app_dir':str(app)}}}
    run=SimpleNamespace(need=lambda key:core.need(config,key))
    result=native_driver._main_workspace_binding(run,'course')
    assert result['sha256']==hashlib.sha256(raw).hexdigest()
    assert result['binding']['active_lecture_count']==1
    assert 'secret-token' not in json.dumps(result) and 'private student text' not in json.dumps(result)


def test_main_failed_selected_refresh_retains_actual_terminal_before_rejecting_success(tmp_path,monkeypatch):
    from product_e2e import learning_journeys,catalog_journeys
    app=tmp_path/'primary';(app/'data').mkdir(parents=True)
    workspace={'schema':2,'id':'a'*32,'parent_id':'b'*32,'courses':{}}
    (app/'data/notion-workspace-v2.json').write_text(json.dumps(workspace),'utf-8')
    cfg={'test_root':str(tmp_path),'instances':{'primary':{'app_dir':str(app)}},
         'learning':{'course_id':'course','source_id':'source','notion_test_parent_id':'b'*32},
         'quiz':{'course_id':'course','source_ids':['source']}}
    checks={}
    run=SimpleNamespace(config=cfg,need=lambda key:core.need(cfg,key),session=lambda *args:object(),actions=[],
                        check=lambda key,value:checks.update({key:value}))
    terminal={'id':'actual-task','kind':'sync','state':'failed','course_id':'course','scope':'selected',
              'stage':'preserved real failure','failure_id':'actual-failure','observed_elapsed_seconds':2.0}
    monkeypatch.setattr(catalog_journeys,'_catalog_setup',lambda *args,**kwargs:None)
    monkeypatch.setattr(catalog_journeys,'_refresh_course',lambda *args:dict(terminal))
    monkeypatch.setattr(learning_journeys,'_open_course',lambda *args:(object(),{}))
    monkeypatch.setattr(learning_journeys,'_workspace',lambda *args,**kwargs:workspace)
    monkeypatch.setattr(learning_journeys,'_page',lambda *args:({},True))
    with pytest.raises(AssertionError,match='refresh failed'):native_driver._fresh_main_chain(run)
    assert checks[native_driver.MAIN_CHECKS[0]] is False
    assert run.actions[-1]=={'event':'main_actual_selected_refresh_terminal',**terminal}


@pytest.mark.parametrize('fault',['none','missing-app-change','extra-asr-change','arbitrary-allowlist','duplicate-allowlist','39-extra-change'])
def test_main_repair40_allows_only_three_reviewed_physical_changes(tmp_path,fault):
    run,proof,_=repair_driver_fixture(tmp_path)
    proof['current_version']='0.1.40';run.context['version']='0.1.40'
    proof['allowed_changed_files']=['pku_sync/lecture_source_download.py','pku_sync/panel/notion_workspace.py','pku_sync/panel/static/app.js']
    path=Path(proof['current_product_manifest']['path'])
    manifest=json.loads(path.read_text('utf-8'))
    names=['pku_sync/panel/notion_workspace.py','pku_sync/panel/static/app.js']
    if fault=='missing-app-change':names.pop()
    if fault=='extra-asr-change':names.append('pku_sync/media.py')
    if fault=='arbitrary-allowlist':proof['allowed_changed_files'].append('pku_sync/media.py')
    if fault=='duplicate-allowlist':proof['allowed_changed_files'].append('pku_sync/panel/static/app.js')
    if fault=='39-extra-change':
        proof['current_version']='0.1.39';run.context['version']='0.1.39'
        proof['allowed_changed_files'].pop()
    for name in names:
        module=Path(manifest['product_root'])/name;module.write_bytes(b'actual reviewed same-goal fix')
        manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest),'utf-8')
    proof['current_product_manifest']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    proof_path=Path(run.config['main_chain_resume']['proof_path']);proof_path.write_text(json.dumps(proof),'utf-8')
    run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if fault=='none':
        assert native_driver._load_main_chain_resume(run)['proof']['current_version']=='0.1.40'
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


@pytest.mark.parametrize('failed',[False,True])
def test_native_request_recorder_observes_safe_physical_times_status_and_failure(run,monkeypatch,failed):
    handlers={}
    page=SimpleNamespace(url='http://127.0.0.1:8791/app',on=lambda event,callback:handlers.update({event:callback}))
    ui=core.NativeUI(run,'primary',page=page)
    clock=iter([100.0,101.5])
    monkeypatch.setattr(core.time,'monotonic',lambda:next(clock))
    request=SimpleNamespace(method='GET',url='http://127.0.0.1:8791/api/campus/notion/parents?selected_only=true&token=private-secret',
        failure='net::ERR_FAILED https://secret.invalid/private-secret')
    handlers['request'](request)
    if failed:
        handlers['requestfailed'](request)
    else:
        handlers['response'](SimpleNamespace(request=request,status=200))
        handlers['requestfinished'](request)
    row=run.network[0]
    assert row['method']=='GET' and row['host']=='127.0.0.1' and row['path']=='/api/campus/notion/parents'
    assert row['started_monotonic']==100.0 and row['finished_monotonic']==101.5
    assert row['started_at'] and row['finished_at']
    assert row['status']==(None if failed else 200) and row['request_failed'] is failed
    assert row['request_failed_type']==('transport_failure' if failed else None)
    assert row['selected_only'] is True
    assert 'private-secret' not in json.dumps(run.network) and 'secret.invalid' not in json.dumps(run.network)
    assert not ui._pending_requests


@pytest.mark.parametrize('suffix,expected',[('?selected_only=false',False),('?selected_only=true',True),
    ('?selected_only=yes',None),('?selected_only=true&selected_only=false',None),
    ('/other?selected_only=true',None)])
def test_native_request_recorder_selected_only_is_an_exact_safe_boolean(run,suffix,expected):
    ui=core.NativeUI(run,'primary')
    request=SimpleNamespace(method='GET',url='http://127.0.0.1:8791/api/campus/notion/parents'+suffix)
    ui._request_event(request)
    row=run.network[0]
    assert row.get('selected_only') is expected
    assert ('selected_only' in row) is (expected is not None)
    assert row['request_failed'] is None and row['status'] is None and row['finished_monotonic'] is None


def repair_summary_attempt_ledger_fixture():
    before={'schema':1,'complete':True,'source':'account_cumulative_usage_rows','observed_at':1000,
        'jobs':[{'id':'old-asr','operation_id':'asr:original','status':'succeeded'}],
        'settlements':[{'id':'old-asr-charge','operation_id':'asr:original','charged_seconds':600},
                       {'id':'legacy','operation_id':None,'charged_points':1}],
        'charged_seconds':600,'charged_points':10.0,'oss_objects':[],'oss_history':[],
        'unattributed_settlement_count':1}
    after=deepcopy(before);after['observed_at']=1001;after['charged_points']=10.297
    for i in range(22):
        op=f'{i+1:032x}'
        after['jobs'].append({'id':f'job{i}','operation_id':op,'operation':'notes','purpose':'lecture-notes',
            'status':'succeeded','source_id':'source','course_id':'course','points_charged':0.0135})
        after['settlements'].append({'id':f'charge{i}','operation_id':op,'operation':'notes','charged_points':0.0135})
    return before,after


@pytest.mark.parametrize('fault',['none','extra-job','missing-charge','unsettled','source','course','asr',
    'seconds','old-row','unknown-field','points','duplicate-operation'])
def test_main_repair41_additional_notes_attempt_preserves_exact_account_boundary(fault):
    before,after=repair_summary_attempt_ledger_fixture()
    if fault=='extra-job':after['jobs'].append({'id':'unexpected','operation_id':'extra'})
    if fault=='missing-charge':after['settlements'].pop()
    if fault=='unsettled':after['jobs'][-1]['status']='running'
    if fault=='source':after['jobs'][-1]['source_id']='other'
    if fault=='course':after['jobs'][-1]['course_id']='other'
    if fault=='asr':after['jobs'][-1]['operation_id']='asr:repeat'
    if fault=='seconds':after['settlements'][-1]['charged_seconds']=1
    if fault=='old-row':after['settlements'][1]['charged_points']=2
    if fault=='unknown-field':after['unattributed_settlement_count']=2
    if fault=='points':after['charged_points']=10.298
    if fault=='duplicate-operation':after['jobs'][-1]['operation_id']=after['jobs'][-2]['operation_id']
    assert native_driver._repair_summary_attempt_ledger(before,after,'source','course') is (fault=='none')


@pytest.mark.parametrize('fault',['none','other-function','signature','import','missing-function'])
def test_main_repair41_media_ast_allows_only_existing_citation_function_body(tmp_path,fault):
    old=tmp_path/'old.py';new=tmp_path/'new.py'
    text='import re\n\ndef _check_source_time_citations(chapter, window, audit=None):\n    return chapter\n\ndef transcribe(value):\n    return value\n'
    old.write_text(text,'utf-8');text=text.replace('return chapter','return chapter.strip()')
    if fault=='other-function':text=text.replace('return value','return value.strip()')
    if fault=='signature':text=text.replace('audit=None','audit=True')
    if fault=='import':text=text.replace('import re','import os')
    if fault=='missing-function':text=text.replace('_check_source_time_citations','other_checker')
    new.write_text(text,'utf-8')
    assert native_driver._repair_media_citation_ast(old,new) is (fault=='none')


def repair41_driver_fixture(tmp_path):
    import shutil
    run,proof,_=repair_driver_fixture(tmp_path)
    before,after=repair_summary_attempt_ledger_fixture()
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    prior_path=Path(proof['prior_product_manifest']['path']);prior=json.loads(prior_path.read_text('utf-8'))
    prior_root=Path(prior['product_root']);media='import re\ndef _check_source_time_citations(chapter, window, audit=None):\n    return chapter\n'
    (prior_root/'pku_sync/media.py').write_text(media,'utf-8')
    prior['files']['pku_sync/media.py']=hashlib.sha256((prior_root/'pku_sync/media.py').read_bytes()).hexdigest()
    proof['prior_product_manifest']=dump(prior_path,prior)
    current_path=Path(proof['current_product_manifest']['path']);current=json.loads(current_path.read_text('utf-8'))
    current_root=Path(current['product_root'])
    for name in ('pku_sync/panel/notion_workspace.py','pku_sync/panel/static/app.js','pku_sync/media.py'):
        p=current_root/name;p.write_text(media.replace('return chapter','return chapter.strip()') if name.endswith('media.py') else 'reviewed repair','utf-8')
        current['files'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
    proof['current_product_manifest']=dump(current_path,current)
    root40=tmp_path/'private/installed40/resources/runtime/Lib/site-packages'
    shutil.copytree(current_root/'pku_sync',root40/'pku_sync')
    (root40/'pku_sync/media.py').write_text(media,'utf-8')
    binary40=root40.parents[3]/'pku-desktop.exe';binary40.write_bytes(b'actual fixture40 binary')
    artifact40=tmp_path/'candidate40.exe';artifact40.write_bytes(b'actual fixture40 artifact')
    artifactsha=hashlib.sha256(artifact40.read_bytes()).hexdigest()
    manifest40={'product_root':str(root40),'artifact_sha256':artifactsha,'files':{
        name:hashlib.sha256((root40/name).read_bytes()).hexdigest() for name in prior['files']}}
    manifest40spec=dump(tmp_path/'manifest40.json',manifest40)
    r8case=Path(proof['prior_report_path']).parent
    proof['prior_before_ledger']=dump(r8case/'oracle-before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    proof['prior_processed_ledger']=dump(r8case/'oracle-post.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    original=json.loads(Path(proof['prior_report_path']).read_text('utf-8'))
    for row in original['result']['evidence']:
        if row['path'] in ('oracle-before.json','oracle-post.json'):row['sha256']=proof['prior_before_ledger' if row['path']=='oracle-before.json' else 'prior_processed_ledger']['sha256']
    originalspec=dump(Path(proof['prior_report_path']),original);proof['prior_report_sha256']=originalspec['sha256']
    case=tmp_path/'r11'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.40','artifact_sha256':artifactsha,
        'installed_path':str(binary40),'installed_sha256':hashlib.sha256(binary40.read_bytes()).hexdigest(),'alive':False}]})
    report={'kind':'installed-native-main-chain-repair-continuation-not-release-verdict','target':'windows','version':'0.1.40',
        'source_digest':'d'*64,'finished_source_digest':'d'*64,'artifact_sha256':artifactsha,
        'complete_main_chains_passed':0,'complete_cases_passed':0,'complete_journeys_passed':0,'full_release_gate_passed':False,
        'result':{'entry':'installed-native','mocked':False,'injection':None,'blockers':['execution_failed_AssertionError'],
            'assertions':{key:i<3 for i,key in enumerate(native_driver.MAIN_CHECKS)},
            'evidence':[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]],
            'repair_continuation':{'kind':'same-goal-main-chain-repair-continuation','prior_version':'0.1.37','current_version':'0.1.40',
                'prior_report_sha256':proof['prior_report_sha256'],'prior_artifact_sha256':proof['prior_artifact_sha256'],
                'current_artifact_sha256':artifactsha,'source_id':'source','no_new_asr':True,
                'prior_native_task_terminal_recorded':False,
                'original_asr_reused_by_verified_hash_and_original_ledger':False,
                'current_native_task':{'id':'actual-failed-task','kind':'process','state':'failed','recording_id':'recording'},
                'repair_summary_cost':{'charged_points':0.297,'charged_seconds':0}}}}
    attempt={'report':dump(case/'report.json',report),'artifact':{'path':str(artifact40),'sha256':artifactsha},
        'product_manifest':manifest40spec,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,
        'source_digest':'d'*64,'native_task_id':'actual-failed-task'}
    proof['additional_summary_attempt']=attempt
    proof['current_version']='0.1.41';run.context['version']='0.1.41'
    proof['allowed_changed_files']=['pku_sync/lecture_source_download.py','pku_sync/panel/notion_workspace.py','pku_sync/panel/static/app.js','pku_sync/media.py']
    return run,proof


@pytest.mark.parametrize('fault',['none','missing-attempt','extra-attempt-field','source-digest','live-runtime','wrong-task','unsettled','evidence-bytes','fourth-other-function','extra-module','missing-media'])
def test_main_repair41_requires_exact_closed_failed_attempt_and_physical_modules(tmp_path,fault):
    run,proof=repair41_driver_fixture(tmp_path)
    attempt=proof['additional_summary_attempt']
    if fault=='missing-attempt':proof.pop('additional_summary_attempt')
    if fault=='extra-attempt-field':attempt['allow_missing']=True
    if fault=='source-digest':attempt['source_digest']='e'*64
    if fault=='wrong-task':attempt['native_task_id']='unknown-task'
    if fault in ('live-runtime','unsettled','evidence-bytes'):
        name='runtime.json' if fault=='live-runtime' else 'after.json'
        p=Path(attempt['report']['path']).parent/name
        value=json.loads(p.read_text('utf-8'))
        if fault=='live-runtime':value['processes'][0]['alive']=True
        elif fault=='unsettled':value['readback']['jobs'][-1]['status']='running'
        else:value['extra']='changed'
        p.write_text(json.dumps(value),'utf-8')
        if fault!='evidence-bytes':
            sha=hashlib.sha256(p.read_bytes()).hexdigest()
            if fault=='unsettled':attempt['after_ledger']['sha256']=sha
            report_path=Path(attempt['report']['path']);report=json.loads(report_path.read_text('utf-8'))
            for row in report['result']['evidence']:
                if row['path']==name:row['sha256']=sha
            report_path.write_text(json.dumps(report),'utf-8');attempt['report']['sha256']=hashlib.sha256(report_path.read_bytes()).hexdigest()
    if fault in ('fourth-other-function','extra-module','missing-media'):
        spec=proof['current_product_manifest'];p=Path(spec['path']);manifest=json.loads(p.read_text('utf-8'))
        name='pku_sync/media.py' if fault!='extra-module' else 'pku_sync/lecture_source_download.py'
        module=Path(manifest['product_root'])/name
        if fault=='missing-media':module.write_bytes((Path(json.loads(Path(proof['prior_product_manifest']['path']).read_text('utf-8'))['product_root'])/name).read_bytes())
        elif fault=='fourth-other-function':module.write_text(module.read_text('utf-8')+'\ndef transcribe(value):\n    return value\n','utf-8')
        else:
            name='pku_sync/other.py';module=Path(manifest['product_root'])/name;module.write_text('extra','utf-8')
        manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();p.write_text(json.dumps(manifest),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    proof_path=Path(run.config['main_chain_resume']['proof_path']);proof_path.write_text(json.dumps(proof),'utf-8')
    run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['proof']['current_version']=='0.1.41'
        assert result['fresh_baseline']['charged_points']==10.297
        assert result['processed']['charged_points']==10.0
        assert result['additional_summary_attempt']['jobs']==22
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair42_driver_fixture(tmp_path):
    import shutil
    run,proof=repair41_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    first=proof.pop('additional_summary_attempt')
    claim='POLICY="strict"\nSCHEMA={"all_pass":True}\ndef audit_chapter(note, transcript):\n    return note\ndef validate(result):\n    return result\n'
    for spec in (proof['prior_product_manifest'],proof['current_product_manifest'],first['product_manifest']):
        p=Path(spec['path']);v=json.loads(p.read_text('utf-8'));root=Path(v['product_root'])
        (root/'pku_sync/claim_audit.py').write_text(claim,'utf-8')
        v['files']['pku_sync/claim_audit.py']=hashlib.sha256((root/'pku_sync/claim_audit.py').read_bytes()).hexdigest()
        media=root/'pku_sync/media.py'
        media.write_text(media.read_text('utf-8')+'\ndef write_notes(notes):\n    return notes\n','utf-8')
        v['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest()
        p.write_text(json.dumps(v),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    (run.context['root']/'pku_sync/claim_audit.py').write_text(claim,'utf-8')
    curpath=Path(proof['current_product_manifest']['path']);current=json.loads(curpath.read_text('utf-8'))
    root41=tmp_path/'private/installed41/resources/runtime/Lib/site-packages'
    shutil.copytree(Path(current['product_root'])/'pku_sync',root41/'pku_sync')
    binary41=root41.parents[3]/'pku-desktop.exe';binary41.write_bytes(b'actual fixture41 binary')
    artifact41=tmp_path/'candidate41.exe';artifact41.write_bytes(b'actual fixture41 artifact')
    artifactsha=hashlib.sha256(artifact41.read_bytes()).hexdigest()
    manifest41=dump(tmp_path/'manifest41.json',{'product_root':str(root41),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root41/name).read_bytes()).hexdigest() for name in current['files']}})
    changed=Path(current['product_root'])/'pku_sync/claim_audit.py';changed.write_text(claim.replace('return note','return note.strip()'),'utf-8')
    current['files']['pku_sync/claim_audit.py']=hashlib.sha256(changed.read_bytes()).hexdigest()
    media=Path(current['product_root'])/'pku_sync/media.py'
    media.write_text(media.read_text('utf-8').replace('return notes','return notes.strip()'),'utf-8')
    current['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest()
    proof['current_product_manifest']=dump(curpath,current)
    before=json.loads(Path(first['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before)
    after['observed_at']=1002;after['charged_points']=10.54
    for i in range(16):
        op=f'{i+100:032x}'
        after['jobs'].append({'id':f'r12job{i}','operation_id':op,'operation':'notes','purpose':'lecture-notes',
            'status':'succeeded','source_id':'source','course_id':'course','points_charged':0.0151875})
        after['settlements'].append({'id':f'r12charge{i}','operation_id':op,'operation':'notes','charged_points':0.0151875})
    case=tmp_path/'r12'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.41','artifact_sha256':artifactsha,
        'installed_path':str(binary41),'installed_sha256':hashlib.sha256(binary41.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(first['report']['path']).read_text('utf-8'))
    report.update(version='0.1.41',artifact_sha256=artifactsha,source_digest='e'*64,finished_source_digest='e'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    continuation=report['result']['repair_continuation']
    continuation.update(current_version='0.1.41',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task12','kind':'process','state':'failed','recording_id':'recording'},
        repair_summary_cost={'charged_points':0.243,'charged_seconds':0},
        additional_summary_attempt={'report_sha256':first['report']['sha256'],'source_digest':first['source_digest'],
            'native_task_id':first['native_task_id'],'version':'0.1.40','charged_points':0.297,'charged_seconds':0,'jobs':22})
    second={'report':dump(case/'report.json',report),'artifact':{'path':str(artifact41),'sha256':artifactsha},
        'product_manifest':manifest41,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,
        'source_digest':'e'*64,'native_task_id':'actual-failed-task12'}
    proof['current_version']='0.1.42';run.context['version']='0.1.42'
    proof['allowed_changed_files'].append('pku_sync/claim_audit.py')
    proof['additional_summary_attempts']=[first,second]
    return run,proof


@pytest.mark.parametrize('fault',['none','missing-stage','extra-stage','swapped-stage','singleton-also','broken-report-chain',
    'wrong-before','old-row-changed','unsettled','live-runtime','evidence-bytes','policy','validator','signature','extra-module'])
def test_main_repair42_requires_two_exact_contiguous_failed_boundaries_and_five_modules(tmp_path,fault):
    run,proof=repair42_driver_fixture(tmp_path)
    second=proof['additional_summary_attempts'][1]
    if fault=='missing-stage':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(second))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='singleton-also':proof['additional_summary_attempt']=proof['additional_summary_attempts'][0]
    if fault in ('broken-report-chain','wrong-before','old-row-changed','unsettled','live-runtime','evidence-bytes'):
        report_path=Path(second['report']['path']);report=json.loads(report_path.read_text('utf-8'))
        if fault=='broken-report-chain':report['result']['repair_continuation']['additional_summary_attempt']['report_sha256']='f'*64
        else:
            name='before.json' if fault=='wrong-before' else 'runtime.json' if fault=='live-runtime' else 'after.json'
            p=report_path.parent/name;v=json.loads(p.read_text('utf-8'))
            if fault=='wrong-before':v['readback']['charged_points']=10.0
            elif fault=='old-row-changed':v['readback']['settlements'][1]['charged_points']=2
            elif fault=='unsettled':v['readback']['jobs'][-1]['status']='running'
            elif fault=='live-runtime':v['processes'][0]['alive']=True
            else:v['extra']='unverified mutation'
            p.write_text(json.dumps(v),'utf-8')
            if fault!='evidence-bytes':
                sha=hashlib.sha256(p.read_bytes()).hexdigest()
                if fault!='live-runtime':second['before_ledger' if name=='before.json' else 'after_ledger']['sha256']=sha
                for row in report['result']['evidence']:
                    if row['path']==name:row['sha256']=sha
        report_path.write_text(json.dumps(report),'utf-8');second['report']['sha256']=hashlib.sha256(report_path.read_bytes()).hexdigest()
    if fault in ('policy','validator','signature','extra-module'):
        spec=proof['current_product_manifest'];p=Path(spec['path']);v=json.loads(p.read_text('utf-8'))
        name='pku_sync/claim_audit.py';module=Path(v['product_root'])/name;text=module.read_text('utf-8')
        if fault=='policy':text=text.replace('POLICY="strict"','POLICY="allow"')
        if fault=='validator':text=text.replace('return result','return True')
        if fault=='signature':text=text.replace('note, transcript','note, transcript=None')
        if fault=='extra-module':name='pku_sync/other.py';module=Path(v['product_root'])/name;text='extra'
        module.write_text(text,'utf-8');v['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest()
        p.write_text(json.dumps(v),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    proof_path=Path(run.config['main_chain_resume']['proof_path']);proof_path.write_text(json.dumps(proof),'utf-8')
    run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==10.54
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


@pytest.mark.parametrize('fault',['none','asr-helper','constant','import','write-signature','missing-write','duplicate-write'])
def test_main_repair42_only_two_reviewed_media_bodies_and41_remains_one(tmp_path,fault):
    old=tmp_path/'old.py';new=tmp_path/'new.py'
    text='import re\nPOLICY="strict"\ndef _check_source_time_citations(chapter, window):\n    return chapter\ndef write_notes(notes):\n    return notes\ndef transcribe(data):\n    return data\n'
    old.write_text(text,'utf-8')
    changed=text.replace('return chapter','return chapter.strip()').replace('return notes','return notes.strip()')
    if fault=='asr-helper':changed=changed.replace('return data','return data.strip()')
    if fault=='constant':changed=changed.replace('POLICY="strict"','POLICY="allow"')
    if fault=='import':changed=changed.replace('import re','import os')
    if fault=='write-signature':changed=changed.replace('write_notes(notes)','write_notes(notes, skip=False)')
    if fault=='missing-write':changed=changed.replace('write_notes','other_writer')
    if fault=='duplicate-write':changed+='\ndef write_notes(notes):\n    return True\n'
    new.write_text(changed,'utf-8')
    assert native_driver._repair42_media_ast(old,new) is (fault=='none')
    assert native_driver._repair_media_citation_ast(old,new) is False


def repair43_driver_fixture(tmp_path):
    import shutil
    run,proof=repair42_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    second=proof['additional_summary_attempts'][1]
    p=Path(proof['current_product_manifest']['path']);current=json.loads(p.read_text('utf-8'))
    root42=tmp_path/'private/installed42/resources/runtime/Lib/site-packages'
    shutil.copytree(Path(current['product_root'])/'pku_sync',root42/'pku_sync')
    binary42=root42.parents[3]/'pku-desktop.exe';binary42.write_bytes(b'actual fixture42 binary')
    artifact42=tmp_path/'candidate42.exe';artifact42.write_bytes(b'actual fixture42 artifact')
    artifactsha=hashlib.sha256(artifact42.read_bytes()).hexdigest()
    manifest42=dump(tmp_path/'manifest42.json',{'product_root':str(root42),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root42/name).read_bytes()).hexdigest() for name in current['files']}})
    before=json.loads(Path(second['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before)
    after['observed_at']=1003;after['charged_points']=11.408
    for i in range(43):
        op=f'{i+200:032x}';points=0.028 if i==42 else 0.02
        after['jobs'].append({'id':f'r13job{i}','operation_id':op,'operation':'notes','purpose':'lecture-notes',
            'status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r13charge{i}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r13'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.42','artifact_sha256':artifactsha,
        'installed_path':str(binary42),'installed_sha256':hashlib.sha256(binary42.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(second['report']['path']).read_text('utf-8'))
    report.update(version='0.1.42',artifact_sha256=artifactsha,source_digest='f'*64,finished_source_digest='f'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    continuation=report['result']['repair_continuation'];first_meta=continuation.pop('additional_summary_attempt')
    second_meta={'report_sha256':second['report']['sha256'],'source_digest':second['source_digest'],'native_task_id':second['native_task_id'],
        'version':'0.1.41','charged_points':0.243,'charged_seconds':0,'jobs':16}
    continuation.update(current_version='0.1.42',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task13','kind':'process','state':'failed','recording_id':'recording'},
        repair_summary_cost={'charged_points':0.868,'charged_seconds':0},additional_summary_attempts=[first_meta,second_meta])
    third={'report':dump(case/'report.json',report),'artifact':{'path':str(artifact42),'sha256':artifactsha},
        'product_manifest':manifest42,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,
        'source_digest':'f'*64,'native_task_id':'actual-failed-task13'}
    proof['current_version']='0.1.43';run.context['version']='0.1.43'
    proof['additional_summary_attempts'].append(third)
    return run,proof


@pytest.mark.parametrize('fault',['none','missing-stage','extra-stage','swapped-stage','broken-history','boolean-history',
    'wrong-before','old-row','unsettled','asr','wrong-cost','live-runtime','evidence-bytes','sixth-module','helper-change','version44','42-three-stages'])
def test_main_repair43_requires_exact_three_failed_boundaries_and_rejects_future_versions(tmp_path,fault):
    run,proof=repair43_driver_fixture(tmp_path);third=proof['additional_summary_attempts'][2]
    if fault=='missing-stage':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(third))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='version44':proof['current_version']='0.1.44';run.context['version']='0.1.44'
    if fault=='42-three-stages':proof['current_version']='0.1.42';run.context['version']='0.1.42'
    if fault in ('broken-history','boolean-history','wrong-before','old-row','unsettled','asr','wrong-cost','live-runtime','evidence-bytes'):
        report_path=Path(third['report']['path']);report=json.loads(report_path.read_text('utf-8'))
        if fault=='broken-history':report['result']['repair_continuation']['additional_summary_attempts'][0]['report_sha256']='0'*64
        elif fault=='boolean-history':report['result']['repair_continuation']['additional_summary_attempts'][0]['charged_seconds']=False
        else:
            name='before.json' if fault=='wrong-before' else 'runtime.json' if fault=='live-runtime' else 'after.json'
            path=report_path.parent/name;value=json.loads(path.read_text('utf-8'))
            if fault=='wrong-before':value['readback']['charged_points']=10.0
            elif fault=='old-row':value['readback']['settlements'][1]['charged_points']=2
            elif fault=='unsettled':value['readback']['jobs'][-1]['status']='running'
            elif fault=='asr':value['readback']['jobs'][-1]['operation_id']='asr:repeat'
            elif fault=='wrong-cost':value['readback']['charged_points']=11.409
            elif fault=='live-runtime':value['processes'][0]['alive']=True
            else:value['extra']='unverified change'
            path.write_text(json.dumps(value),'utf-8')
            if fault!='evidence-bytes':
                sha=hashlib.sha256(path.read_bytes()).hexdigest()
                if fault!='live-runtime':third['before_ledger' if name=='before.json' else 'after_ledger']['sha256']=sha
                for row in report['result']['evidence']:
                    if row['path']==name:row['sha256']=sha
        report_path.write_text(json.dumps(report),'utf-8');third['report']['sha256']=hashlib.sha256(report_path.read_bytes()).hexdigest()
    if fault in ('sixth-module','helper-change'):
        spec=proof['current_product_manifest'];path=Path(spec['path']);manifest=json.loads(path.read_text('utf-8'))
        name='pku_sync/other.py' if fault=='sixth-module' else 'pku_sync/media.py';module=Path(manifest['product_root'])/name
        module.write_text('extra' if fault=='sixth-module' else module.read_text('utf-8')+'\ndef transcribe(data):\n    return data\n','utf-8')
        manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();path.write_text(json.dumps(manifest),'utf-8');spec['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    path=Path(run.config['main_chain_resume']['proof_path']);path.write_text(json.dumps(proof),'utf-8')
    run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    if fault=='none':
        value=native_driver._load_main_chain_resume(run)
        assert value['fresh_baseline']['charged_points']==11.408
        assert value['processed']['charged_points']==10.0
        assert [row['jobs'] for row in value['additional_summary_attempt']['stages']]==[22,16,43]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair44_driver_fixture(tmp_path):
    import shutil
    run,proof=repair43_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    helper='\ndef _repair_factual_conflicts(llm, model: str, transcript: str, draft: str, issues: list[str], *, settings, sources: list[dict] | None = None) -> str:\n    return draft\n'
    for spec in [proof['prior_product_manifest'],proof['current_product_manifest']]+[s['product_manifest'] for s in proof['additional_summary_attempts']]:
        path=Path(spec['path']);manifest=json.loads(path.read_text('utf-8'));media=Path(manifest['product_root'])/'pku_sync/media.py'
        media.write_text(media.read_text('utf-8')+helper,'utf-8');manifest['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest()
        path.write_text(json.dumps(manifest),'utf-8');spec['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    third=proof['additional_summary_attempts'][2]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root43=tmp_path/'private/installed43/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root43/'pku_sync')
    binary43=root43.parents[3]/'pku-desktop.exe';binary43.write_bytes(b'actual fixture43 binary')
    artifact43=tmp_path/'candidate43.exe';artifact43.write_bytes(b'actual fixture43 artifact');artifactsha=hashlib.sha256(artifact43.read_bytes()).hexdigest()
    manifest43=dump(tmp_path/'manifest43.json',{'product_root':str(root43),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root43/name).read_bytes()).hexdigest() for name in current['files']}})
    media=Path(current['product_root'])/'pku_sync/media.py';text=media.read_text('utf-8')
    text=text.replace('sources: list[dict] | None = None) -> str','sources: list[dict] | None = None, source_time_context: dict | None = None) -> str').replace('return draft','return draft.strip()')
    media.write_text(text,'utf-8');current['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(third['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1004;after['charged_points']=11.635
    for i in range(10):
        op=f'{i+300:032x}';points=0.047 if i==9 else 0.02
        after['jobs'].append({'id':f'r14job{i}','operation_id':op,'operation':'notes','purpose':'lecture-notes',
            'status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r14charge{i}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r14'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.43','artifact_sha256':artifactsha,
        'installed_path':str(binary43),'installed_sha256':hashlib.sha256(binary43.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(third['report']['path']).read_text('utf-8'))
    report.update(version='0.1.43',artifact_sha256=artifactsha,source_digest='a'*64,finished_source_digest='a'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];previous=c['additional_summary_attempts']
    previous.append({'report_sha256':third['report']['sha256'],'source_digest':third['source_digest'],'native_task_id':third['native_task_id'],
        'version':'0.1.42','charged_points':0.868,'charged_seconds':0,'jobs':43})
    c.update(current_version='0.1.43',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task14','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.227,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    closeout=dump(case/'safe-closeout.json',{'kind':'actual-installed43-summary-failure-safe-closeout-not-pass',
        'report_sha256':report_spec['sha256'],'source_digest_unchanged':True,'normal_window_quit_confirmed':True,
        'original_notes_and_transcript_unchanged':True,'new_jobs_terminal':True,'no_new_asr':True,
        'pending_intents':[],'projection_journals':[],'new_jobs':10,'new_settlements':10,'job_status_counts':{'succeeded':10},
        'charged_points':'0.227','charged_seconds':0,'active_oss_objects':0,'complete_main_chains_passed':0,
        'complete_cases_passed':0,'complete_journeys_passed':0,'full_release_gate_passed':False,
        'billing_ops_evidence':[ledgerbefore,ledgerafter]})
    fourth={'report':report_spec,'artifact':{'path':str(artifact43),'sha256':artifactsha},'product_manifest':manifest43,
        'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'a'*64,'native_task_id':'actual-failed-task14','safe_closeout':closeout}
    proof['current_version']='0.1.44';run.context['version']='0.1.44';proof['additional_summary_attempts'].append(fourth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len3','extra-stage','swapped-stage','dropped-fee','old-row','unsettled','asr',
    'unknown-writing','unsafe-quit','missing-closeout','old-stage-closeout','report-chain','helper-default','helper-positional',
    'helper-other-arg','checker-changed43','asr-helper','claim-changed43','other-module-changed43','version45'])
def test_main_repair44_exact_four_boundaries_closeout_and_one_optional_helper_keyword(tmp_path,fault):
    run,proof=repair44_driver_fixture(tmp_path);fourth=proof['additional_summary_attempts'][3]
    if fault=='old-len3':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(fourth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='missing-closeout':fourth.pop('safe_closeout')
    if fault=='old-stage-closeout':proof['additional_summary_attempts'][0]['safe_closeout']=deepcopy(fourth['safe_closeout'])
    if fault=='version45':proof['current_version']='0.1.45';run.context['version']='0.1.45'
    if fault in ('dropped-fee','old-row','unsettled','asr','report-chain'):
        p=Path(fourth['report']['path']);r=json.loads(p.read_text('utf-8'))
        if fault=='report-chain':r['result']['repair_continuation']['additional_summary_attempts'][2]['charged_points']=0
        else:
            name='after.json';vpath=p.parent/name;v=json.loads(vpath.read_text('utf-8'))
            if fault=='dropped-fee':v['readback']['charged_points']=11.408
            if fault=='old-row':v['readback']['settlements'][1]['charged_points']=2
            if fault=='unsettled':v['readback']['jobs'][-1]['status']='running'
            if fault=='asr':v['readback']['jobs'][-1]['operation_id']='asr:repeat'
            vpath.write_text(json.dumps(v),'utf-8');fourth['after_ledger']['sha256']=hashlib.sha256(vpath.read_bytes()).hexdigest()
            for row in r['result']['evidence']:
                if row['path']==name:row['sha256']=fourth['after_ledger']['sha256']
        p.write_text(json.dumps(r),'utf-8');fourth['report']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        cp=Path(fourth['safe_closeout']['path']);v=json.loads(cp.read_text('utf-8'));v['report_sha256']=fourth['report']['sha256'];cp.write_text(json.dumps(v),'utf-8');fourth['safe_closeout']['sha256']=hashlib.sha256(cp.read_bytes()).hexdigest()
    if fault in ('unknown-writing','unsafe-quit'):
        p=Path(fourth['safe_closeout']['path']);v=json.loads(p.read_text('utf-8'))
        if fault=='unknown-writing':v['pending_intents']=[{'phase':'writing-unknown'}]
        else:v['normal_window_quit_confirmed']=False
        p.write_text(json.dumps(v),'utf-8');fourth['safe_closeout']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault in ('helper-default','helper-positional','helper-other-arg','checker-changed43','asr-helper','claim-changed43','other-module-changed43'):
        spec=proof['current_product_manifest'];p=Path(spec['path']);manifest=json.loads(p.read_text('utf-8'));name='pku_sync/media.py'
        if fault=='claim-changed43':name='pku_sync/claim_audit.py'
        if fault=='other-module-changed43':name='pku_sync/panel/static/app.js'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='helper-default':text=text.replace('source_time_context: dict | None = None','source_time_context: dict | None = {}')
        if fault=='helper-positional':text=text.replace('*, settings, sources: list[dict] | None = None, source_time_context: dict | None = None','source_time_context: dict | None = None, *, settings, sources: list[dict] | None = None')
        if fault=='helper-other-arg':text=text.replace('issues: list[str]','issues: str')
        if fault=='checker-changed43':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='asr-helper':text+='\ndef transcribe(data):\n    return data\n'
        if fault=='claim-changed43':text=text.replace('return note.strip()','return note.upper()')
        if fault=='other-module-changed43':text+=' changed'
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest()
        p.write_text(json.dumps(manifest),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==11.635
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair45_driver_fixture(tmp_path):
    import shutil
    run,proof=repair44_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    fourth=proof['additional_summary_attempts'][3]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root44=tmp_path/'private/installed44/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root44/'pku_sync')
    binary44=root44.parents[3]/'pku-desktop.exe';binary44.write_bytes(b'actual fixture44 binary')
    artifact44=tmp_path/'candidate44.exe';artifact44.write_bytes(b'actual fixture44 artifact');artifactsha=hashlib.sha256(artifact44.read_bytes()).hexdigest()
    manifest44=dump(tmp_path/'manifest44.json',{'product_root':str(root44),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root44/name).read_bytes()).hexdigest() for name in current['files']}})
    media=Path(current['product_root'])/'pku_sync/media.py';media.write_text(media.read_text('utf-8').replace('return draft.strip()','return draft.rstrip()'),'utf-8')
    current['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(fourth['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1005;after['charged_points']=11.721
    op='4'*32
    after['jobs'].append({'id':'r15job','operation_id':op,'operation':'notes','purpose':'lecture-notes',
        'status':'succeeded','source_id':'source','course_id':'course','points_charged':0.086})
    after['settlements'].append({'id':'r15charge','operation_id':op,'operation':'notes','charged_points':0.086})
    case=tmp_path/'r15'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.44','artifact_sha256':artifactsha,
        'installed_path':str(binary44),'installed_sha256':hashlib.sha256(binary44.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(fourth['report']['path']).read_text('utf-8'))
    report.update(version='0.1.44',artifact_sha256=artifactsha,source_digest='b'*64,finished_source_digest='b'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({
        'report_sha256':fourth['report']['sha256'],'source_digest':fourth['source_digest'],'native_task_id':fourth['native_task_id'],
        'version':'0.1.43','charged_points':0.227,'charged_seconds':0,'jobs':10})
    c.update(current_version='0.1.44',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task15','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.086,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    lock_names=[
        'data/.projection-permits/16c1426fe665d592e7752e0390dfba878efcc9484c0a085ac9e18c06ed8b183f/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite',
        'data/.projection-receipts/55e54ce2bfafc81d77311a479fd4c038a5d59e4abd45df56ea4db960211fbc67/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite',
        'data/.projection-receipts/cf72c64376179358bca34b2aaaf96189ae92674c359e575ada60eaf4912e4a6b/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite']
    locks=[]
    for name in lock_names:
        p=Path(proof['app_dir'])/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'')
        locks.append({'relative_path':name,'bytes':0,'sha256':hashlib.sha256(b'').hexdigest()})
    closeout=dump(case/'safe-closeout.json',{'kind':'actual-installed44-summary-failure-safe-closeout-not-pass',
        'report_sha256':report_spec['sha256'],'source_digest_unchanged':True,'normal_window_quit_confirmed':True,
        'original_notes_and_transcript_unchanged':True,'new_jobs_terminal':True,'no_new_asr':True,
        'pending_intents':[],'projection_journals':[],'new_jobs':1,'new_settlements':1,'job_status_counts':{'succeeded':1},
        'charged_points':'0.086','charged_seconds':0,'active_oss_objects':0,'complete_main_chains_passed':0,
        'complete_cases_passed':0,'complete_journeys_passed':0,'full_release_gate_passed':False,
        'billing_ops_evidence':[ledgerbefore,ledgerafter],'empty_locks_preserved':locks})
    fifth={'report':report_spec,'artifact':{'path':str(artifact44),'sha256':artifactsha},'product_manifest':manifest44,
        'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'b'*64,'native_task_id':'actual-failed-task15','safe_closeout':closeout}
    proof['current_version']='0.1.45';run.context['version']='0.1.45';proof['additional_summary_attempts'].append(fifth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len4','extra-stage','swapped-stage','dropped-fee','old-row','unsettled','asr',
    'unknown-writing','unsafe-quit','missing-closeout','report-chain','missing-lock','nonempty-lock','changed-lock-sha',
    'unknown-lock','helper-default','helper-other-arg','checker-changed44','claim-changed44','other-module-changed44','version46'])
def test_main_repair45_exact_five_boundaries_preserved_empty_locks_and_exact44_signature(tmp_path,fault):
    run,proof=repair45_driver_fixture(tmp_path);fifth=proof['additional_summary_attempts'][4]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len4':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(fifth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='missing-closeout':fifth.pop('safe_closeout')
    if fault=='version46':proof['current_version']='0.1.46';run.context['version']='0.1.46'
    if fault in ('dropped-fee','old-row','unsettled','asr','report-chain'):
        r=json.loads(Path(fifth['report']['path']).read_text('utf-8'))
        if fault=='report-chain':r['result']['repair_continuation']['additional_summary_attempts'][3]['charged_points']=0
        else:
            v=json.loads(Path(fifth['after_ledger']['path']).read_text('utf-8'))
            if fault=='dropped-fee':v['readback']['charged_points']=11.635
            if fault=='old-row':v['readback']['settlements'][1]['charged_points']=2
            if fault=='unsettled':v['readback']['jobs'][-1]['status']='running'
            if fault=='asr':v['readback']['jobs'][-1]['operation_id']='asr:repeat'
            save(fifth['after_ledger'],v)
            for row in r['result']['evidence']:
                if row['path']=='after.json':row['sha256']=fifth['after_ledger']['sha256']
        save(fifth['report'],r)
        v=json.loads(Path(fifth['safe_closeout']['path']).read_text('utf-8'));v['report_sha256']=fifth['report']['sha256'];save(fifth['safe_closeout'],v)
    if fault in ('unknown-writing','unsafe-quit','missing-lock','nonempty-lock','changed-lock-sha','unknown-lock'):
        v=json.loads(Path(fifth['safe_closeout']['path']).read_text('utf-8'))
        if fault=='unknown-writing':v['pending_intents']=[{'phase':'writing-unknown'}]
        if fault=='unsafe-quit':v['normal_window_quit_confirmed']=False
        if fault=='missing-lock':v['empty_locks_preserved'].pop()
        if fault=='nonempty-lock':(Path(proof['app_dir'])/v['empty_locks_preserved'][0]['relative_path']).write_bytes(b'unknown')
        if fault=='changed-lock-sha':v['empty_locks_preserved'][0]['sha256']='0'*64
        if fault=='unknown-lock':v['empty_locks_preserved'][0]['relative_path']='data/unreviewed.lock.sqlite'
        save(fifth['safe_closeout'],v)
    if fault in ('helper-default','helper-other-arg','checker-changed44','claim-changed44','other-module-changed44'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/media.py'
        if fault=='claim-changed44':name='pku_sync/claim_audit.py'
        if fault=='other-module-changed44':name='pku_sync/panel/static/app.js'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='helper-default':text=text.replace('source_time_context: dict | None = None','source_time_context: dict | None = {}')
        if fault=='helper-other-arg':text=text.replace('issues: list[str]','issues: str')
        if fault=='checker-changed44':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='claim-changed44':text=text.replace('return note.strip()','return note.upper()')
        if fault=='other-module-changed44':text+=' changed'
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==11.721
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair46_driver_fixture(tmp_path):
    import shutil
    run,proof=repair45_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    fifth=proof['additional_summary_attempts'][4]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root45=tmp_path/'private/installed45/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root45/'pku_sync')
    binary45=root45.parents[3]/'pku-desktop.exe';binary45.write_bytes(b'actual fixture45 binary')
    artifact45=tmp_path/'candidate45.exe';artifact45.write_bytes(b'actual fixture45 artifact');artifactsha=hashlib.sha256(artifact45.read_bytes()).hexdigest()
    manifest45=dump(tmp_path/'manifest45.json',{'product_root':str(root45),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root45/name).read_bytes()).hexdigest() for name in current['files']}})
    media=Path(current['product_root'])/'pku_sync/media.py';media.write_text(media.read_text('utf-8').replace('return draft.rstrip()','return draft.lstrip()'),'utf-8')
    current['files']['pku_sync/media.py']=hashlib.sha256(media.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(fifth['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1006;after['charged_points']=11.833
    op='5'*32
    after['jobs'].append({'id':'r16job','operation_id':op,'operation':'notes','purpose':'lecture-notes',
        'status':'succeeded','source_id':'source','course_id':'course','points_charged':0.112})
    after['settlements'].append({'id':'r16charge','operation_id':op,'operation':'notes','charged_points':0.112})
    case=tmp_path/'r16'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.45','artifact_sha256':artifactsha,
        'installed_path':str(binary45),'installed_sha256':hashlib.sha256(binary45.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(fifth['report']['path']).read_text('utf-8'))
    report.update(version='0.1.45',artifact_sha256=artifactsha,source_digest='c'*64,finished_source_digest='c'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({
        'report_sha256':fifth['report']['sha256'],'source_digest':fifth['source_digest'],'native_task_id':fifth['native_task_id'],
        'version':'0.1.44','charged_points':0.086,'charged_seconds':0,'jobs':1})
    c.update(current_version='0.1.45',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task16','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.112,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    close=json.loads(Path(fifth['safe_closeout']['path']).read_text('utf-8'))
    close.update(kind='actual-installed45-summary-failure-safe-closeout-not-pass',report_sha256=report_spec['sha256'],
        charged_points='0.112',billing_ops_evidence=[ledgerbefore,ledgerafter]);close.pop('complete_journeys_passed')
    closeout=dump(case/'safe-closeout.json',close)
    sixth={'report':report_spec,'artifact':{'path':str(artifact45),'sha256':artifactsha},'product_manifest':manifest45,
        'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'c'*64,'native_task_id':'actual-failed-task16','safe_closeout':closeout}
    proof['current_version']='0.1.46';run.context['version']='0.1.46';proof['additional_summary_attempts'].append(sixth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len5','extra-stage','swapped-stage','dropped-fee','old-row','unsettled','asr',
    'unknown-writing','unsafe-quit','missing-closeout','report-chain','missing-lock','nonempty-lock','changed-lock-sha',
    'unknown-lock','invented-journeys','helper-default','helper-other-arg','checker-changed45','claim-changed45','other-module-changed45','version47'])
def test_main_repair46_exact_six_boundaries_preserved_actual_r16_schema_and_exact45_signature(tmp_path,fault):
    run,proof=repair46_driver_fixture(tmp_path);sixth=proof['additional_summary_attempts'][5]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len5':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(sixth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='missing-closeout':sixth.pop('safe_closeout')
    if fault=='version47':proof['current_version']='0.1.47';run.context['version']='0.1.47'
    if fault in ('dropped-fee','old-row','unsettled','asr','report-chain'):
        r=json.loads(Path(sixth['report']['path']).read_text('utf-8'))
        if fault=='report-chain':r['result']['repair_continuation']['additional_summary_attempts'][4]['charged_points']=0
        else:
            v=json.loads(Path(sixth['after_ledger']['path']).read_text('utf-8'))
            if fault=='dropped-fee':v['readback']['charged_points']=11.721
            if fault=='old-row':v['readback']['settlements'][1]['charged_points']=2
            if fault=='unsettled':v['readback']['jobs'][-1]['status']='running'
            if fault=='asr':v['readback']['jobs'][-1]['operation_id']='asr:repeat'
            save(sixth['after_ledger'],v)
            for row in r['result']['evidence']:
                if row['path']=='after.json':row['sha256']=sixth['after_ledger']['sha256']
        save(sixth['report'],r)
        v=json.loads(Path(sixth['safe_closeout']['path']).read_text('utf-8'));v['report_sha256']=sixth['report']['sha256'];save(sixth['safe_closeout'],v)
    if fault in ('unknown-writing','unsafe-quit','missing-lock','nonempty-lock','changed-lock-sha','unknown-lock','invented-journeys'):
        v=json.loads(Path(sixth['safe_closeout']['path']).read_text('utf-8'))
        if fault=='unknown-writing':v['pending_intents']=[{'phase':'writing-unknown'}]
        if fault=='unsafe-quit':v['normal_window_quit_confirmed']=False
        if fault=='missing-lock':v['empty_locks_preserved'].pop()
        if fault=='nonempty-lock':(Path(proof['app_dir'])/v['empty_locks_preserved'][0]['relative_path']).write_bytes(b'unknown')
        if fault=='changed-lock-sha':v['empty_locks_preserved'][0]['sha256']='0'*64
        if fault=='unknown-lock':v['empty_locks_preserved'][0]['relative_path']='data/unreviewed.lock.sqlite'
        if fault=='invented-journeys':v['complete_journeys_passed']=0
        save(sixth['safe_closeout'],v)
    if fault in ('helper-default','helper-other-arg','checker-changed45','claim-changed45','other-module-changed45'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/media.py'
        if fault=='claim-changed45':name='pku_sync/claim_audit.py'
        if fault=='other-module-changed45':name='pku_sync/panel/static/app.js'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='helper-default':text=text.replace('source_time_context: dict | None = None','source_time_context: dict | None = {}')
        if fault=='helper-other-arg':text=text.replace('issues: list[str]','issues: str')
        if fault=='checker-changed45':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='claim-changed45':text=text.replace('return note.strip()','return note.upper()')
        if fault=='other-module-changed45':text+=' changed'
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==11.833
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1,1]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086,0.112]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair47_driver_fixture(tmp_path):
    import shutil
    run,proof=repair46_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    platform='POLICY = 1\ndef _llm_payload_hash(operation: str, system: str, user: str) -> str:\n    return user\ndef _llm_job_key(operation: str, system: str, user: str, *, attempt: int = 0) -> str:\n    return user\ndef _llm_durable_job(operation: str, prompt: str, settings, *, system: str, token: str, operation_context=None) -> dict:\n    return {}\ndef transcribe(data):\n    return data\n'
    for spec in [proof['prior_product_manifest'],proof['current_product_manifest']]+[s['product_manifest'] for s in proof['additional_summary_attempts']]:
        p=Path(spec['path']);m=json.loads(p.read_text('utf-8'));module=Path(m['product_root'])/'pku_sync/platform.py'
        module.write_text(platform,'utf-8');m['files']['pku_sync/platform.py']=hashlib.sha256(module.read_bytes()).hexdigest();value=dump(p,m);spec.update(value)
    (Path(run.context['root'])/'pku_sync/platform.py').write_text(platform,'utf-8')
    sixth=proof['additional_summary_attempts'][5]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root46=tmp_path/'private/installed46/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root46/'pku_sync')
    binary46=root46.parents[3]/'pku-desktop.exe';binary46.write_bytes(b'actual fixture46 binary')
    artifact46=tmp_path/'candidate46.exe';artifact46.write_bytes(b'actual fixture46 artifact');artifactsha=hashlib.sha256(artifact46.read_bytes()).hexdigest()
    manifest46=dump(tmp_path/'manifest46.json',{'product_root':str(root46),'artifact_sha256':artifactsha,
        'files':{name:hashlib.sha256((root46/name).read_bytes()).hexdigest() for name in current['files']}})
    source_root=Path(current['product_root'])
    edits={'pku_sync/media.py':(source_root/'pku_sync/media.py').read_text('utf-8').replace('return draft.lstrip()','return draft.upper()'),
           'pku_sync/claim_audit.py':(source_root/'pku_sync/claim_audit.py').read_text('utf-8').replace('return note.strip()','return note.rstrip()'),
           'pku_sync/platform.py':platform.replace('user: str) -> str','user: str, *, model: str | None = None) -> str').replace('attempt: int = 0) -> str','attempt: int = 0, model: str | None = None) -> str').replace('return user','return user.strip()').replace('return {}',"return {'model': 'qwen3.5-flash'}")}
    for name,text in edits.items():
        module=source_root/name;module.write_text(text,'utf-8');current['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest()
    proof['current_product_manifest']=dump(path,current);proof['allowed_changed_files'].append('pku_sync/platform.py')
    before=json.loads(Path(sixth['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1007;after['charged_points']=12.22
    for n in range(16):
        op=f'{n+700:032x}';points=0.087 if n==15 else 0.02
        after['jobs'].append({'id':f'r17job{n}','operation_id':op,'operation':'notes','purpose':'lecture-notes',
            'status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r17charge{n}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r17'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.46','artifact_sha256':artifactsha,
        'installed_path':str(binary46),'installed_sha256':hashlib.sha256(binary46.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(sixth['report']['path']).read_text('utf-8'))
    report.update(version='0.1.46',artifact_sha256=artifactsha,source_digest='d'*64,finished_source_digest='d'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({
        'report_sha256':sixth['report']['sha256'],'source_digest':sixth['source_digest'],'native_task_id':sixth['native_task_id'],
        'version':'0.1.45','charged_points':0.112,'charged_seconds':0,'jobs':1})
    c.update(current_version='0.1.46',current_artifact_sha256=artifactsha,
        current_native_task={'id':'actual-failed-task17','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.387,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    close=json.loads(Path(sixth['safe_closeout']['path']).read_text('utf-8'))
    close.update(kind='actual-installed46-summary-failure-safe-closeout-not-pass',report_sha256=report_spec['sha256'],
        charged_points='0.387',new_jobs=16,new_settlements=16,job_status_counts={'succeeded':16},
        complete_journeys_passed=0,billing_ops_evidence=[ledgerbefore,ledgerafter])
    closeout=dump(case/'safe-closeout.json',close)
    seventh={'report':report_spec,'artifact':{'path':str(artifact46),'sha256':artifactsha},'product_manifest':manifest46,
        'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'d'*64,'native_task_id':'actual-failed-task17','safe_closeout':closeout}
    proof['current_version']='0.1.47';run.context['version']='0.1.47';proof['additional_summary_attempts'].append(seventh)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len6','extra-stage','swapped-stage','dropped-fee','old-row','unsettled','asr',
    'unknown-writing','unsafe-quit','missing-closeout','report-chain','missing-lock','nonempty-lock','dropped-journeys',
    'helper-default','checker-changed46','claim-schema','other-module-changed46','platform-default','platform-positional',
    'platform-other-arg','durable-signature','platform-asr-helper','platform-constant','version48'])
def test_main_repair47_exact_seven_boundaries_and_only_three_reviewed_modules_function_scope(tmp_path,fault):
    run,proof=repair47_driver_fixture(tmp_path);seventh=proof['additional_summary_attempts'][6]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len6':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(seventh))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='missing-closeout':seventh.pop('safe_closeout')
    if fault=='version48':proof['current_version']='0.1.48';run.context['version']='0.1.48'
    if fault in ('dropped-fee','old-row','unsettled','asr','report-chain'):
        report=json.loads(Path(seventh['report']['path']).read_text('utf-8'))
        if fault=='report-chain':report['result']['repair_continuation']['additional_summary_attempts'][5]['charged_points']=0
        else:
            value=json.loads(Path(seventh['after_ledger']['path']).read_text('utf-8'))
            if fault=='dropped-fee':value['readback']['charged_points']=11.833
            if fault=='old-row':value['readback']['settlements'][1]['charged_points']=2
            if fault=='unsettled':value['readback']['jobs'][-1]['status']='running'
            if fault=='asr':value['readback']['jobs'][-1]['operation_id']='asr:repeat'
            save(seventh['after_ledger'],value)
            for row in report['result']['evidence']:
                if row['path']=='after.json':row['sha256']=seventh['after_ledger']['sha256']
        save(seventh['report'],report)
        value=json.loads(Path(seventh['safe_closeout']['path']).read_text('utf-8'));value['report_sha256']=seventh['report']['sha256'];save(seventh['safe_closeout'],value)
    if fault in ('unknown-writing','unsafe-quit','missing-lock','nonempty-lock','dropped-journeys'):
        value=json.loads(Path(seventh['safe_closeout']['path']).read_text('utf-8'))
        if fault=='unknown-writing':value['pending_intents']=[{'phase':'writing-unknown'}]
        if fault=='unsafe-quit':value['normal_window_quit_confirmed']=False
        if fault=='missing-lock':value['empty_locks_preserved'].pop()
        if fault=='nonempty-lock':(Path(proof['app_dir'])/value['empty_locks_preserved'][0]['relative_path']).write_bytes(b'unknown')
        if fault=='dropped-journeys':value.pop('complete_journeys_passed')
        save(seventh['safe_closeout'],value)
    if fault in ('helper-default','checker-changed46','claim-schema','other-module-changed46','platform-default','platform-positional',
                 'platform-other-arg','durable-signature','platform-asr-helper','platform-constant'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/platform.py'
        if fault in ('helper-default','checker-changed46'):name='pku_sync/media.py'
        if fault=='claim-schema':name='pku_sync/claim_audit.py'
        if fault=='other-module-changed46':name='pku_sync/panel/static/app.js'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='helper-default':text=text.replace('source_time_context: dict | None = None','source_time_context: dict | None = {}')
        if fault=='checker-changed46':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='claim-schema':text+='\nSCHEMA = 999\n'
        if fault=='other-module-changed46':text+=' changed'
        if fault=='platform-default':text=text.replace('model: str | None = None','model: str | None = "flash"')
        if fault=='platform-positional':text=text.replace('user: str, *, model: str | None = None','user: str, model: str | None = None')
        if fault=='platform-other-arg':text=text.replace('attempt: int = 0','attempt: str = 0')
        if fault=='durable-signature':text=text.replace('operation_context=None','operation_context=None, model=None')
        if fault=='platform-asr-helper':text=text.replace('return data','return None')
        if fault=='platform-constant':text=text.replace('POLICY = 1','POLICY = 2')
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==12.22
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1,1,16]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086,0.112,0.387]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def repair48_driver_fixture(tmp_path):
    import shutil
    run,proof=repair47_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    extras='\ndef _verified_ledger_sentences(llm, model: str, claims: list[dict], *, settings, record: dict, cache_path: Path, fallbacks: list[dict] | None = None) -> list[dict]:\n    return claims\ndef _recover_chapter_with_ledger(llm, model: str, title: str, transcript: str, draft: str, safe_summary: str, *, settings, sources: list[dict], cache_path: Path, evidence_blocks: list[dict] | None = None, natural_rewrite: bool = False, review_packet_path: Path | None = None, other_transcripts: list[str] | None = None, rewrite_fallbacks: list[dict] | None = None, recovery_gaps: list[dict] | None = None) -> str:\n    return draft\ndef _chapters(segments: list[dict], *, target_seconds: int = 480, max_chars: int = 11000) -> list[dict]:\n    target_seconds = max(120, target_seconds)\n    min_seconds = max(90, round(target_seconds * 0.35))\n    max_seconds = max(min_seconds + 60, round(target_seconds * 1.5))\n    chapters = []\n    return chapters\n'
    for name in ('_ledger_claim_citation','_ledger_claim_text','_ledger_claim_line','_ledger_group_citation','_ledger_render_guard','_compose_verified_recovery_claims'):
        extras+=f'\ndef {name}(claim):\n    return claim\n'
    specs=[proof['prior_product_manifest'],proof['current_product_manifest']]+[s['product_manifest'] for s in proof['additional_summary_attempts']]
    for spec in specs:
        manifest=json.loads(Path(spec['path']).read_text('utf-8'));module=Path(manifest['product_root'])/'pku_sync/media.py'
        module.write_text(module.read_text('utf-8')+extras,'utf-8');manifest['files']['pku_sync/media.py']=hashlib.sha256(module.read_bytes()).hexdigest();spec.update(dump(Path(spec['path']),manifest))
    seventh=proof['additional_summary_attempts'][6]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root47=tmp_path/'private/installed47/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root47/'pku_sync')
    binary47=root47.parents[3]/'pku-desktop.exe';binary47.write_bytes(b'actual fixture47 binary')
    artifact47=tmp_path/'candidate47.exe';artifact47.write_bytes(b'actual fixture47 artifact');artifactsha=hashlib.sha256(artifact47.read_bytes()).hexdigest()
    manifest47=dump(tmp_path/'manifest47.json',{'product_root':str(root47),'artifact_sha256':artifactsha,'files':{name:hashlib.sha256((root47/name).read_bytes()).hexdigest() for name in current['files']}})
    module=Path(current['product_root'])/'pku_sync/media.py';text=module.read_text('utf-8')
    text=text.replace('fallbacks: list[dict] | None = None) -> list[dict]','fallbacks: list[dict] | None = None, evidence_blocks: list[dict] | None = None, sources: list[dict] | None = None) -> list[dict]')
    text=text.replace('recovery_gaps: list[dict] | None = None) -> str','recovery_gaps: list[dict] | None = None, full_sources: list[dict] | None = None) -> str')
    text=text.replace('return claims','return list(claims)').replace('return claim\n','return dict(claim)\n')
    text=text.replace('return chapters','grouped = list(chapters)\n    for row in segments:\n        grouped.append(row)\n    return grouped')
    module.write_text(text,'utf-8');current['files']['pku_sync/media.py']=hashlib.sha256(module.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(seventh['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1008;after['charged_points']=13.48
    for n in range(49):
        op=f'{n+800:032x}';points=0.3 if n==48 else 0.02
        after['jobs'].append({'id':f'r19job{n}','operation_id':op,'operation':'notes','purpose':'lecture-notes','status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r19charge{n}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r19'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.47','artifact_sha256':artifactsha,'installed_path':str(binary47),'installed_sha256':hashlib.sha256(binary47.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(seventh['report']['path']).read_text('utf-8'))
    report.update(version='0.1.47',artifact_sha256=artifactsha,source_digest='e'*64,finished_source_digest='e'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({'report_sha256':seventh['report']['sha256'],'source_digest':seventh['source_digest'],'native_task_id':seventh['native_task_id'],'version':'0.1.46','charged_points':0.387,'charged_seconds':0,'jobs':16})
    c.update(current_version='0.1.47',current_artifact_sha256=artifactsha,current_native_task={'id':'actual-failed-task19','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':1.26,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    close=json.loads(Path(seventh['safe_closeout']['path']).read_text('utf-8'))
    close.update(kind='actual-installed47-summary-failure-safe-closeout-not-pass',report_sha256=report_spec['sha256'],charged_points='1.260',new_jobs=49,new_settlements=49,job_status_counts={'succeeded':49},billing_ops_evidence=[ledgerbefore,ledgerafter])
    eighth={'report':report_spec,'artifact':{'path':str(artifact47),'sha256':artifactsha},'product_manifest':manifest47,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'e'*64,'native_task_id':'actual-failed-task19','safe_closeout':dump(case/'safe-closeout.json',close)}
    proof['current_version']='0.1.48';run.context['version']='0.1.48';proof['additional_summary_attempts'].append(eighth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len7','extra-stage','swapped-stage','fee-format','old-row','new-asr','report-chain','unknown-writing','missing-lock','other-module','other-function','signature-default','signature-positional','chapter-budget','version49'])
def test_main_repair48_exact_eight_stages_full_sources_and_only_reviewed_media_scope(tmp_path,fault):
    run,proof=repair48_driver_fixture(tmp_path);eighth=proof['additional_summary_attempts'][7]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len7':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(eighth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='version49':proof['current_version']='0.1.49';run.context['version']='0.1.49'
    if fault in ('fee-format','unknown-writing','missing-lock'):
        close=json.loads(Path(eighth['safe_closeout']['path']).read_text('utf-8'))
        if fault=='fee-format':close['charged_points']='1.26'
        if fault=='unknown-writing':close['pending_intents']=[{'state':'writing-unknown'}]
        if fault=='missing-lock':close['empty_locks_preserved'].pop()
        save(eighth['safe_closeout'],close)
    if fault in ('old-row','new-asr','report-chain'):
        report=json.loads(Path(eighth['report']['path']).read_text('utf-8'))
        if fault=='report-chain':report['result']['repair_continuation']['additional_summary_attempts'][6]['charged_points']=0
        else:
            value=json.loads(Path(eighth['after_ledger']['path']).read_text('utf-8'))
            if fault=='old-row':value['readback']['settlements'][1]['charged_points']=2
            if fault=='new-asr':value['readback']['jobs'][-1]['operation_id']='asr:duplicate'
            save(eighth['after_ledger'],value)
            for entry in report['result']['evidence']:
                if entry['path']=='after.json':entry['sha256']=eighth['after_ledger']['sha256']
        save(eighth['report'],report)
        value=json.loads(Path(eighth['safe_closeout']['path']).read_text('utf-8'));value['report_sha256']=eighth['report']['sha256'];save(eighth['safe_closeout'],value)
    if fault in ('other-module','other-function','signature-default','signature-positional','chapter-budget'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/platform.py' if fault=='other-module' else 'pku_sync/media.py'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='other-module':text+='\nUNREVIEWED = True\n'
        if fault=='other-function':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='signature-default':text=text.replace('full_sources: list[dict] | None = None','full_sources: list[dict] | None = []')
        if fault=='signature-positional':text=text.replace('claims: list[dict], *, settings','claims: list[dict], settings')
        if fault=='chapter-budget':text=text.replace('target_seconds * 1.5','target_seconds * 3')
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==13.48
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1,1,16,49]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086,0.112,0.387,1.26]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def test_main_repair48_report_routes_all_eight_verified_stages_without_singular_alias():
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(native_driver._fresh_main_chain))
    branch=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
                and ast.unparse(node.test)=="resume['additional_summary_attempt'] is not None")
    stages=[{'version':f'0.1.{version}','jobs':jobs} for version,jobs in zip(range(40,48),(22,16,43,10,1,1,16,49))]
    run=SimpleNamespace(_repair_continuation={})
    resume={'proof':{'current_version':'0.1.48'},'additional_summary_attempt':{'stages':stages,'bound':['private-physical-proof'],'processed':{'complete':True}}}
    exec(compile(ast.Module(body=[branch],type_ignores=[]),'<actual-main-report-route>','exec'),{'run':run,'resume':resume})
    assert run._repair_continuation=={'additional_summary_attempts':stages}
    assert 'additional_summary_attempt' not in run._repair_continuation
    assert 'bound' not in run._repair_continuation and 'processed' not in run._repair_continuation


def repair49_driver_fixture(tmp_path):
    import shutil
    run,proof=repair48_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    eighth=proof['additional_summary_attempts'][7]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root48=tmp_path/'private/installed48/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root48/'pku_sync')
    binary48=root48.parents[3]/'pku-desktop.exe';binary48.write_bytes(b'actual fixture48 binary')
    artifact48=tmp_path/'candidate48.exe';artifact48.write_bytes(b'actual fixture48 artifact');artifactsha=hashlib.sha256(artifact48.read_bytes()).hexdigest()
    manifest48=dump(tmp_path/'manifest48.json',{'product_root':str(root48),'artifact_sha256':artifactsha,'files':{name:hashlib.sha256((root48/name).read_bytes()).hexdigest() for name in current['files']}})
    module=Path(current['product_root'])/'pku_sync/media.py';text=module.read_text('utf-8').replace('return list(claims)','return tuple(claims)')
    module.write_text(text,'utf-8');current['files']['pku_sync/media.py']=hashlib.sha256(module.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(eighth['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1009;after['charged_points']=14.21
    for n in range(30):
        op=f'{n+900:032x}';points=0.15 if n==29 else 0.02
        after['jobs'].append({'id':f'r20job{n}','operation_id':op,'operation':'notes','purpose':'lecture-notes','status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r20charge{n}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r20'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.48','artifact_sha256':artifactsha,'installed_path':str(binary48),'installed_sha256':hashlib.sha256(binary48.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(eighth['report']['path']).read_text('utf-8'))
    report.update(version='0.1.48',artifact_sha256=artifactsha,source_digest='f'*64,finished_source_digest='f'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({'report_sha256':eighth['report']['sha256'],'source_digest':eighth['source_digest'],'native_task_id':eighth['native_task_id'],'version':'0.1.47','charged_points':1.26,'charged_seconds':0,'jobs':49})
    c.update(current_version='0.1.48',current_artifact_sha256=artifactsha,current_native_task={'id':'actual-failed-task20','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.73,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    close=json.loads(Path(eighth['safe_closeout']['path']).read_text('utf-8'))
    close.update(kind='actual-installed48-summary-failure-safe-closeout-not-pass',report_sha256=report_spec['sha256'],charged_points='0.730',new_jobs=30,new_settlements=30,job_status_counts={'succeeded':30},billing_ops_evidence=[ledgerbefore,ledgerafter])
    ninth={'report':report_spec,'artifact':{'path':str(artifact48),'sha256':artifactsha},'product_manifest':manifest48,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'f'*64,'native_task_id':'actual-failed-task20','safe_closeout':dump(case/'safe-closeout.json',close)}
    proof['current_version']='0.1.49';run.context['version']='0.1.49';proof['additional_summary_attempts'].append(ninth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len8','extra-stage','swapped-stage','fee-format','old-row','new-asr','report-chain','unknown-writing','missing-lock','unsettled','wrong-page-source','other-module','other-function','signature-default','version50'])
def test_main_repair49_exact_nine_stages_and_single_sentence_verifier_body(tmp_path,fault):
    run,proof=repair49_driver_fixture(tmp_path);ninth=proof['additional_summary_attempts'][8]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len8':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(ninth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='version50':proof['current_version']='0.1.50';run.context['version']='0.1.50'
    if fault in ('fee-format','unknown-writing','missing-lock'):
        value=json.loads(Path(ninth['safe_closeout']['path']).read_text('utf-8'))
        if fault=='fee-format':value['charged_points']='0.73'
        if fault=='unknown-writing':value['pending_intents']=[{'state':'writing-unknown'}]
        if fault=='missing-lock':value['empty_locks_preserved'].pop()
        save(ninth['safe_closeout'],value)
    if fault in ('old-row','new-asr','report-chain','unsettled','wrong-page-source'):
        report=json.loads(Path(ninth['report']['path']).read_text('utf-8'))
        if fault=='report-chain':report['result']['repair_continuation']['additional_summary_attempts'][7]['charged_points']=0
        else:
            value=json.loads(Path(ninth['after_ledger']['path']).read_text('utf-8'))
            if fault=='old-row':value['readback']['settlements'][1]['charged_points']=2
            if fault=='new-asr':value['readback']['jobs'][-1]['operation_id']='asr:duplicate'
            if fault=='unsettled':value['readback']['jobs'][-1]['status']='running'
            if fault=='wrong-page-source':value['readback']['jobs'][-1]['source_id']='other-course-page'
            save(ninth['after_ledger'],value)
            for entry in report['result']['evidence']:
                if entry['path']=='after.json':entry['sha256']=ninth['after_ledger']['sha256']
        save(ninth['report'],report)
        value=json.loads(Path(ninth['safe_closeout']['path']).read_text('utf-8'));value['report_sha256']=ninth['report']['sha256'];save(ninth['safe_closeout'],value)
    if fault in ('other-module','other-function','signature-default'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/platform.py' if fault=='other-module' else 'pku_sync/media.py'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='other-module':text+='\nUNREVIEWED = True\n'
        if fault=='other-function':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='signature-default':text=text.replace('full_sources: list[dict] | None = None','full_sources: list[dict] | None = []')
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==14.21
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1,1,16,49,30]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086,0.112,0.387,1.26,0.73]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def test_main_repair49_report_routes_nine_verified_stages_without_singular_alias():
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(native_driver._fresh_main_chain))
    branch=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
                and ast.unparse(node.test)=="resume['additional_summary_attempt'] is not None")
    stages=[{'version':f'0.1.{version}','jobs':jobs} for version,jobs in zip(range(40,49),(22,16,43,10,1,1,16,49,30))]
    run=SimpleNamespace(_repair_continuation={})
    resume={'proof':{'current_version':'0.1.49'},'additional_summary_attempt':{'stages':stages,'bound':['private-physical-proof'],'processed':{'complete':True}}}
    exec(compile(ast.Module(body=[branch],type_ignores=[]),'<actual-main49-report-route>','exec'),{'run':run,'resume':resume})
    assert run._repair_continuation=={'additional_summary_attempts':stages}
    assert 'additional_summary_attempt' not in run._repair_continuation
    assert 'bound' not in run._repair_continuation and 'processed' not in run._repair_continuation


def repair50_driver_fixture(tmp_path):
    import shutil
    run,proof=repair49_driver_fixture(tmp_path)
    def dump(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),'utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    ninth=proof['additional_summary_attempts'][8]
    path=Path(proof['current_product_manifest']['path']);current=json.loads(path.read_text('utf-8'))
    root49=tmp_path/'private/installed49/resources/runtime/Lib/site-packages';shutil.copytree(Path(current['product_root'])/'pku_sync',root49/'pku_sync')
    binary49=root49.parents[3]/'pku-desktop.exe';binary49.write_bytes(b'actual fixture49 binary')
    artifact49=tmp_path/'candidate49.exe';artifact49.write_bytes(b'actual fixture49 artifact');artifactsha=hashlib.sha256(artifact49.read_bytes()).hexdigest()
    manifest49=dump(tmp_path/'manifest49.json',{'product_root':str(root49),'artifact_sha256':artifactsha,'files':{name:hashlib.sha256((root49/name).read_bytes()).hexdigest() for name in current['files']}})
    module=Path(current['product_root'])/'pku_sync/media.py';text=module.read_text('utf-8').replace('return tuple(claims)','return tuple(reversed(claims))')
    module.write_text(text,'utf-8');current['files']['pku_sync/media.py']=hashlib.sha256(module.read_bytes()).hexdigest();proof['current_product_manifest']=dump(path,current)
    before=json.loads(Path(ninth['after_ledger']['path']).read_text('utf-8'))['readback'];after=deepcopy(before);after['observed_at']=1010;after['charged_points']=14.286
    for n in range(4):
        op=f'{n+1000:032x}';points=0.016 if n==3 else 0.02
        after['jobs'].append({'id':f'r21job{n}','operation_id':op,'operation':'notes','purpose':'lecture-notes','status':'succeeded','source_id':'source','course_id':'course','points_charged':points})
        after['settlements'].append({'id':f'r21charge{n}','operation_id':op,'operation':'notes','charged_points':points})
    case=tmp_path/'r21'
    ledgerbefore=dump(case/'before.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':before})
    ledgerafter=dump(case/'after.json',{'service':'account','method':'GET','path':'/v1/operations','status':200,'readback':after})
    runtime=dump(case/'runtime.json',{'processes':[{'target':'windows','version':'0.1.49','artifact_sha256':artifactsha,'installed_path':str(binary49),'installed_sha256':hashlib.sha256(binary49.read_bytes()).hexdigest(),'alive':False}]})
    report=json.loads(Path(ninth['report']['path']).read_text('utf-8'))
    report.update(version='0.1.49',artifact_sha256=artifactsha,source_digest='a'*64,finished_source_digest='a'*64)
    report['result']['evidence']=[{'path':Path(s['path']).name,'sha256':s['sha256'],'role':'runtime' if s is runtime else 'oracle'} for s in [ledgerbefore,ledgerafter,runtime]]
    c=report['result']['repair_continuation'];c['additional_summary_attempts'].append({'report_sha256':ninth['report']['sha256'],'source_digest':ninth['source_digest'],'native_task_id':ninth['native_task_id'],'version':'0.1.48','charged_points':0.73,'charged_seconds':0,'jobs':30})
    c.update(current_version='0.1.49',current_artifact_sha256=artifactsha,current_native_task={'id':'actual-failed-task21','kind':'process','state':'failed','recording_id':'recording'},repair_summary_cost={'charged_points':0.076,'charged_seconds':0})
    report_spec=dump(case/'report.json',report)
    close=json.loads(Path(ninth['safe_closeout']['path']).read_text('utf-8'))
    close.update(kind='actual-installed49-summary-failure-safe-closeout-not-pass',report_sha256=report_spec['sha256'],charged_points='0.076',new_jobs=4,new_settlements=4,job_status_counts={'succeeded':4},billing_ops_evidence=[ledgerbefore,ledgerafter])
    tenth={'report':report_spec,'artifact':{'path':str(artifact49),'sha256':artifactsha},'product_manifest':manifest49,'before_ledger':ledgerbefore,'after_ledger':ledgerafter,'source_digest':'a'*64,'native_task_id':'actual-failed-task21','safe_closeout':dump(case/'safe-closeout.json',close)}
    proof['current_version']='0.1.50';run.context['version']='0.1.50';proof['additional_summary_attempts'].append(tenth)
    return run,proof


@pytest.mark.parametrize('fault',['none','old-len9','extra-stage','swapped-stage','fee-format','old-row','new-asr','report-chain','unknown-writing','missing-lock','unsettled','wrong-page-source','other-module','other-function','signature-default','version51'])
def test_main_repair50_exact_ten_stages_and_single_sentence_verifier_body(tmp_path,fault):
    run,proof=repair50_driver_fixture(tmp_path);tenth=proof['additional_summary_attempts'][9]
    def save(spec,value):
        p=Path(spec['path']);p.write_text(json.dumps(value),'utf-8');spec['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='old-len9':proof['additional_summary_attempts'].pop()
    if fault=='extra-stage':proof['additional_summary_attempts'].append(deepcopy(tenth))
    if fault=='swapped-stage':proof['additional_summary_attempts'].reverse()
    if fault=='version51':proof['current_version']='0.1.51';run.context['version']='0.1.51'
    if fault in ('fee-format','unknown-writing','missing-lock'):
        value=json.loads(Path(tenth['safe_closeout']['path']).read_text('utf-8'))
        if fault=='fee-format':value['charged_points']='0.0760'
        if fault=='unknown-writing':value['pending_intents']=[{'state':'writing-unknown'}]
        if fault=='missing-lock':value['empty_locks_preserved'].pop()
        save(tenth['safe_closeout'],value)
    if fault in ('old-row','new-asr','report-chain','unsettled','wrong-page-source'):
        report=json.loads(Path(tenth['report']['path']).read_text('utf-8'))
        if fault=='report-chain':report['result']['repair_continuation']['additional_summary_attempts'][7]['charged_points']=0
        else:
            value=json.loads(Path(tenth['after_ledger']['path']).read_text('utf-8'))
            if fault=='old-row':value['readback']['settlements'][1]['charged_points']=2
            if fault=='new-asr':value['readback']['jobs'][-1]['operation_id']='asr:duplicate'
            if fault=='unsettled':value['readback']['jobs'][-1]['status']='running'
            if fault=='wrong-page-source':value['readback']['jobs'][-1]['source_id']='other-course-page'
            save(tenth['after_ledger'],value)
            for entry in report['result']['evidence']:
                if entry['path']=='after.json':entry['sha256']=tenth['after_ledger']['sha256']
        save(tenth['report'],report)
        value=json.loads(Path(tenth['safe_closeout']['path']).read_text('utf-8'));value['report_sha256']=tenth['report']['sha256'];save(tenth['safe_closeout'],value)
    if fault in ('other-module','other-function','signature-default'):
        spec=proof['current_product_manifest'];manifest=json.loads(Path(spec['path']).read_text('utf-8'));name='pku_sync/platform.py' if fault=='other-module' else 'pku_sync/media.py'
        module=Path(manifest['product_root'])/name;text=module.read_text('utf-8')
        if fault=='other-module':text+='\nUNREVIEWED = True\n'
        if fault=='other-function':text=text.replace('return chapter.strip()','return chapter.upper()')
        if fault=='signature-default':text=text.replace('full_sources: list[dict] | None = None','full_sources: list[dict] | None = []')
        module.write_text(text,'utf-8');manifest['files'][name]=hashlib.sha256(module.read_bytes()).hexdigest();save(spec,manifest)
    p=Path(run.config['main_chain_resume']['proof_path']);p.write_text(json.dumps(proof),'utf-8');run.config['main_chain_resume']['proof_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault=='none':
        result=native_driver._load_main_chain_resume(run)
        assert result['fresh_baseline']['charged_points']==14.286
        assert result['processed']['charged_points']==10.0
        assert [row['jobs'] for row in result['additional_summary_attempt']['stages']]==[22,16,43,10,1,1,16,49,30,4]
        assert [row['charged_points'] for row in result['additional_summary_attempt']['stages']]==[0.297,0.243,0.868,0.227,0.086,0.112,0.387,1.26,0.73,0.076]
    else:
        with pytest.raises(core.ResourceBlocked):native_driver._load_main_chain_resume(run)


def test_main_repair50_report_routes_ten_verified_stages_without_singular_alias():
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(native_driver._fresh_main_chain))
    branch=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
                and ast.unparse(node.test)=="resume['additional_summary_attempt'] is not None")
    stages=[{'version':f'0.1.{version}','jobs':jobs} for version,jobs in zip(range(40,50),(22,16,43,10,1,1,16,49,30,4))]
    run=SimpleNamespace(_repair_continuation={})
    resume={'proof':{'current_version':'0.1.50'},'additional_summary_attempt':{'stages':stages,'bound':['private-physical-proof'],'processed':{'complete':True}}}
    exec(compile(ast.Module(body=[branch],type_ignores=[]),'<actual-main50-report-route>','exec'),{'run':run,'resume':resume})
    assert run._repair_continuation=={'additional_summary_attempts':stages}
    assert 'additional_summary_attempt' not in run._repair_continuation
    assert 'bound' not in run._repair_continuation and 'processed' not in run._repair_continuation
