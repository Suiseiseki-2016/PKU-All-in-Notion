from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pku_sync.e2e_window_d import (
    ANSWER_FIXTURE,
    QUESTION_FIXTURE,
    WindowDReadOnlyVerifier,
    build_dry_run,
    run_dry_run,
    write_dry_run_pack,
)


def test_fixture_is_deterministic_and_covers_the_adopted_five_types():
    assert [item["question_id"] for item in QUESTION_FIXTURE] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert [item["type"] for item in QUESTION_FIXTURE] == ["选择", "判断", "填空", "简答", "论述"]
    assert [item["expected_score"] for item in QUESTION_FIXTURE] == [2, 0, 1, 2, 0]
    assert [item["register_wrong"] for item in QUESTION_FIXTURE] == [False, True, False, False, True]
    assert tuple(ANSWER_FIXTURE) == ("Q1", "Q2", "Q3", "Q4", "Q5")
    assert all(ANSWER_FIXTURE[key] for key in ANSWER_FIXTURE)


def test_driver_uses_panel_routes_and_reruns_are_zero_spend_noops(tmp_path):
    harness = build_dry_run(tmp_path / "scratch")
    try:
        outcome = harness.driver.run()
    finally:
        cleanup = harness.cleanup(harness.driver.created_page_id)
    assert cleanup["post_cleanup_fetch"] == "not_found"
    assert cleanup["scratch_root_removed"] is True
    assert outcome["organize"]["status"] == "completed"
    assert outcome["organize"]["title"].startswith("[E2E]")
    assert outcome["grade"]["status"] == "completed"
    assert outcome["grade"]["score"] == 50
    assert outcome["organize_rerun"]["reused"] is True
    assert outcome["organize_rerun"]["points_charged"] == 0
    assert outcome["grade_rerun"]["points_charged"] == 0
    assert [request["path"] for request in outcome["panel_requests"]] == [
        "/api/exercises/organize/estimate",
        "/api/exercises/organize",
        "/api/exercises/organize/status",
        "/api/exercises/{exercise_id}/grade",
        "/api/exercises/grade/status",
        "/api/exercises/organize/estimate",
        "/api/exercises/organize",
        "/api/exercises/organize/status",
        "/api/exercises/{exercise_id}/grade",
        "/api/exercises/{exercise_id}/grade",
        "/api/exercises/grade/status",
    ]
    snapshot = harness.notion.pages[outcome["organize"]["exercise_id"]]
    assert snapshot["deleted"] is True
    assert snapshot["result_block_sets"] == 1
    assert sorted(harness.notion.wrong_answers) == ["Q2", "Q5"]
    assert [row["operation"] for row in harness.relay.read_ledger()] == ["quiz", "grade"]


def test_read_only_verifier_checks_full_state_without_mutating(tmp_path):
    harness = build_dry_run(tmp_path / "scratch")
    try:
        outcome = harness.driver.run()
        snapshot_before = harness.notion.read_snapshot(outcome["organize"]["exercise_id"])
        mutations_before = harness.notion.mutation_count
        report = WindowDReadOnlyVerifier().verify(
            snapshot=snapshot_before,
            panel_summary=outcome["grade"],
            organize_result=outcome["organize"],
            organize_rerun=outcome["organize_rerun"],
            grade_rerun=outcome["grade_rerun"],
            ledger_rows=harness.relay.read_ledger(),
            expected_parent_id=outcome["course_id"],
        )
        assert report["passed"] is True
        assert all(check["result"] == "pass" for check in report["checks"])
        assert harness.notion.mutation_count == mutations_before
        assert harness.notion.read_snapshot(outcome["organize"]["exercise_id"]) == snapshot_before
    finally:
        cleanup = harness.cleanup(harness.driver.created_page_id)
    assert cleanup["post_cleanup_fetch"] == "not_found"
    assert cleanup["scratch_root_removed"] is True


def test_run_writes_documented_evidence_and_proves_cleanup(tmp_path):
    output = tmp_path / "window-d-dry-run.json"
    evidence = run_dry_run(output=output, scratch_root=tmp_path / "scratch")
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8-sig")) == evidence
    assert evidence["schema_version"] == 1
    assert evidence["mode"] == "dry-run-fakes-only"
    assert evidence["outcome"] == "passed"
    assert evidence["fixture"]["question_ids"] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert evidence["page"]["title"].startswith("[E2E]")
    assert evidence["page"]["cleanup_status"] == "deleted"
    assert evidence["page"]["post_cleanup_fetch"] == "not_found"
    assert [row["operation"] for row in evidence["ledger_rows"]] == ["quiz", "grade"]
    assert evidence["ledger_reconciliation"] == {
        "expected_llm_ops": 2,
        "actual_llm_ops": 2,
        "rerun_llm_ops": 0,
        "matched": True,
    }
    assert evidence["cleanup"]["scratch_root_removed"] is True
    assert not Path(evidence["cleanup"]["scratch_root"]).exists()
    assert all(check["result"] == "pass" for check in evidence["checks"])
    serialized = output.read_text(encoding="utf-8-sig").lower()
    assert "authorization:" not in serialized
    assert "notion_token" not in serialized
    assert "platform_token" not in serialized


def test_cli_runs_fake_only_probe_and_reports_cleanup(tmp_path):
    output = tmp_path / "cli-window-d.json"
    scratch_root = tmp_path / "cli-scratch"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e_window_d_panel.py",
            "--output",
            str(output),
            "--scratch-root",
            str(scratch_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)
    assert summary["outcome"] == "passed"
    assert summary["mode"] == "dry-run-fakes-only"
    assert summary["destination"] == str(output)
    assert summary["checks_passed"] == len(
        json.loads(output.read_text(encoding="utf-8"))["checks"]
    )
    assert summary["ledger_rows"] == 2
    assert summary["cleanup_complete"] is True
    assert not scratch_root.exists()


def test_pack_writer_emits_harness_schema_without_real_spend(tmp_path):
    pack_dir = tmp_path / "window-d-fixture-pack"
    result = write_dry_run_pack(
        pack_dir=pack_dir,
        scratch_root=tmp_path / "pack-scratch",
    )
    scenario = result["scenario"]
    ledger = result["ledger"]
    assert scenario["status"] == "passed"
    assert scenario["window"] == "D"
    assert scenario["settlement"]["llm_ops"] == 0
    assert scenario["cleanup_status"] == "complete"
    assert scenario["notion_pages"][0]["cleanup_status"] == "deleted"
    assert scenario["notion_pages"][0]["post_cleanup_fetch"]["result"] == "not_found"
    assert ledger["ceiling"]["llm_ops"] == 2
    assert ledger["totals"]["llm_ops"] == 0
    assert (pack_dir / "window-ledger.json").exists()
    assert (pack_dir / "scenarios" / "WINDOW-D-PANEL-TOOLING-DRY-RUN.json").exists()
    assert (pack_dir / "evidence" / "window-d-dry-run.json").exists()


def test_build_rejects_nonempty_unowned_scratch_root(tmp_path):
    scratch_root = tmp_path / "unowned"
    scratch_root.mkdir()
    sentinel = scratch_root / "keep.txt"
    sentinel.write_text("user-owned", encoding="utf-8")
    try:
        build_dry_run(scratch_root)
    except ValueError as exc:
        assert "absent or empty" in str(exc)
    else:
        raise AssertionError("non-empty scratch root should be rejected")
    assert sentinel.read_text(encoding="utf-8") == "user-owned"


def test_required_automate_regression_probe_uses_current_service_seam():
    # UTF-8 end to end: the child emits CJK/arrow copy and CI runners may run
    # a non-UTF-8 default codepage, where a strict locale decode kills the
    # capture reader thread and leaves stdout None (recorded on
    # windows-latest, 2026-09-23). PYTHONUTF8=1 pins the child's output
    # encoding and the explicit utf-8/replace decode makes the ASCII sentinel
    # assertion deterministic on every runner.
    result = subprocess.run(
        [sys.executable, "scripts/e2e_automate_workflow.py"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert "E2E PASS: automate ran daily" in result.stdout
