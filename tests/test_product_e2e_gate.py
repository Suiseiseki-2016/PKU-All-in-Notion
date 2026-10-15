"""Offline integrity tests; these are not live/native product acceptance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("product_gate", REPO / "scripts/product_e2e_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_explicit_physical_dpi_deferral_preserves_original_assertions_and_faults():
    manifest = gate.load_manifest(REPO)
    original = next(row for row in manifest['scenarios'] if row['id'] == 'JOURNEY-06')
    scoped = gate.scoped_scenario(original, 'windows')
    assert scoped['assertions'] == original['assertions']
    assert scoped['fault_variants'] == gate.FAULT_VARIANTS['JOURNEY-06']
    assert scoped['supported_dpi'] == [96]
    assert gate.scoped_scenario(original, 'macos')['supported_dpi'] == [96, 120, 144]
    deferred = next(row for row in gate.deferred_coverage(manifest, 'windows') if row['id'] == 'JOURNEY-06')
    assert deferred['physical_dpi'] == {'required': [96], 'deferred': [120, 144]}
    assert deferred['status'] == 'deferred-unverified'


def test_config_cannot_silently_change_fingerprinted_physical_dpi_deferral(tmp_path):
    (tmp_path / 'docs').mkdir()
    path = tmp_path / 'docs/PRODUCT_E2E_SCENARIOS.json'
    manifest = json.loads((REPO / 'docs/PRODUCT_E2E_SCENARIOS.json').read_text('utf-8'))
    row = next(row for row in manifest['scenarios'] if row['id'] == 'JOURNEY-06')
    row['deferrals']['windows']['physical_dpi']['required'] = []
    path.write_text(json.dumps(manifest), 'utf-8')
    with pytest.raises(gate.GateError):
        gate.load_manifest(tmp_path)


@pytest.fixture
def accepted(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "pku_sync").mkdir()
    (root / "pku_sync/app.py").write_text("test-source\n", encoding="utf-8")
    policy = root / "docs/PRODUCT_E2E_SCENARIOS.json"
    policy.write_bytes((REPO / "docs/PRODUCT_E2E_SCENARIOS.json").read_bytes())
    artifact = tmp_path / "candidate.exe"
    artifact.write_bytes(b"test-candidate-bytes")
    run = tmp_path / "run"
    run.mkdir()
    report_path = run / "report.json"
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=10), now - timedelta(minutes=1)
    report = {"schema_version": 1, "target": "windows", "version": "0.1.24",
              "entry": "installed-native", "installed_path": "test-installed-path",
              "runner_generated": True, "observed_live_hosts": ["pku.aeoluswu.info", "course.pku.edu.cn", "api.notion.com"],
              "deferred_coverage": gate.deferred_coverage(gate.load_manifest(root), "windows"),
              "artifact_sha256": gate.sha256(artifact), "source_digest": gate.source_digest(root),
              "manifest_sha256": gate.sha256(policy), "started_at": start.isoformat(),
              "finished_at": end.isoformat(), "metrics": {"download_bytes": 100, "upload_bytes": 20,
                                                        "charged_points": 1, "charged_seconds": 8}, "cases": []}
    for (cid, mode), row in gate.required_cases(gate.load_manifest(root), "windows").items():
        case = {"id": cid, "mode": mode, "outcome": "passed", "entry": "installed-native",
                "artifact_sha256": gate.sha256(artifact), "started_at": start.isoformat(),
                "finished_at": end.isoformat(), "mocked": False,
                "injection": "test-boundary-drop" if mode == "fault" else None,
                "assertions": {key: True for key in row["assertions"]}, "evidence": []}
        if mode == "fault":
            variants = row["fault_variants"]
            case["fault_variants"] = {variant: True for variant in variants}
        for role in ("ui", "oracle", "network", "runtime"):
            path = run / f"{cid}-{mode}-{role}.json"
            path.write_text(json.dumps({"test_fixture_only": True, "case": cid, "role": role}), encoding="utf-8")
            case["evidence"].append({"role": role, "path": path.name, "sha256": gate.sha256(path)})
        report["cases"].append(case)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return root, report_path, artifact, report, now


def verify(value):
    root, path, artifact, report, now = value
    path.write_text(json.dumps(report), encoding="utf-8")
    return gate.verify(root, path, artifact, "windows", "0.1.24", now)


def test_complete_report_shape_is_accepted_by_integrity_gate(accepted):
    # This proves the gate's shape, not that synthetic evidence is product acceptance.
    assert verify(accepted)["passed_cases"] == 12


def test_windows_scope_is_explicit_and_does_not_claim_mac_or_real_submission(accepted):
    policy = gate.load_manifest(accepted[0])
    windows, mac = gate.required_cases(policy, "windows"), gate.required_cases(policy, "macos")
    assert len(windows) == 12 and len(mac) == 14
    assert len(windows[("JOURNEY-04", "live")]["assertions"]) == 4
    assert len(mac[("JOURNEY-04", "live")]["assertions"]) == 8
    assert windows[("JOURNEY-04", "fault")]["fault_variants"] == ["double-click", "changed-file", "changed-target", "changed-deadline"]
    result = verify(accepted)
    assert result["covered_assertions"] == 39
    assert result["deferred_coverage"][0]["status"] == "deferred-unverified"
    accepted[3].pop("deferred_coverage")
    with pytest.raises(gate.GateError, match="Deferred coverage must"):
        verify(accepted)


def test_user_approved_single_machine_scope_keeps_reopen_and_restart_checks(accepted):
    policy = gate.load_manifest(accepted[0])
    windows = gate.required_cases(policy, "windows")
    mac = gate.required_cases(policy, "macos")
    identity = windows[("JOURNEY-01", "live")]
    assert identity['device_scope'] == 'single-machine'
    assert 'new-device-pulls-all-supported-shared-settings-without-copying-device-private-data' not in identity['assertions']
    assert {'upgrade-and-restart-preserve-data', 'account-switch-no-cross-account-content',
            'quit-releases-owned-processes-and-ports'} <= set(identity['assertions'])
    assert 'existing-verified-account-login-logout-and-oauth' in identity['assertions']
    assert 'wrong-expired-session-actionable' in identity['assertions']
    assert 'unverified' not in identity['fault_variants']
    assert 'register-verify-login-logout-and-oauth' in mac[("JOURNEY-01", "live")]['assertions']
    assert 'unverified' in mac[("JOURNEY-01", "fault")]['fault_variants']
    quiz = windows[("JOURNEY-05", "fault")]
    assert 'reopen-and-restart-keep-quiz-hidden' in quiz['assertions']
    assert 'course-navigation-does-not-request-quiz-apis' in quiz['assertions']
    assert quiz['fault_variants'] == ['kill-process-and-restart']
    assert sum(len(row['fault_variants']) for (_, mode), row in windows.items() if mode == 'fault') == 43
    assert 'unfinished-quiz-entry-hidden' in mac[("JOURNEY-05", "live")]['assertions']
    assert 'new-device-pulls-all-supported-shared-settings-without-copying-device-private-data' in mac[("JOURNEY-01", "live")]['assertions']


def test_single_machine_replacements_cannot_be_relaxed_without_policy_review(accepted):
    root, path, artifact, report, now = accepted
    policy_path = root / 'docs/PRODUCT_E2E_SCENARIOS.json'
    policy = json.loads(policy_path.read_text('utf-8'))
    quiz = next(row for row in policy['scenarios'] if row['id'] == 'JOURNEY-05')
    quiz['assertions'].remove('course-navigation-does-not-request-quiz-apis')
    policy_path.write_text(json.dumps(policy), 'utf-8')
    report['manifest_sha256'] = gate.sha256(policy_path)
    with pytest.raises(gate.GateError, match='Mandatory assertion coverage changed'):
        verify(accepted)


@pytest.mark.parametrize("outcome", ["failed", "blocked", "skipped", "xfail", "not_run", None])
def test_any_non_passed_case_blocks_release(accepted, outcome):
    accepted[3]["cases"][0]["outcome"] = outcome
    with pytest.raises(gate.GateError):
        verify(accepted)


@pytest.mark.parametrize("change", ["missing-case", "duplicate-case", "unknown-case", "missing-assertion",
                                   "failed-assertion", "string-pass", "live-fake", "live-injection",
                                   "missing-fault", "missing-fault-variant", "api-only", "wrong-case-package", "old-report",
                                   "future-report", "wrong-target", "wrong-version", "manual-report",
                                   "missing-live-host", "missing-metric", "infinite-metric", "missing-role",
                                   "evidence-missing", "evidence-tampered", "evidence-escape",
                                   "evidence-reused", "changed-artifact", "changed-source", "removed-policy",
                                   "reduced-assertion-policy"])
def test_release_cannot_silently_bypass_e2e(accepted, change):
    root, path, artifact, report, now = accepted
    first = report["cases"][0]
    if change == "missing-case": report["cases"].pop()
    elif change == "duplicate-case": report["cases"].append(deepcopy(first))
    elif change == "unknown-case": first["id"] = "UNKNOWN-01"
    elif change == "missing-assertion": first["assertions"].pop(next(iter(first["assertions"])))
    elif change == "failed-assertion": first["assertions"][next(iter(first["assertions"]))] = False
    elif change == "string-pass": first["assertions"][next(iter(first["assertions"]))] = "true"
    elif change == "live-fake": first["mocked"] = True
    elif change == "live-injection": first["injection"] = "mock-network"
    elif change == "missing-fault": report["cases"][1]["injection"] = None
    elif change == "missing-fault-variant": report["cases"][1]["fault_variants"].pop(next(iter(report["cases"][1]["fault_variants"])))
    elif change == "api-only": first["entry"] = "source-browser"
    elif change == "wrong-case-package": first["artifact_sha256"] = "wrong"
    elif change == "old-report": report["started_at"] = (now - timedelta(days=2)).isoformat()
    elif change == "future-report": report["finished_at"] = (now + timedelta(days=1)).isoformat()
    elif change == "wrong-target": report["target"] = "macos"
    elif change == "wrong-version": report["version"] = "old"
    elif change == "manual-report": report["runner_generated"] = False
    elif change == "missing-live-host": report["observed_live_hosts"] = ["pku.aeoluswu.info"]
    elif change == "missing-metric": report["metrics"].pop("charged_points")
    elif change == "infinite-metric": report["metrics"]["charged_points"] = float("inf")
    elif change == "missing-role": first["evidence"].pop()
    elif change == "evidence-missing": (path.parent / first["evidence"][0]["path"]).unlink()
    elif change == "evidence-tampered": (path.parent / first["evidence"][0]["path"]).write_text("changed", encoding="utf-8")
    elif change == "evidence-escape": first["evidence"][0]["path"] = "../candidate.exe"
    elif change == "evidence-reused": report["cases"][1]["evidence"][0] = deepcopy(first["evidence"][0])
    elif change == "changed-artifact": artifact.write_bytes(b"different installer")
    elif change == "changed-source": (root / "pku_sync/app.py").write_text("changed", encoding="utf-8")
    elif change == "removed-policy":
        policy = root / "docs/PRODUCT_E2E_SCENARIOS.json"
        value = json.loads(policy.read_text("utf-8"))
        value["scenarios"].pop()
        policy.write_text(json.dumps(value), encoding="utf-8")
    elif change == "reduced-assertion-policy":
        policy = root / "docs/PRODUCT_E2E_SCENARIOS.json"
        value = json.loads(policy.read_text("utf-8"))
        scenario = value["scenarios"][0]
        removed = scenario["assertions"].pop()
        policy.write_text(json.dumps(value), encoding="utf-8")
        for case in report["cases"]:
            if case["id"] == scenario["id"]:
                case["assertions"].pop(removed)
        # Fresh hashes and matching report assertions must not bless reduced coverage.
        report["manifest_sha256"] = gate.sha256(policy)
        report["source_digest"] = gate.source_digest(root)
    expected_error = (
        "Mandatory assertion coverage changed" if change == "reduced-assertion-policy" else None
    )
    with pytest.raises(gate.GateError, match=expected_error):
        verify(accepted)


def test_reduced_fault_policy_cannot_pass_with_matching_report(monkeypatch, accepted):
    reduced = deepcopy(gate.FAULT_VARIANTS)
    removed = reduced["JOURNEY-02"].pop()
    monkeypatch.setattr(gate, "FAULT_VARIANTS", reduced)
    for case in accepted[3]["cases"]:
        if case["id"] == "JOURNEY-02" and case["mode"] == "fault":
            case["fault_variants"].pop(removed)
    with pytest.raises(gate.GateError, match="Mandatory assertion coverage changed"):
        verify(accepted)


def test_missing_report_fails_closed(accepted):
    accepted[1].unlink()
    with pytest.raises(gate.GateError):
        gate.verify(accepted[0], accepted[1], accepted[2], "windows", "0.1.24", accepted[4])


def test_mac_needs_actual_cross_platform_cases():
    manifest = gate.load_manifest(REPO)
    windows = gate.required_cases(manifest, "windows")
    mac = gate.required_cases(manifest, "macos")
    assert len(windows) == 12 and len(mac) == 14
    assert ("XPLAT-01", "live") not in windows and ("XPLAT-01", "live") in mac


def test_live_and_fault_split_preserves_every_resilience_assertion(accepted):
    row = next(item for item in gate.load_manifest(accepted[0])["scenarios"] if item["id"] == "JOURNEY-06")
    groups = row["assertions_by_mode"]
    assert set(groups["live"]) | set(groups["fault"]) == set(row["assertions"])
    for case in accepted[3]["cases"]:
        if case["id"] == row["id"]:
            case["assertions"] = {key: key in groups[case["mode"]] for key in row["assertions"]}
    assert verify(accepted)["passed_cases"] == 12
    faulty = next(case for case in accepted[3]["cases"] if case["id"] == row["id"] and case["mode"] == "fault")
    faulty["assertions"][groups["fault"][0]] = False
    with pytest.raises(gate.GateError, match="Failed mandatory mode assertion"):
        verify(accepted)


def test_generated_python_runtime_is_not_a_source_input(accepted):
    root = accepted[0]
    previous = gate.source_digest(root)
    generated = root / "desktop/src-tauri/resources/runtime/lib/installed_package.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated-bundled-library", encoding="utf-8")
    assert gate.source_digest(root) == previous
    build = root / "desktop/src-tauri/src/main.rs"
    build.parent.mkdir()
    build.write_text("changed-native-launcher", encoding="utf-8")
    assert gate.source_digest(root) != previous


@pytest.mark.parametrize('name', ['PRODUCT_INCREMENTAL_SYNC_ACCEPTANCE.md', 'WINDOWS_E2E_GOAL_PROMPT.md'])
def test_incremental_user_policy_change_invalidates_original_source_binding(accepted, name):
    root = accepted[0]
    policy = root / 'docs' / name
    policy.write_text('Explicit user requirements before change', 'utf8')
    previous = gate.source_digest(root)
    policy.write_text('Explicit user requirements after change', 'utf8')
    assert gate.source_digest(root) != previous
    with pytest.raises(gate.GateError, match='Source/dependencies changed'):
        verify(accepted)


def test_missing_native_driver_collects_entire_suite_and_blocks(accepted):
    root, _, artifact, _, _ = accepted
    scripts = root / "scripts"
    scripts.mkdir()
    for name in ("product_e2e_gate.py", "run_product_e2e.py"):
        (scripts / name).write_bytes((REPO / "scripts" / name).read_bytes())
    config = root.parent / "private-config.json"
    config.write_text("{}", encoding="utf-8")
    output = root.parent / "native-run"
    result = subprocess.run([sys.executable, str(scripts / "run_product_e2e.py"),
                             "--target", "windows", "--version", "0.1.24",
                             "--artifact", str(artifact), "--installed-path", "test-path",
                             "--config", str(config), "--output-dir", str(output)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    report = json.loads((output / "report.json").read_text("utf-8"))
    assert len(report["cases"]) == 12
    assert all(case["outcome"] == "failed" and
               case["blocker"] == "native_driver_or_private_config_missing"
               for case in report["cases"])
    assert "PRODUCT RELEASE BLOCKED" in result.stderr
