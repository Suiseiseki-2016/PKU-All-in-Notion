#!/usr/bin/env python3
"""Isolation E2E for the assignment-submission boundary (text + file).

The scenario exercises the real submit module against a deterministic fake
Blackboard HTTP boundary. It proves dry-run, multipart submission, receipt
parsing and the already-submitted guard without posting to a real course.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Keep the script runnable from a clean checkout before editable installation.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pku_sync import submit


FORM_HTML = """
<form>
  <input type="hidden" name="blackboard.platform.security.NonceUtil.nonce.ajax" value="nonce">
  <input type="hidden" name="studentSubmission.text_f" value="text-f">
  <input type="hidden" name="studentSubmission.text_w" value="text-w">
</form>
"""
ALREADY_SUBMITTED_HTML = "<html>Review Submission History</html>"

SUBMIT_PARAMS = {"content_id": "content-e2e", "course_id": "_course_e2e_"}


class FakeClient:
    def __init__(self, html: str = FORM_HTML):
        self.html = html
        self.posts: list[dict] = []

    def get(self, path, params):
        assert path == submit.SUBMIT_PATH
        assert params == SUBMIT_PARAMS
        return SimpleNamespace(status_code=200, text=self.html)

    def post(self, path, data, files, headers):
        self.posts.append({"path": path, "data": data, "files": files, "headers": headers})
        return SimpleNamespace(
            status_code=200,
            text='{"destinationUrl": "/webapps/assignment/receipt/e2e"}',
            json=lambda: {"destinationUrl": "/webapps/assignment/receipt/e2e"},
        )


class FileFakeClient(FakeClient):
    """Assignments whose fetch goes through the file-upload (newAttempt) mode."""

    def get(self, path, params):
        assert path == submit.SUBMIT_PATH
        assert params == {**SUBMIT_PARAMS, "action": "newAttempt"}
        return SimpleNamespace(status_code=200, text=self.html)


def main() -> int:
    try:
        dry_run = FakeClient()
        form = submit.fetch_form(dry_run, "_course_e2e_", "content-e2e")
        assert not dry_run.posts, "fetching the form must not POST"
        assert form.fields("draft")["studentSubmission.text"] == "draft"

        live = FakeClient()
        live_form = submit.fetch_form(live, "_course_e2e_", "content-e2e")
        receipt = submit.submit_text(live, live_form, "final E2E answer")
        assert receipt["destinationUrl"].endswith("/e2e")
        assert len(live.posts) == 1
        post = live.posts[0]
        assert post["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert "_force_multipart" in post["files"]
        assert post["data"]["studentSubmission.text"] == "final E2E answer"

        already = FakeClient(ALREADY_SUBMITTED_HTML)
        try:
            submit.fetch_form(already, "_course_e2e_", "content-e2e")
        except submit.AlreadySubmitted:
            pass
        else:
            raise AssertionError("already-submitted page bypassed the guard")
        assert not already.posts

        # File-attachment path: newAttempt fetch (no POST), multipart with the
        # real file part and the FilePicker fields, receipt-or-empty tolerated.
        answer = Path(__file__).with_name("answer_e2e.docx")
        answer.write_bytes(b"PK\x03\x04-e2e-docx")
        try:
            file_dry = FileFakeClient()
            file_form = submit.fetch_form(file_dry, "_course_e2e_", "content-e2e", action="newAttempt")
            assert not file_dry.posts, "fetching the file form must not POST"

            file_live = FileFakeClient()
            live_file_form = submit.fetch_form(file_live, "_course_e2e_", "content-e2e", action="newAttempt")
            file_receipt = submit.submit_file(file_live, live_file_form, answer)
            assert file_receipt["destinationUrl"].endswith("/e2e")
            (fpost,) = file_live.posts
            assert fpost["path"].startswith("/webapps/assignment/uploadAssignment?action=submit")
            assert fpost["data"]["dispatch"] == "submit"
            assert fpost["data"]["newFile_linkTitle"] == "answer_e2e.docx"
            assert fpost["files"]["newFile_LocalFile0"][1] == b"PK\x03\x04-e2e-docx"
            assert fpost["headers"]["X-Requested-With"] == "XMLHttpRequest"
        finally:
            answer.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - preserve scenario failure
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "E2E PASS: assignment text+file dry-run, multipart receipt, "
        "FilePicker fields, and AlreadySubmitted guard without a real POST"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
