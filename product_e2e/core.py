"""One native UI adapter and evidence context shared by all product journeys.

Never launches a fake panel, mutates via panel POST, or accepts configured
``passed`` values. Windows attaches to the launched app's WebView2; macOS
requires a W3C endpoint owned by that same launched WKWebView process.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import socket
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx


class ResourceBlocked(RuntimeError):
    def __init__(self, code):
        self.code = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(code))[:120]
        super().__init__(self.code)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def need(config, key):
    value = config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ResourceBlocked("missing_" + key)
        value = value[part]
    if value is None or value == "":
        raise ResourceBlocked("missing_" + key)
    return value


def credential(config, key):
    reference = need(config, "credentials." + key)
    if not isinstance(reference, dict) or set(reference) != {"env"}:
        raise ResourceBlocked("credential_must_be_environment_reference_" + key)
    name = reference["env"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", name):
        raise ResourceBlocked("invalid_credential_reference_" + key)
    value = os.environ.get(name)
    if not value:
        raise ResourceBlocked("credential_unavailable_" + key)
    return value


def wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("bounded_wait_expired")
        time.sleep(0.1)


def loopback_url(value):
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ResourceBlocked("native_endpoint_must_be_loopback")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ResourceBlocked("invalid_native_endpoint")
    return value.rstrip("/")


def process_inventory():
    if sys.platform == "win32":
        command = ["powershell.exe", "-NoProfile", "-Command",
                   "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,SessionId | ConvertTo-Json -Compress"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
        rows = json.loads(result.stdout or "[]")
        rows = rows if isinstance(rows, list) else [rows]
        return [{"pid": row["ProcessId"], "parent_pid": row["ParentProcessId"],
                 "name": row["Name"], "path": row.get("ExecutablePath"),
                 "session_id": row.get("SessionId")} for row in rows]
    result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True,
                            text=True, timeout=20, check=True)
    return [{"pid": int(line.split(None, 1)[0]), "path": line.split(None, 1)[1]}
            for line in result.stdout.splitlines() if len(line.split(None, 1)) == 2]


def close_owned_windows(pid, *, browser_path=None):
    """Send the normal Windows close action only to this live owned process."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    handles = []
    if browser_path is not None:
        row = next((p for p in process_inventory() if p['pid'] == pid), None)
        expected = Path(browser_path).resolve()
        if (not row or expected.name.lower() not in {'msedge.exe', 'chrome.exe'}
                or Path(row.get('path') or '').resolve() != expected
                or type(row.get('session_id')) is not int or row['session_id'] <= 0):
            raise ResourceBlocked('owned_external_browser_close_identity_unverified')
    def visit(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            caption = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, caption, len(caption))
            window_class = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, window_class, len(window_class))
            if ((browser_path is None and caption.value == 'PKU All in Notion')
                    or (browser_path is not None and window_class.value == 'Chrome_WidgetWin_1')):
                handles.append(handle)
        return True
    if not user32.EnumWindows(callback_type(visit), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not handles or (browser_path is None and len(handles) != 1):
        raise ResourceBlocked('owned_native_main_window_count_' + str(len(handles)))
    for handle in handles:
        if not user32.PostMessageW(handle, 0x0010, 0, 0):  # WM_CLOSE, as for the title-bar X.
            raise ctypes.WinError(ctypes.get_last_error())
    return len(handles)


def endpoint_owned_by(endpoint, pid):
    """Reject stale CDP/WebDriver servers before giving them any UI credentials."""
    port = urlsplit(loopback_url(endpoint)).port
    if port is None:
        raise ResourceBlocked("native_endpoint_port_required")
    if sys.platform == "win32":
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                 f"[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-NetTCPConnection -State Listen -LocalPort {int(port)} | Select-Object -ExpandProperty OwningProcess | ConvertTo-Json -Compress"],
                                capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
        owners = json.loads(result.stdout or "[]")
        owners = set(owners if isinstance(owners, list) else [owners])
        parents = {row["pid"]: row["parent_pid"] for row in process_inventory()}
    else:
        result = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
                                capture_output=True, text=True, timeout=20, check=True)
        owners = {int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p")}
        result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                                text=True, timeout=20, check=True)
        parents = {int(row[0]): int(row[1]) for line in result.stdout.splitlines()
                   if len(row := line.split()) == 2}
    for owner in owners:
        seen = set()
        while owner != pid and owner in parents and owner not in seen:
            seen.add(owner)
            owner = parents[owner]
        if owner != pid:
            raise ResourceBlocked("native_endpoint_owned_by_another_process")
    if not owners:
        raise ResourceBlocked("native_endpoint_owner_unobserved")
    return sorted(owners)


def installed_version(path):
    """Read the actual OS package; never return the requested version as evidence."""
    if sys.platform == "win32":
        escaped = str(path).replace("'", "''")
        command = f"(Get-Item -LiteralPath '{escaped}').VersionInfo.ProductVersion"
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        value = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
                               capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    elif sys.platform == "darwin":
        import plistlib
        with (Path(path).parent.parent / "Info.plist").open("rb") as stream:
            value = plistlib.load(stream).get("CFBundleShortVersionString")
    else:
        raise ResourceBlocked("installed_version_requires_target_os")
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", value):
        raise ResourceBlocked("installed_os_version_unobserved")
    return value


class NativeUI:
    """UI writes use locator actions; JSON requests are GET-only observations."""
    def __init__(self, owner, name, page=None, endpoint=None, session_id=None):
        self.owner, self.name, self.page = owner, name, page
        self.endpoint, self.session_id = endpoint, session_id
        self.origin = None
        self._dialog = None
        self._dialog_decision = None
        self._dialog_handled = False
        self._pending_requests = {}
        if page is not None:
            self.origin = page.url.split("/app", 1)[0]
            page.on("dialog", self._on_dialog)
            page.on("request", self._request_event)
            page.on("response", self._response_event)
            page.on("requestfinished", self._request_finished)
            page.on("requestfailed", self._request_failed)

    def prepare_dialog(self, accepted=True):
        self._dialog_decision = accepted
        self._dialog_handled = False

    def _on_dialog(self, dialog):
        self._dialog = dialog
        # Playwright's click waits while a JS dialog is open. Arm the explicit
        # student's decision first, then handle it during the click callback.
        if self._dialog_decision is not None:
            dialog.accept() if self._dialog_decision else dialog.dismiss()
            self._dialog_handled = True

    def _request_event(self, request):
        parts = urlsplit(request.url)
        # Do not store query strings, headers, request bodies or input values.
        row = {"instance": self.name, "method": request.method,
               "host": parts.hostname, "path": parts.path,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "started_monotonic": time.monotonic(),
               "finished_at": None, "finished_monotonic": None, "status": None,
               "request_failed": None, "request_failed_type": None}
        if parts.path == '/api/campus/notion/parents':
            selected = parse_qs(parts.query).get('selected_only')
            if selected in (['true'], ['false']):
                row['selected_only'] = selected == ['true']
        self.owner.network.append(row)
        self._pending_requests[id(request)] = row

    def _response_event(self, response):
        row = self._pending_requests.get(id(response.request))
        if row is not None and type(response.status) is int and 100 <= response.status <= 599:
            row['status'] = response.status

    def _finish_request(self, request, failed):
        row = self._pending_requests.pop(id(request), None)
        if row is not None:
            row['finished_at'] = datetime.now(timezone.utc).isoformat()
            row['finished_monotonic'] = time.monotonic()
            row['request_failed'] = failed
            # Browser failure strings may contain full URLs; keep only the
            # observed event type, never its private message or payload.
            row['request_failed_type'] = 'transport_failure' if failed else None

    def _request_finished(self, request):
        self._finish_request(request, False)

    def _request_failed(self, request):
        self._finish_request(request, True)

    def _wd(self, method, suffix, data=None):
        response = httpx.request(method, self.endpoint + "/session/" + self.session_id + suffix,
                                 json=data, timeout=30, trust_env=False)
        response.raise_for_status()
        payload = response.json()["value"]
        if isinstance(payload, dict) and payload.get("error"):
            raise AssertionError("native_webdriver_" + payload["error"])
        return payload

    def _element(self, selector):
        value = self._wd("POST", "/element", {"using": "css selector", "value": selector})
        return value["element-6066-11e4-a52e-4f735466cecf"]

    def _action(self, operation, selector):
        self.owner.actions.append({"instance": self.name, "operation": operation,
                                   "selector": selector, "at": datetime.now(timezone.utc).isoformat()})

    def click(self, selector):
        self._action("click", selector)
        if self.page is not None:
            self.page.locator(selector).click(timeout=30000)
        else:
            self._wd("POST", "/element/" + self._element(selector) + "/click", {})

    def fill(self, selector, value):
        self._action("fill", selector)
        if self.page is not None:
            self.page.locator(selector).fill(str(value), timeout=30000)
        else:
            element = self._element(selector)
            self._wd("POST", "/element/" + element + "/clear", {})
            self._wd("POST", "/element/" + element + "/value", {"text": str(value)})

    def double_click(self, selector):
        self._action("double_click", selector)
        if self.page is None:
            raise ResourceBlocked("native_double_click_adapter_unavailable")
        # Observe actual trusted input in the owned installed WebView. A driver
        # action label alone cannot prove that two clicks reached the product.
        # Capture on document because the first handler can replace the button.
        key = '__pku_e2e_input_' + os.urandom(12).hex()
        self.page.evaluate("""({key, selector}) => {
            const rows = [];
            const listener = event => {
                if (event.target instanceof Element && event.target.closest(selector)) {
                    rows.push({type: event.type, trusted: event.isTrusted,
                        detail: event.detail, button: event.button});
                }
            };
            for (const type of ['pointerdown', 'click', 'dblclick'])
                document.addEventListener(type, listener, true);
            window[key] = {rows, listener};
        }""", {'key': key, 'selector': selector})
        try:
            self.page.locator(selector).dblclick(timeout=30000)
        finally:
            events = self.page.evaluate("""key => {
                const observer = window[key];
                if (!observer) return [];
                for (const type of ['pointerdown', 'click', 'dblclick'])
                    document.removeEventListener(type, observer.listener, true);
                delete window[key];
                return observer.rows;
            }""", key)
            clicks = [row for row in events if row['type'] == 'click' and row['trusted'] is True]
            self.owner.actions.append({'instance': self.name, 'operation': 'observed-native-double-click',
                'selector': selector, 'actual_native_double_click': {
                    'boundary': 'owned-installed-webview-input', 'events': events,
                    'observed': self.name != 'external' and len(clicks) == 2}})

    def text(self, selector="body"):
        if self.page is not None:
            return self.page.locator(selector).inner_text(timeout=30000)
        return self._wd("GET", "/element/" + self._element(selector) + "/text")

    def visible(self, selector):
        if self.page is not None:
            return self.page.locator(selector).count() == 1 and self.page.locator(selector).is_visible()
        return self._wd("GET", "/element/" + self._element(selector) + "/displayed") is True

    def attr(self, selector, name):
        if name.lower() in {"value", "password", "token"}:
            raise ResourceBlocked("credential_attribute_read_forbidden")
        if self.page is not None:
            return self.page.locator(selector).get_attribute(name)
        return self._wd("GET", "/element/" + self._element(selector) + "/attribute/" + name)

    def upload(self, selector, paths):
        self._action("upload", selector)
        if self.page is None:
            raise ResourceBlocked("native_mac_file_picker_not_available")
        self.page.locator(selector).set_input_files([str(Path(path).resolve()) for path in paths])

    def is_checked(self, selector):
        if self.page is not None:
            return self.page.locator(selector).is_checked()
        return self._wd("GET", "/element/" + self._element(selector) + "/selected") is True

    @contextmanager
    def guard_submission(self):
        """Fail-safe for an unchecked-button trial, never a substitute success.

        Passing runs send no matching request, so nothing is replaced. If a
        defect tries to submit, block it before the panel and mark the assertion
        false. This interlock must not be used to pretend submission was tested.
        """
        if self.page is None:
            raise ResourceBlocked("native_submission_safety_interlock_unavailable")
        attempted = []
        origin = urlsplit(self.origin)
        def protect(route):
            request = route.request
            parts = urlsplit(request.url)
            is_submit = (request.method == "POST" and parts.netloc == origin.netloc
                         and parts.path.startswith("/api/campus/courses/")
                         and "/assignments/" in parts.path and parts.path.endswith("/submit"))
            if is_submit:
                attempted.append(parts.path)
                self.owner.network.append({"instance": self.name, "method": "POST",
                                           "host": parts.hostname, "path": parts.path,
                                           "safety_interlock": "blocked-unapproved-submission"})
                route.abort("blockedbyclient")
            else:
                route.continue_()
        self.page.route("**/*", protect)
        try:
            yield attempted
        finally:
            self.page.unroute("**/*", protect)
            self._dialog_decision = None
            self._dialog_handled = False
            self._dialog = None

    def confirm(self, accepted=True):
        if self.page is None:
            self._wd("POST", "/alert/accept" if accepted else "/alert/dismiss", {})
        else:
            if self._dialog_decision is not None:
                if accepted != self._dialog_decision:
                    raise AssertionError("native_dialog_decision_changed")
                wait(lambda: self._dialog_handled or self.visible('[data-product-confirmation]'))
                if not self._dialog_handled:
                    self.screenshot('native-confirmation-before-user-choice')
                    self.click('[data-product-confirmation] [data-confirmation-decision="' +
                               ('confirm' if accepted else 'cancel') + '"]')
                    wait(lambda: not self.visible('[data-product-confirmation]'))
                self._dialog_decision = None
                self._dialog_handled = False
            else:
                dialog = wait(lambda: self._dialog)
                dialog.accept() if accepted else dialog.dismiss()
            self._dialog = None

    def prompt(self, text):
        if self.page is None:
            self._wd("POST", "/alert/text", {"text": text})
            self._wd("POST", "/alert/accept", {})
        else:
            dialog = wait(lambda: self._dialog)
            dialog.accept(text)
            self._dialog = None

    def wait(self, predicate, timeout=30):
        def observed():
            if self.page is not None and not self.page.is_closed():
                self.page.wait_for_timeout(50)
            return predicate()
        return wait(observed, timeout)

    def request_read(self, path):
        if not path.startswith("/api/") or "://" in path:
            raise ResourceBlocked("panel_observation_requires_relative_api_path")
        if not self.origin:
            raise ResourceBlocked("native_panel_origin_unobserved")
        # Parent selection hydrates the account's existing Notion tree before
        # returning. Keep a bounded wait large enough for that real read; this
        # changes no expected state or assertion and does not retry mutations.
        timeout = 180 if path == "/api/campus/notion/parents" else 30
        response = httpx.get(self.origin + path, timeout=timeout, trust_env=False)
        response.raise_for_status()
        return response.json()

    def screenshot(self, name):
        stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)
        path = self.owner.case_dir / (stem + ".png")
        suffix = 2
        while path.exists() or path in self.owner.ui_evidence:
            path = self.owner.case_dir / (stem + '-' + str(suffix) + '.png')
            suffix += 1
        if self.page is not None:
            # Credentials, student answers and email inputs must not enter
            # screenshots, including non-password controls and contenteditables.
            self.page.screenshot(path=str(path), full_page=False,
                                 mask=[self.page.locator("input,textarea,[contenteditable=true],.account-email,.auth-email")])
        else:
            # Do not dump unredacted native screenshots merely to fill a role.
            raise ResourceBlocked("mac_native_redacted_screenshot_not_implemented")
        self.owner.ui_evidence.append(path)
        return path


class NativeRun:
    def __init__(self, scenario, mode, context):
        self.scenario, self.mode, self.context = scenario, mode, context
        self.config = deepcopy(context["config"])
        self.case_dir = Path(context["run_dir"]) / (scenario["id"] + "-" + mode)
        self.case_dir.mkdir(exist_ok=False)
        self.assertions, self.fault_variants, self.blockers = {}, {}, []
        self.actions, self.network, self.ui_evidence = [], [], []
        self.sessions, self.processes = {}, {}
        self._playwright, self._browsers = None, []
        self._fault_boundary = None
        self._live_account_observer = None
        self._notion_boundary = None
        self._media_boundary = None
        self._baseline = None
        self._budget = {"points": 0, "seconds": 0}
        # A failed login or account switch in one case must not silently poison
        # another case's initial fixture. Seeds are runner-owned TEMP fixtures,
        # never the student's profile or a copy of a second device's secrets.
        for name, instance in self.config.get("instances", {}).items():
            if "seed_dir" not in instance:
                continue
            test_root = Path(self.need("test_root")).resolve()
            seed = Path(instance["seed_dir"]).resolve()
            destination = test_root / "profiles" / Path(context["run_dir"]).name / (scenario["id"] + "-" + mode) / name
            if (seed == test_root or not seed.is_relative_to(test_root)
                    or destination.exists() or not seed.is_dir()):
                raise ResourceBlocked("case_seed_not_isolated_or_destination_exists")
            shutil.copytree(seed, destination)
            instance["app_dir"] = str(destination)
        from .oracles import Oracle
        self.oracle = Oracle(self.config, self.case_dir)

    def need(self, key):
        return need(self.config, key)

    def credential(self, key):
        return credential(self.config, key)

    def blocked(self, code, detail=None):
        return ResourceBlocked(code)

    def check(self, key, passed, detail=None):
        if key not in self.scenario["assertions"] or type(passed) is not bool:
            raise AssertionError("invalid_assertion_identity_or_boolean")
        self.assertions[key] = self.assertions.get(key, True) and passed
        # Details may contain native content; store only a deterministic hash.
        if detail is not None:
            self.actions.append({"assertion": key, "passed": passed,
                                 "observation_sha256": hashlib.sha256(
                                     json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()})

    def fault_check(self, variant, passed, detail=None):
        if type(passed) is not bool:
            raise AssertionError("invalid_fault_boolean")
        self.fault_variants[variant] = self.fault_variants.get(variant, True) and passed

    def authorize_cost(self, points=0, seconds=0):
        budget = self.need("budget")
        # Per-case profile configuration is isolated, while approval and the
        # total spending ceiling belong to the complete run.
        approved_budget = self.context["config"].setdefault("budget", deepcopy(budget))
        if approved_budget != budget:
            raise ResourceBlocked("run_budget_policy_changed")
        if budget.get("approved") is not True:
            raise ResourceBlocked("paid_budget_not_approved")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (points, seconds)):
            raise ValueError("invalid_cost")
        for field, value in (("points", points), ("seconds", seconds)):
            if self._budget[field] + value > budget.get("max_" + field, 0):
                raise ResourceBlocked("budget_exceeded_" + field)
        # Reserve across the entire run in a file created by this runner, not
        # just per case; loss/unknown responses keep their reservation.
        ledger_path = Path(self.context["run_dir"]) / "cost-reservations.json"
        used = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"points": 0, "seconds": 0}
        if used["points"] + points > budget.get("max_points", 0) or used["seconds"] + seconds > budget.get("max_seconds", 0):
            raise ResourceBlocked("total_run_budget_exceeded")
        used["points"] += points
        used["seconds"] += seconds
        ledger_path.write_text(json.dumps(used), encoding="utf-8")
        self._budget["points"] += points
        self._budget["seconds"] += seconds

    def _installed_binary(self, name):
        instance = self.need("instances." + name)
        path = Path(instance["installed_path"]).resolve()
        if path.suffix == ".app":
            binaries = list((path / "Contents/MacOS").iterdir())
            if len(binaries) != 1:
                raise ResourceBlocked("mac_bundle_binary_ambiguous")
            path = binaries[0]
        if not path.is_file() or path.name.lower() not in {"pku-desktop.exe", "pku-desktop"}:
            raise ResourceBlocked("native_installed_binary_missing")
        receipt = self.need("installation_receipt")
        if receipt.get("artifact_sha256") != self.context["artifact_sha256"]:
            raise ResourceBlocked("candidate_installation_receipt_mismatch")
        if receipt.get("installed_sha256") != sha256(path):
            raise ResourceBlocked("installed_binary_hash_mismatch")
        if receipt.get("version") != self.context["version"]:
            raise ResourceBlocked("installed_version_mismatch")
        if installed_version(path) != self.context["version"]:
            raise ResourceBlocked("actual_installed_os_version_mismatch")
        if name == "primary" and Path(instance["installed_path"]).resolve() != Path(self.context["installed_path"]).resolve():
            raise ResourceBlocked("runner_installed_path_mismatch")
        return path

    def session(self, name="primary"):
        if self.scenario.get("device_scope") == "single-machine" and name != "primary":
            raise ResourceBlocked("single_machine_scope_forbids_secondary_native_instance")
        if name in self.sessions:
            return self.sessions[name]
        instance = self.need("instances." + name)
        binary = self._installed_binary(name)
        test_root = Path(self.need("test_root")).resolve()
        app_dir = Path(instance["app_dir"]).resolve()
        if not app_dir.is_relative_to(test_root) or app_dir == test_root:
            raise ResourceBlocked("instance_data_not_isolated")
        if app_dir == (Path.home() / "PKU-All-in-Notion").resolve():
            raise ResourceBlocked("real_student_data_directory_forbidden")
        # Bootstrap can publish to a shared home before our first click. UI
        # data isolation alone does not isolate the remote Notion workspace.
        from .catalog_journeys import _catalog_scope_preflight
        _catalog_scope_preflight(self)
        app_dir.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        # Explicit test profile, never repurpose HOME/USERPROFILE.
        environment.update({"PKU_DESKTOP_DATA_DIR": str(app_dir), "DATA_DIR": str(app_dir / "data"),
                            "PYTHONUTF8": "1"})
        if self.context.get('_windows_disk_boundary') is not None:
            from .windows_disk_boundary import WindowsDiskBoundary
            disk = self.context['_windows_disk_boundary']
            if not isinstance(disk, WindowsDiskBoundary):
                raise ResourceBlocked('actual_owned_windows_disk_boundary_required')
            disk.bind(self)
            # The controller verified this exact physical volume belongs to
            # the isolated profile. Its ordinary drive alias also works in
            # the packaged Python 3.11, which misparses a GUID volume root.
            environment['DATA_DIR'] = disk.ready['native_data_dir']
        # Do not shadow credentials legitimately saved by the UI in this test
        # instance's .env on restart; only strip inherited developer credentials.
        for key in ("NOTION_TOKEN", "PLATFORM_TOKEN", "PKU_USERNAME", "PKU_PASSWORD", "OPENAI_API_KEY"):
            environment.pop(key, None)
        environment.pop("PYTHONPATH", None)
        environment.pop('PKU_NOTION_API_BASE', None)
        if self.mode == "fault":
            from .fault_boundary import AccountFaultBoundary
            if self._fault_boundary is None:
                self._fault_boundary = AccountFaultBoundary(self)
            environment["CLOUD_TRANSCRIBE_URL"] = self._fault_boundary.origin + "/v1/transcribe"
            parent = self.config.get('learning', {}).get('notion_test_parent_id')
            if parent:
                from .notion_fault_boundary import NotionFaultBoundary
                if self._notion_boundary is None:
                    self._notion_boundary = NotionFaultBoundary(self, parent)
                    media_scope = self.config.get('learning', {}).get('notion_media_uploads')
                    if media_scope is not None:
                        self._notion_boundary.configure_media_uploads(media_scope)
                environment['PKU_NOTION_API_BASE'] = self._notion_boundary.origin
            media = self.config.get('learning', {}).get('expired_media_fixture')
            if media:
                from .media_fault_boundary import MediaFaultBoundary
                if self._media_boundary is None:
                    self._media_boundary = MediaFaultBoundary(self, media)
                environment['PKU_CAMPUS_MEDIA_PROXY'] = self._media_boundary.origin
        elif self.mode == 'live' and self.scenario['id'] == 'JOURNEY-01':
            from .identity_live import AccountRequestObserver
            if self._live_account_observer is None:
                self._live_account_observer = AccountRequestObserver(self)
            environment['CLOUD_TRANSCRIBE_URL'] = self._live_account_observer.origin + '/v1/transcribe'
        if "external_browser" in self.config:
            self._launch_external_browser()
            resource = self.config["external_browser"]
            # Python's standard webbrowser controller opens the real product
            # OAuth URL in the browser process this run actually owns.
            environment["BROWSER"] = ('"' + resource["executable_path"] + '" '
                '--user-data-dir="' + resource["profile_dir"] + '" %s')
        if self.context["target"] == "windows" and sys.platform != "win32":
            raise ResourceBlocked("windows_journey_requires_real_windows")
        if self.context["target"] == "macos" and sys.platform != "darwin":
            raise ResourceBlocked("mac_journey_requires_real_mac")
        if sys.platform == "win32":
            inventory = process_inventory()
            current = next(row for row in inventory if row["pid"] == os.getpid())
            if current["session_id"] == 0:
                raise ResourceBlocked("interactive_windows_desktop_required_not_ssh_session0")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-address=127.0.0.1 --remote-debugging-port={port}"
            environment["WEBVIEW2_USER_DATA_FOLDER"] = str(app_dir / "webview-profile")
            endpoint = f"http://127.0.0.1:{port}"
        else:
            endpoint = loopback_url(instance["webdriver_endpoint"])
            environment["TAURI_WEBDRIVER_PORT"] = str(urlsplit(endpoint).port)
        process = subprocess.Popen([str(binary)], cwd=str(app_dir), env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes[name] = process
        try:
            if sys.platform == "win32":
                wait(lambda: self._ready(endpoint + "/json/version", process))
                endpoint_owned_by(endpoint, process.pid)
                from playwright.sync_api import sync_playwright
                if self._playwright is None:
                    self._playwright = sync_playwright().start()
                browser = self._playwright.chromium.connect_over_cdp(endpoint)
                self._browsers.append(browser)
                def native_panel_page():
                    # The sync Playwright transport dispatches navigation events
                    # during API calls, not during time.sleep in our outer wait.
                    # Pump it before inspecting the live native WebView targets.
                    pages = [page for ctx in browser.contexts for page in ctx.pages]
                    if pages:
                        pages[0].wait_for_timeout(100)
                    else:
                        browser.new_browser_cdp_session().send("Target.getTargets")
                    return next((page for ctx in browser.contexts for page in ctx.pages
                                 if urlsplit(page.url).hostname == "127.0.0.1" and
                                 urlsplit(page.url).path == "/app"), None)
                page = wait(native_panel_page)
                if page.evaluate("typeof window.__TAURI_INTERNALS__") == "undefined":
                    raise ResourceBlocked("not_a_native_tauri_webview")
                ui = NativeUI(self, name, page=page)
                # A native shell can still accidentally load an unrelated
                # localhost service. Verify the panel as well as WebView CDP.
                endpoint_owned_by(ui.origin, process.pid)
            else:
                wait(lambda: self._ready(endpoint + "/status", process))
                endpoint_owned_by(endpoint, process.pid)
                response = httpx.post(endpoint + "/session", json={"capabilities": {"alwaysMatch": {}}},
                                      timeout=30, trust_env=False)
                response.raise_for_status()
                session_id = response.json()["value"]["sessionId"]
                ui = NativeUI(self, name, endpoint=endpoint, session_id=session_id)
                location = ui._wd("GET", "/url")
                parts = urlsplit(location)
                if parts.hostname != "127.0.0.1" or parts.path != "/app":
                    raise ResourceBlocked("mac_native_panel_not_loaded")
                ui.origin = location.rsplit("/app", 1)[0]
            if process.poll() is not None:
                raise ResourceBlocked("native_single_instance_reused_wrong_profile")
            self.sessions[name] = ui
            ui.screenshot("startup-" + name)
            return ui
        except Exception:
            self.quit(name)
            raise

    @staticmethod
    def _ready(url, process):
        if process.poll() is not None:
            raise ResourceBlocked("native_process_exited_before_webview_ready")
        try:
            return httpx.get(url, timeout=0.5, trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    def runtime(self, name="primary"):
        process = self.processes.get(name)
        row = next((row for row in process_inventory() if process and row["pid"] == process.pid), None)
        if name == "external":
            resource = self.need("external_browser")
            return {"target": self.context["target"], "pid": process.pid if process else None,
                    "alive": bool(process and process.poll() is None), "process": row,
                    "role": "owned-oauth-browser", "executable_sha256": sha256(resource["executable_path"])}
        return {"target": self.context["target"], "pid": process.pid if process else None,
                "alive": bool(process and process.poll() is None), "process": row,
                "version": installed_version(self._installed_binary(name)), "artifact_sha256": self.context["artifact_sha256"],
                "installed_path": str(self._installed_binary(name)),
                "installed_sha256": sha256(self._installed_binary(name)),
                "single_instance": len([item for item in process_inventory() if row and item.get("path") == row.get("path")]) == 1}

    def quit(self, name="primary", require_graceful=False):
        ui = self.sessions.get(name)
        process = self.processes.get(name)
        if (sys.platform == "win32" and name != "external"
                and process and process.poll() is None and ui
                and getattr(ui, "origin", None) and callable(getattr(ui, "request_read", None))):
            def catalog_terminal():
                try:
                    task = ui.request_read("/api/recordings/task")
                    if (not isinstance(task, dict) or task.get("state") not in
                            {"idle", "running", "done", "partial", "failed", "cancelled"}):
                        raise ValueError("invalid task observation")
                    background = task.get("background_sync")
                    if background is not None:
                        if (not isinstance(background, dict) or background.get("state") not in
                                {"idle", "running", "done", "partial", "failed", "cancelled", "interrupted"}):
                            raise ValueError("invalid background task observation")
                        if background["state"] == "running":
                            if background.get("kind") != "sync" or background.get("scope") != "background":
                                raise ValueError("invalid background writer identity")
                            return False
                    if task["state"] == "running":
                        return False
                    if self.scenario["id"] in {"JOURNEY-05", "MAIN-CHAIN"}:
                        status = ui.request_read("/api/recordings/task")
                        exercise = status.get("exercise_job") if isinstance(status, dict) else None
                        if not isinstance(exercise, dict) or exercise.get("state") not in {"idle", "running"}:
                            raise ValueError("exercise writer observation unavailable")
                        if exercise["state"] == "running":
                            return False
                    return True
                except Exception as exc:
                    raise ResourceBlocked("catalog_state_unavailable_before_native_quit") from exc
            # A normal close must not interrupt an active mutation merely
            # because the driver's earlier assertion timed out. Fault/crash
            # journeys still use their explicit interruption path.
            if not catalog_terminal():
                ui.wait(catalog_terminal, timeout=900)
        self.sessions.pop(name, None)
        graceful = False
        close_error = None
        try:
            if process and process.poll() is None and sys.platform == "win32":
                if name == 'external':
                    close_owned_windows(process.pid, browser_path=self.need('external_browser.executable_path'))
                else:
                    close_owned_windows(process.pid)
            elif ui and ui.page is not None:
                ui.page.close()
            elif ui:
                ui._wd("DELETE", "")
        except Exception as exc:
            close_error = exc.code if isinstance(exc, ResourceBlocked) else type(exc).__name__
        finally:
            if process and process.poll() is None:
                try:
                    process.wait(timeout=10)
                    graceful = close_error is None
                except subprocess.TimeoutExpired:
                    # Terminating only Rust leaves its Python/sidecar descendants
                    # listening forever on Windows. The root is our still-live
                    # Popen handle; terminate this exact owned tree, never a name.
                    if sys.platform == "win32":
                        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=15, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        process.terminate()
                    process.wait(timeout=10)
            elif process:
                graceful = close_error is None
        receipt = {"native_quit": {"instance": name, "pid": process.pid if process else None,
                   "graceful": graceful, "close_error": close_error,
                   "method": "owned-windows-WM_CLOSE" if sys.platform == "win32" else "browser-close"}}
        self.actions.append(receipt)
        if require_graceful and not graceful:
            raise AssertionError('native_graceful_quit_failed_cleanup_is_not_evidence')
        return receipt['native_quit']

    def restart(self, name="primary", *, require_graceful=True):
        # Keep the explicit proof requirement in callers; never permit a
        # forced-cleanup restart to be counted as native acceptance.
        if require_graceful is not True:
            raise ValueError('native_restart_requires_graceful_quit')
        self.quit(name, require_graceful=True)
        return self.session(name)

    def crash_and_restart(self, name="primary"):
        """Kill our actual installed root, observe cleanup, then reopen it."""
        if self.mode != 'fault' or sys.platform != 'win32' or name == 'external':
            raise ResourceBlocked('actual_native_crash_requires_owned_windows_fault_run')
        process = self.processes.get(name)
        ui = self.sessions.get(name)
        installed = self._installed_binary(name).resolve()
        test_root = Path(self.need('test_root')).resolve()
        runtime = self.runtime(name)
        if (not process or process.poll() is not None or not ui or not ui.origin
                or not installed.is_relative_to(test_root) or not runtime.get('single_instance')
                or not runtime.get('process') or not runtime['process'].get('path')
                or Path(runtime['process']['path']).resolve() != installed):
            raise ResourceBlocked('actual_native_crash_process_ownership_unproven')
        inventory = process_inventory()
        descendants, parents = set(), {process.pid}
        while True:
            added = {row['pid'] for row in inventory if row['parent_pid'] in parents} - descendants
            if not added:
                break
            descendants |= added
            parents |= added
        owned = [row for row in inventory if row['pid'] in descendants and row.get('path')
                 and Path(row['path']).resolve().is_relative_to(installed.parent)]
        if not any(row['name'].lower() == 'python.exe' for row in owned):
            raise ResourceBlocked('actual_native_crash_installed_backend_unobserved')
        origin = ui.origin
        receipt = {'instance': name, 'pid': process.pid, 'backend_pids': [row['pid'] for row in owned],
                   'boundary': 'owned-installed-Popen-root-only', 'observed': False,
                   'automatic_backend_cleanup': False, 'old_panel_port_closed': False}
        self.actions.append({'actual_native_crash': receipt})
        process.kill()
        process.wait(timeout=10)
        receipt['observed'] = process.poll() is not None
        deadline = time.monotonic() + 8
        while True:
            current = {row['pid']: row for row in process_inventory()}
            remaining = [row for row in owned if row['pid'] in current]
            for row in remaining:
                if current[row['pid']].get('path') != row['path']:
                    raise ResourceBlocked('actual_native_crash_pid_reused_during_observation')
            if not remaining:
                receipt['automatic_backend_cleanup'] = True
                break
            if time.monotonic() >= deadline:
                raise AssertionError('actual_native_crash_backend_leak_no_forced_cleanup_acceptance')
            time.sleep(.2)
        try:
            httpx.get(origin + '/healthz', timeout=1, trust_env=False)
        except httpx.TransportError:
            receipt['old_panel_port_closed'] = True
        else:
            raise AssertionError('actual_native_crash_old_panel_still_reachable')
        self.sessions.pop(name)
        return self.session(name)

    def upgrade(self, previous_artifact=None):
        installation = self.need("installation")
        if installation.get("approved") is not True:
            raise ResourceBlocked("os_installation_not_approved")
        if sys.platform != "win32":
            raise ResourceBlocked("isolated_mac_os_upgrade_not_provisioned")
        instance = self.need("instances.primary")
        installed = Path(instance["installed_path"]).resolve()
        test_root = Path(self.need("test_root")).resolve()
        if not installed.is_relative_to(test_root) or installed.parent == test_root:
            raise ResourceBlocked("installer_destination_not_isolated")
        # Never run user-supplied shell commands. NSIS /D must be the last arg.
        artifacts = ([Path(previous_artifact)] if previous_artifact else []) + [Path(self.context["artifact"])]
        for artifact in artifacts:
            if not artifact.is_file() or artifact.suffix.lower() != ".exe":
                raise ResourceBlocked("candidate_nsis_installer_missing")
            expected = self.context["artifact_sha256"] if artifact == Path(self.context["artifact"]) else self.need("previous_artifact_sha256")
            if sha256(artifact) != expected:
                raise ResourceBlocked("os_upgrade_artifact_hash_mismatch")
            process = self.processes.get("primary")
            if process is not None and process.poll() is None:
                self.quit("primary", require_graceful=True)
            from .windows_install import install_isolated
            installed_receipt = install_isolated(artifact, installed, test_root)
        if installed_receipt['version'] != self.context['version']:
            raise AssertionError('native_os_installer_version_mismatch')
        receipt = {**installed_receipt,
                   "installed_path": str(installed), "generated_by": "os-installer",
                   "at": datetime.now(timezone.utc).isoformat()}
        self.config["installation_receipt"] = receipt
        from uuid import uuid4
        receipt_path = self.case_dir / ('installation-receipt-' + uuid4().hex + '.json')
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.actions.append({'actual_os_upgrade': {**receipt,
            'receipt_file': receipt_path.name, 'receipt_sha256': sha256(receipt_path)}})
        self.ui_evidence.append(receipt_path)
        return receipt

    def _launch_external_browser(self):
        process = self.processes.get("external")
        if process is not None and process.poll() is None:
            return
        if sys.platform != "win32":
            raise ResourceBlocked("owned_external_browser_requires_windows")
        resource = self.need("external_browser")
        executable = Path(need(resource, "executable_path")).resolve()
        profile = Path(need(resource, "profile_dir")).resolve()
        test_root = Path(self.need("test_root")).resolve()
        if (not executable.is_file() or executable.name.lower() not in {"chrome.exe", "msedge.exe"}
                or not profile.is_relative_to(test_root) or profile == test_root):
            raise ResourceBlocked("external_browser_executable_or_isolated_profile_invalid")
        profile.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}"
        process = subprocess.Popen([str(executable), "--user-data-dir=" + str(profile),
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port),
            "--no-first-run", "--no-default-browser-check", "--disable-background-mode", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes["external"] = process
        wait(lambda: self._ready(endpoint + "/json/version", process))
        endpoint_owned_by(endpoint, process.pid)
        resource.update(executable_path=str(executable), profile_dir=str(profile), cdp_endpoint=endpoint)
        self.actions.append({"operation": "launch-owned-external-browser", "pid": process.pid})

    def external_page(self, url=None):
        self._launch_external_browser()
        resource = self.need("external_browser")
        endpoint = loopback_url(resource["cdp_endpoint"])
        # A JSON nonce is not OS ownership proof. This runner must have actually
        # launched the external browser before any authenticated navigation.
        process = self.processes.get("external")
        if process is None or process.poll() is not None:
            raise ResourceBlocked("owned_external_browser_process_exited")
        endpoint_owned_by(endpoint, process.pid)
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
        browser = self._playwright.chromium.connect_over_cdp(endpoint)
        self._browsers.append(browser)
        page = browser.contexts[0].new_page()
        if url:
            if urlsplit(url).hostname not in {"www.notion.so", "notion.so", "www.notion.com", "app.notion.com", "course.pku.edu.cn"}:
                raise ResourceBlocked("external_browser_host_not_allowed")
            page.goto(url)
        return NativeUI(self, "external", page=page)

    def verify_pair(self, path):
        pair = self.need("cross_platform")
        path = Path(path).resolve()
        signature = Path(pair["signature_path"]).resolve()
        public_key = Path(pair["trusted_public_key_path"]).resolve()
        if sha256(public_key) != pair.get("trusted_public_key_sha256"):
            raise ResourceBlocked("pair_public_key_not_pinned")
        # Use the existing controlled publisher's public key; no new key or
        # self-signed local runner attestation can make an unexecuted pair pass.
        command = ["openssl", "dgst", "-sha256", "-verify", str(public_key),
                   "-signature", str(signature), str(path)]
        result = subprocess.run(command, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise ResourceBlocked("pair_execution_signature_invalid")
        value = json.loads(path.read_text("utf-8"))
        observed = datetime.fromisoformat(value["finished_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 86400:
            raise ResourceBlocked("pair_execution_expired_or_future")
        if value.get("target") != "windows" or value.get("entry") != "installed-native":
            raise ResourceBlocked("pair_requires_actual_windows_native_execution")
        if value.get("pair_nonce") != pair.get("nonce") or not pair.get("nonce"):
            raise ResourceBlocked("pair_nonce_mismatch")
        return value

    @contextmanager
    def fault(self, variant):
        if self.mode != "fault":
            raise ResourceBlocked("fault_injection_forbidden_in_live")
        if variant in {'oauth-cancel', 'switch-account', 'upgrade-restart'}:
            from .identity_action_boundary import inject
            self.session()
            with inject(self, variant) as receipt:
                yield receipt
            return
        from .fault_boundary import SUPPORTED
        from .notion_fault_boundary import SUPPORTED as NOTION_SUPPORTED
        from .draft_fault_boundary import SUPPORTED as DRAFT_SUPPORTED
        if variant in DRAFT_SUPPORTED:
            from .draft_fault_boundary import inject
            self.session()
            with inject(self, variant) as receipt:
                yield receipt
            return
        if variant == 'restart-after-charge':
            from .restart_charge_boundary import inject
            with inject(self) as receipt:
                yield receipt
            return
        if variant == 'disk-full':
            from .windows_disk_boundary import WindowsDiskBoundary
            boundary = self.context.get('_windows_disk_boundary')
            if not isinstance(boundary, WindowsDiskBoundary):
                raise ResourceBlocked('software_capability_missing_fault_disk-full')
            self.session()
            with boundary.inject(self) as receipt:
                yield receipt
            return
        if variant == 'double-click':
            start = len(self.actions)
            receipt = {'variant': variant, 'boundary': 'owned-installed-webview-input', 'observed': False}
            yield receipt
            observations = [row['actual_native_double_click'] for row in self.actions[start:]
                if 'actual_native_double_click' in row and row.get('instance') != 'external']
            if len(observations) != 1:
                raise ResourceBlocked('actual_native_double_click_fault_not_exercised')
            events = observations[0].get('events', [])
            clicks = [row for row in events if row.get('type') == 'click' and row.get('trusted') is True]
            if (observations[0].get('boundary') != 'owned-installed-webview-input'
                    or observations[0].get('observed') is not True or len(clicks) != 2):
                raise ResourceBlocked('actual_native_double_click_fault_not_exercised')
            receipt.update(observed=True, trusted_click_count=2, events=events)
            return
        if variant == 'kill-process-and-restart':
            start = len(self.actions)
            yield {'variant': variant, 'boundary': 'actual-owned-native-process'}
            crashes = [row['actual_native_crash'] for row in self.actions[start:]
                       if 'actual_native_crash' in row]
            if len(crashes) != 1 or not all(crashes[0].get(key) is True for key in
                    ('observed', 'automatic_backend_cleanup', 'old_panel_port_closed')):
                raise ResourceBlocked('actual_native_crash_fault_not_exercised')
            return
        if variant == 'network-restored':
            self.session()
            with self._fault_boundary.inject('offline') as receipt:
                yield receipt
            return
        if variant in NOTION_SUPPORTED:
            self.session()
            if self._notion_boundary is None:
                raise ResourceBlocked('notion_test_parent_required_for_real_fault')
            with self._notion_boundary.inject(variant) as receipt:
                yield receipt
            return
        if variant == 'expired-media-url':
            self.session()
            if self._media_boundary is None:
                raise ResourceBlocked('verified_media_source_fixture_required_for_real_fault')
            with self._media_boundary.inject(variant) as receipt:
                yield receipt
            return
        if variant not in SUPPORTED:
            raise ResourceBlocked("software_capability_missing_fault_" + variant)
        self.session()
        with self._fault_boundary.inject(variant) as receipt:
            yield receipt

    def observe(self):
        local_files, logs = [], []
        for name, instance in self.config.get("instances", {}).items():
            root = Path(instance["app_dir"])
            for path in (root / "data").rglob("*"):
                if path.is_file():
                    local_files.append({"instance": name, "path": path.relative_to(root).as_posix(),
                                        "bytes": path.stat().st_size, "sha256": sha256(path)})
            log = root / "panel.log"
            if log.exists():
                # No raw log serialization: record correlation IDs only.
                logs.extend(re.findall(r"failure_id[=:]([A-F0-9]{8})", log.read_text("utf-8", errors="replace")))
        return {"network": {"requests": list(self.network), "request_count": len(self.network)},
                "local_files": local_files, "private_files": local_files, "failure_ids": logs,
                "processes": [self.runtime(name) for name in self.processes]}

    def snapshot(self):
        return {**self.oracle.snapshot(), **self.observe()}

    def finish(self):
        metrics = {"download_bytes": None, "upload_bytes": None, "charged_points": None, "charged_seconds": None}
        measurement_file = None
        if self._baseline is not None:
            try:
                after = self.oracle.snapshot(include_billing=True)
                metrics = self.oracle.metrics(self._baseline, after, require_billing=True)
                # Save only measurement inputs, never profile/student content.
                # NIC totals are explicitly a host upper bound, not per-app IO.
                measurement_file = self.case_dir / "measurements.json"
                measurement_file.write_text(json.dumps({
                    "before": {key: self._baseline[key] for key in ("billing", "traffic")},
                    "after": {key: after[key] for key in ("billing", "traffic")},
                    "metrics": metrics,
                    "traffic_attribution": after["traffic"]["source"],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                self.blockers.append(exc.code if isinstance(exc, ResourceBlocked)
                                     else "measurement_failed_" + type(exc).__name__)
        files = []
        if measurement_file is not None:
            files.append({"role": "oracle", "path": measurement_file.relative_to(
                self.context["run_dir"]).as_posix(), "sha256": sha256(measurement_file)})
        for name, payload, role in (
            ("ui-actions.json", {"actions": self.actions}, "ui"),
            ("network.json", {"requests": self.network}, "network"),
            ("runtime.json", self._safe_observe(), "runtime"),
        ):
            path = self.case_dir / name
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            files.append({"role": role, "path": path.relative_to(self.context["run_dir"]).as_posix(), "sha256": sha256(path)})
        for path in self.ui_evidence:
            files.append({"role": "ui", "path": path.relative_to(self.context["run_dir"]).as_posix(), "sha256": sha256(path)})
        for path in self.case_dir.glob("oracle*.json"):
            files.append({"role": "oracle", "path": path.relative_to(self.context["run_dir"]).as_posix(), "sha256": sha256(path)})
        return {"entry": "installed-native", "mocked": self.mode == "fault",
                "injection": "owned-native-request-boundary" if self.mode == "fault" else None,
                "assertions": {key: self.assertions.get(key, False) for key in self.scenario["assertions"]},
                "fault_variants": self.fault_variants, "evidence": files,
                "observed_live_hosts": sorted(self.oracle.hosts),
                "metrics": metrics, "blockers": sorted(set(self.blockers))}

    def _safe_observe(self):
        try:
            return self.observe()
        except Exception as exc:
            code = exc.code if isinstance(exc, ResourceBlocked) else "runtime_observation_failed_" + type(exc).__name__
            self.blockers.append(code)
            return {"observation_error": code, "processes": [], "local_files": []}

    def close(self):
        failures = []
        for name in list(self.processes):
            try:
                self.quit(name)
            except Exception as exc:
                failures.append(type(exc).__name__)
        if failures:
            # Preserve observers and the owned window when safe writer state is
            # unknown. Cleanup must not bypass a rejected normal close.
            raise ResourceBlocked("owned_native_cleanup_failed")
        if self._playwright is not None:
            self._playwright.stop()
        if self._fault_boundary is not None:
            self._fault_boundary.close()
        if self._live_account_observer is not None:
            self._live_account_observer.close()
        if getattr(self, '_notion_boundary', None) is not None:
            self._notion_boundary.close()
        if getattr(self, '_media_boundary', None) is not None:
            self._media_boundary.close()
