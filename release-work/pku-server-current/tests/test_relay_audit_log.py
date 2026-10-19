"""Relay audit logging for transient OSS temp objects (INFO, stdout).

Window A's deletion proof (VAL-DEDUP-030, "head_object on the logged
keys") runs with a least-privilege OSS probe key that has no
ListObjects grant: the probe learns each transient pku-temp object key
from the relay's own stdout log (--relay-log key-diff) and
head_object-checks it. This additive suite pins that audit surface
without any vendor spend:

  * a miss logs one INFO upload line and one INFO delete-success line,
    both carrying the exact temp key of the OSS object actually used
    (uuid + extension only -- never the original filename);
  * an upstream failure on a miss still logs upload + delete (the raw
    audio is deleted on every path);
  * a dedup hit emits no upload line at all -- the absence is the
    hit-leaves-no-key evidence;
  * the production factory wires the audit logger to stdout at INFO by
    default (the native uvicorn start path), and
  * no captured log record ever carries a filename, credential, token,
    account label, or signed-URL substring (honeypot-tested).
"""

from __future__ import annotations

import logging
import re
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server import accounts, relay
from server.asr import ASR_AUDIT_LOGGER, BailianFileAsr, OssTempStore

# Deliberate decoy strings, same form as the OAuth honeypots in
# test_relay_oauth_exchange.py; none is a real secret. Every captured log
# record is scanned for all of them.
HONEYPOT_FILENAME = "lecture-topsecret-7f3e9a1b.m4a"
HONEYPOT_FILENAME_BARE = "lecture-topsecret-7f3e9a1b"
HONEYPOT_STRING_ONE = "JUST_A_DECOY_STRING_9f8e7d6c5b4a"
HONEYPOT_ACCOUNT_LABEL = "同学A"
SIGNED_URL_HOST = "oss.example"
SIGNED_URL_MARKER = "signature="
HTTP_PREFIX = "https://"

# The probe's OSS_KEY_PATTERN shape (scripts/e2e_window_a.py): exactly the
# temp key, uuid + lowercased extension only.
TEMP_KEY_PATTERN = re.compile(r"pku-temp/\d{4}/\d{2}/\d{2}/[0-9a-f]{32}\.[a-z0-9]+")


def make_audit_client(tmp_path, streaming_asr_fn=None, asr_fn=None):
    """One relay app on a fresh scratch DB with two provisioned accounts."""
    conn = accounts.connect(tmp_path / "accounts.db")
    tokens = {}
    for label, quota in (("同学A", 600), ("同学B", 600)):
        account_id = accounts.create_account(conn, label)
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
            (quota, account_id),
        )
        conn.commit()
        tokens[label] = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db", asr_fn=asr_fn, streaming_asr_fn=streaming_asr_fn
    )
    return TestClient(app), tokens["同学A"], tokens["同学B"]


def upload(
    client, token=None, audio=b"fake-audio", filename="a.m4a", content_type="audio/mp4"
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/v1/transcribe",
        headers=headers,
        files={"audio": (filename, audio, content_type)},
    )


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, key, source):
        self.objects[key] = b"".join(iter(lambda: source.read(3), b""))
        return SimpleNamespace(status=200)

    def sign_url(self, method, key, expires, slash_safe):
        assert method == "GET" and expires == 1800 and slash_safe
        return f"https://{SIGNED_URL_HOST}/{key}?{SIGNED_URL_MARKER}short-lived"

    def delete_object(self, key):
        self.deleted.append(key)


class FakeTaskApi:
    """DashScope task API stand-in counting submits; can fail the first N."""

    def __init__(self, fail_first_n=0):
        self.submits = 0
        self.fail_first_n = fail_first_n

    def async_call(self, **kwargs):
        self.submits += 1
        if self.submits <= self.fail_first_n:
            return SimpleNamespace(status_code=400, message="bad request")
        return SimpleNamespace(status_code=200, message="", output={"task_id": "task"})

    def wait(self, submitted, **kwargs):
        assert submitted.output["task_id"] == "task"
        assert kwargs["wait_timeout"] == 1800
        return SimpleNamespace(
            status_code=200,
            message="",
            output={"results": [{"transcription_url": f"https://{SIGNED_URL_HOST}/result"}]},
        )


def oss_vendor(bucket, fail_first_n=0, api_key="not-a-real-key"):
    """The production streaming vendor shape over a FakeBucket (no network)."""
    api = FakeTaskApi(fail_first_n=fail_first_n)
    vendor = BailianFileAsr(
        OssTempStore(bucket),
        api_key,
        transcription_api=api,
        download_json=lambda _: {
            "properties": {"language": "zh"},
            "transcripts": [
                {"sentences": [{"begin_time": 0, "end_time": 1200, "text": "第一句"}]}
            ],
        },
    )
    return vendor, api


def asr_records(caplog):
    return [record for record in caplog.records if record.name == ASR_AUDIT_LOGGER]


def seeds_hit(message, *seeds):
    return [seed for seed in seeds if seed in message]


# --- miss: one upload line + one delete line, exact key used ---


def test_miss_path_logs_upload_and_delete_lines_with_the_actual_key(tmp_path, caplog):
    bucket = FakeBucket()
    vendor, _ = oss_vendor(bucket)
    client, token_a, _ = make_audit_client(tmp_path, streaming_asr_fn=vendor)

    with caplog.at_level(logging.INFO):
        response = upload(client, token_a, audio=b"audit-miss-bytes")

    assert response.status_code == 200
    assert response.json()["reused"] is False
    key = next(iter(bucket.objects))  # the OSS object actually used
    assert bucket.deleted == [key]
    assert TEMP_KEY_PATTERN.fullmatch(key), key
    assert [(record.levelname, record.getMessage()) for record in asr_records(caplog)] == [
        ("INFO", f"oss upload: {key}"),
        ("INFO", f"oss delete: {key}"),
    ]


# --- failure path: raw audio still deleted, so the delete line still lands ---


def test_upstream_failure_still_logs_upload_and_delete_lines(tmp_path, caplog):
    bucket = FakeBucket()
    vendor, _ = oss_vendor(bucket, fail_first_n=1)
    client, token_a, _ = make_audit_client(tmp_path, streaming_asr_fn=vendor)

    with caplog.at_level(logging.INFO):
        response = upload(client, token_a, audio=b"audit-fail-bytes")

    assert response.status_code == 502
    key = next(iter(bucket.objects))
    assert bucket.deleted == [key]
    assert [(record.levelname, record.getMessage()) for record in asr_records(caplog)] == [
        ("INFO", f"oss upload: {key}"),
        ("INFO", f"oss delete: {key}"),
    ]


# --- hit: spool-first zero OSS work, no upload line ---


def test_hit_path_emits_no_upload_or_delete_line(tmp_path, caplog):
    bucket = FakeBucket()
    vendor, _ = oss_vendor(bucket)
    client, token_a, token_b = make_audit_client(tmp_path, streaming_asr_fn=vendor)

    with caplog.at_level(logging.INFO):
        miss = upload(client, token_a, audio=b"audit-hit-bytes")
        assert miss.status_code == 200 and miss.json()["reused"] is False
        caplog.clear()
        hit = upload(client, token_b, audio=b"audit-hit-bytes")
        assert hit.status_code == 200 and hit.json()["reused"] is True

    assert asr_records(caplog) == []  # the hit left no OSS audit trace at all
    assert len(bucket.objects) == 1  # spool-first: the hit uploaded nothing new


# --- honeypot scan: no filename / credential / account / signed-URL material ---


def test_log_records_never_carry_filenames_credentials_accounts_or_signed_urls(
    tmp_path, caplog
):
    bucket = FakeBucket()
    # Prod-shaped vendor carrying a decoy secret under a honeypot original
    # filename; the second upload uses a different name to prove the audit
    # surface is filename-independent as well.
    vendor, _ = oss_vendor(bucket, api_key=HONEYPOT_STRING_ONE)
    client, token_a, token_b = make_audit_client(tmp_path, streaming_asr_fn=vendor)

    with caplog.at_level(logging.INFO):
        miss = upload(client, token_a, audio=b"audit-honeypot", filename=HONEYPOT_FILENAME)
        hit = upload(client, token_b, audio=b"audit-honeypot", filename="other-name.wav")
        assert miss.status_code == 200 and hit.status_code == 200

    assert asr_records(caplog), "expected the upload + delete audit records"
    for record in caplog.records:
        message = record.getMessage()
        assert seeds_hit(
            message,
            HONEYPOT_FILENAME,
            HONEYPOT_FILENAME_BARE,
            "topsecret",
            "7f3e9a1b",
            HONEYPOT_STRING_ONE,
            HONEYPOT_ACCOUNT_LABEL,
            SIGNED_URL_HOST,
            SIGNED_URL_MARKER,
            HTTP_PREFIX,
        ) == [], message


# --- production start path: audit logger wired to stdout at INFO by default ---


def test_production_factory_wires_audit_logger_to_stdout_by_default(capsys):
    from server.main import app

    relay_app = app()  # native start path (uvicorn --factory server.main:app)
    assert relay_app is not None
    logger = logging.getLogger(ASR_AUDIT_LOGGER)
    try:
        assert logger.level <= logging.INFO, "audit logger must pass INFO"
        assert any(
            isinstance(handler, logging.StreamHandler)
            and handler.stream is sys.stdout
            for handler in logger.handlers
        ), "production factory must attach an INFO stdout handler"
        logger.info("oss upload: pku-temp/2099/01/02/feedface0000000000000000000000ff.m4a")
        captured = capsys.readouterr()
        assert (
            "oss upload: pku-temp/2099/01/02/feedface0000000000000000000000ff.m4a"
            in captured.out
        ), "an INFO audit record must surface on the relay stdout"
    finally:
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
