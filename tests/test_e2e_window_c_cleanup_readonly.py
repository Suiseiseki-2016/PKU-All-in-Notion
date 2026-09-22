"""Window C scratch-cleanup hardening: read-only organize_failures captures.

Local deterministic check for the Windows fix in scripts/e2e_window_c.py: the
runner's scratch teardown must remove a tree that contains an organizer
parse-failure capture (panel/organize_failures/quiz-parse-*.txt, written
chmod 0444 by persist_parse_failure, i.e. FILE_ATTRIBUTE_READONLY on Windows)
while keeping shutil.rmtree ignore_errors=False so unrelated deletion errors
stay fail-loud. No services, credentials, Notion calls, or spend.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SELFTEST_REL_CAPTURE = os.path.normpath(
    "client-data/panel/organize_failures/quiz-parse-selftest-1.txt"
)


def _run_cleanup_selftest(output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "scripts/e2e_window_c.py",
            "cleanup-selftest",
            "--out",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cleanup_selftest_removes_readonly_capture_and_proves_it(tmp_path):
    output = tmp_path / "cleanup-selftest.json"
    result = _run_cleanup_selftest(output)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    evidence = json.loads(output.read_text(encoding="utf-8-sig"))
    assert evidence["step"] == "cleanup-selftest"
    assert evidence["mode"] == "local-deterministic"
    assert all(check["result"] == "pass" for check in evidence["checks"])
    assert evidence["fixture"]["readonly_mode"] is True
    assert evidence["removal"]["root_removed"] is True
    # The read-only capture is either explicitly unlocked (Windows) or simply
    # unlinked (POSIX never blocks unlink on file mode); either way the tree
    # must be gone with an honest proof.
    if os.name == "nt":
        assert SELFTEST_REL_CAPTURE in evidence["removal"]["cleared_readonly_files"]
        fail_loud = evidence["removal"]["unrelated_error_fail_loud"]
        assert fail_loud["raised_permission_error"] is True
    else:
        assert evidence["removal"]["cleared_readonly_files"] == []
    assert evidence["removal"]["removed_entry_count"] >= 1
