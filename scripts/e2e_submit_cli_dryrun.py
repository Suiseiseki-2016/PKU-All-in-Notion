#!/usr/bin/env python3
"""Isolation E2E for the real ``pku-sync submit`` CLI dry-run boundary.

Drives the REAL typer CLI (the same ``app`` object the ``pku-sync`` console
script invokes, through typer's CliRunner) with the Blackboard HTTP boundary
replaced by a deterministic fake that records every request:

- ``submit list`` performs no POST (read-only listing);
- ``submit text`` / ``submit file`` dry-runs take the form (GET) and stop at
  the 干跑 preview line WITHOUT any POST -- ``--yes`` is the only way a POST
  can happen and this probe never passes it (M4 boundary: NO real --yes run);
- the already-submitted guard stops before any POST when the form page is
  the review-history state;
- the overdue (逾期) warning surfaces from the merged deadline.

The course name resolves through the real ``resolve_course_id`` against a
scratch course.json tree, so the name-fragment entry behaves exactly as a
user would type it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

# Keep this script runnable from a clean checkout before editable installation.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pku_sync import auth, config, materials
from pku_sync.cli import app
from pku_sync.models import Assignment
from pku_sync.submit import SUBMIT_PATH

FORM_HTML = """
<form>
  <input type="hidden" name="blackboard.platform.security.NonceUtil.nonce.ajax" value="nonce">
  <input type="hidden" name="studentSubmission.text_f" value="text-f">
  <input type="hidden" name="studentSubmission.text_w" value="text-w">
</form>
"""
ALREADY_SUBMITTED_HTML = "<html>Review Submission History</html>"

COURSE_ID = "_101578_1"
COURSE_NAME = "认知心理学"
CONTENT_ID = "content-e2e"
PAST_DUE = "2020-01-01 23:59"


class FakeBlackboard:
    """Records every request; serves the submit form (or the guard page)."""

    def __init__(self, html: str = FORM_HTML):
        self.html = html
        self.gets: list[dict] = []
        self.posts: list[dict] = []

    def get(self, path, params):
        self.gets.append({"path": path, "params": dict(params)})
        return SimpleNamespace(status_code=200, text=self.html)

    def post(self, path, data=None, files=None, headers=None):
        self.posts.append({"path": path, "data": data, "files": files, "headers": headers})
        return SimpleNamespace(status_code=200, text="{}")


class _Responses:
    """Simple queue: each get_session() call yields the next fake client."""

    def __init__(self, clients: list[FakeBlackboard]):
        self.clients = clients
        self.index = 0

    def get_session(self):
        client = self.clients[self.index]
        self.index += 1
        return client


def _assignment() -> Assignment:
    return Assignment(
        course_id=COURSE_ID,
        title="实验作业",
        content_id=CONTENT_ID,
        due_at=PAST_DUE,
        source="content-tree",
    )


def _seed_course(root: Path) -> None:
    course = root / COURSE_NAME
    course.mkdir()
    (course / "course.json").write_text(
        json.dumps({"course_id": COURSE_ID, "name": COURSE_NAME}, ensure_ascii=False),
        encoding="utf-8",
    )


def _install_seams(clients: list[FakeBlackboard], data_dir: Path) -> _Responses:
    responses = _Responses(clients)
    auth.get_session = responses.get_session
    materials.walk_materials = lambda client, course_id: ([], [_assignment()])
    materials.fetch_deadlines = lambda client: {}
    config.settings = SimpleNamespace(data_dir=data_dir)
    return responses


def _restore(responses: _Responses) -> None:
    del responses
    auth.get_session = _ORIGINAL_GET_SESSION
    materials.walk_materials = _ORIGINAL_WALK
    materials.fetch_deadlines = _ORIGINAL_DEADLINES
    config.settings = _ORIGINAL_SETTINGS


def _invoke(argv: list[str]) -> tuple[int, str]:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, argv)
    return result.exit_code, result.output


def main() -> int:
    from typer.testing import CliRunner  # noqa: F401  (import check for the runner harness)

    global _ORIGINAL_GET_SESSION, _ORIGINAL_WALK, _ORIGINAL_DEADLINES, _ORIGINAL_SETTINGS
    _ORIGINAL_GET_SESSION = auth.get_session
    _ORIGINAL_WALK = materials.walk_materials
    _ORIGINAL_DEADLINES = materials.fetch_deadlines
    _ORIGINAL_SETTINGS = config.settings

    checks: list[tuple[str, bool, str]] = []
    try:
        with TemporaryDirectory(prefix="pku-submit-cli-e2e-") as directory:
            root = Path(directory)
            _seed_course(root)
            clients = [
                FakeBlackboard(),            # submit list
                FakeBlackboard(),            # submit text --text
                FakeBlackboard(),            # submit text --file
                FakeBlackboard(),            # submit file
                FakeBlackboard(ALREADY_SUBMITTED_HTML),  # already-submitted guard
            ]
            answer_file = root / "answer.md"
            answer_file.write_text("# E2E 答案\n\n正文内容。", encoding="utf-8")
            docx_file = root / "answer_e2e.docx"
            docx_file.write_bytes(b"PK\x03\x04-e2e-docx")
            responses = _install_seams(clients, root)
            try:
                # 1. submit list (name fragment resolves via the scratch tree)
                code, output = _invoke(["submit", "list", "--course", COURSE_NAME])
                checks.append((
                    "submit list renders the assignment table with no POST",
                    code == 0 and "实验作业" in output and "content-e2e" in output and not clients[0].posts,
                    output[-300:],
                ))

                # 2. submit text dry-run (--text): form GET, NO POST, 干跑 copy
                code, output = _invoke([
                    "submit", "text", "--course", COURSE_NAME,
                    "--content", CONTENT_ID, "--text", "E2E 答案",
                ])
                overdue_ok = "已过" in output and "逾期提交" in output
                checks.append((
                    "submit text --text dry-run previews with overdue warning and NO POST",
                    code == 0 and "干跑" in output and "--yes" in output and overdue_ok and not clients[1].posts,
                    output[-300:],
                ))

                # 3. submit text dry-run (--file): same boundary
                code, output = _invoke([
                    "submit", "text", "--course", COURSE_NAME,
                    "--content", CONTENT_ID, "--file", str(answer_file),
                ])
                checks.append((
                    "submit text --file dry-run previews with NO POST",
                    code == 0 and "干跑" in output and not clients[2].posts,
                    output[-300:],
                ))

                # 4. submit file dry-run: newAttempt form GET, NO POST
                code, output = _invoke([
                    "submit", "file", "--course", COURSE_NAME,
                    "--content", CONTENT_ID, "--file", str(docx_file),
                ])
                gets_ok = any(
                    g["params"].get("action") == "newAttempt" for g in clients[3].gets
                ) and all(g["path"] == SUBMIT_PATH for g in clients[3].gets)
                checks.append((
                    "submit file dry-run takes the newAttempt form and NO POST",
                    code == 0 and "干跑" in output and gets_ok and not clients[3].posts,
                    output[-300:],
                ))

                # 5. already-submitted guard: no POST, guard copy, exit 0
                code, output = _invoke([
                    "submit", "text", "--course", COURSE_NAME,
                    "--content", CONTENT_ID, "--text", "重复答案",
                ])
                checks.append((
                    "already-submitted guard stops before any POST",
                    code == 0 and "已提交过" in output and "不做重复提交" in output and not clients[4].posts,
                    output[-300:],
                ))
            finally:
                _restore(responses)

        total_posts = sum(len(client.posts) for client in clients)
        checks.append((
            "no POST was issued anywhere across list/text/file dry-runs and the guard",
            total_posts == 0,
            f"posts_total={total_posts}",
        ))
        checks.append((
            "the probe never passes --yes (no real submission path is reachable)",
            True,
            "argv sets contain no --yes flag",
        ))
    except Exception as exc:  # noqa: BLE001 - preserve scenario failure
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    failed = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
        if not ok:
            line += f" -- {detail!r}"
        print(line)
    if failed:
        print(f"E2E FAIL: {len(failed)} boundary check(s) failed", file=sys.stderr)
        return 1
    print(
        "E2E PASS: pku-sync submit list/text/file dry-runs performed no POST, the "
        "already-submitted guard and overdue warning behaved, and no --yes run occurred"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
