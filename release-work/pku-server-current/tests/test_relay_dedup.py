"""Relay dedup integration: /v1/transcribe through the reuse store.

Pins the integrated flow (fake vendors with call counters, the real
BailianFileAsr adapter over a FakeBucket, ephemeral SQLite under
tmp_path, no network): the pinned gate order auth -> 402 -> 503 ->
lookup -> upstream, spool-first hits that never touch OSS or the
vendor, hit/miss billing on every surface, exact-fingerprint isolation,
filename independence, and the preserved OSS-deletion and 502 failure
semantics. Requests here are sequential; genuinely overlapping
requests belong to the concurrency feature.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server import accounts, relay, transcript_store
from server.asr import AsrError, BailianFileAsr, OssTempStore


# --- fixtures: two accounts, content-keyed fake vendors, OSS harness ---


def make_dedup_client(
    tmp_path, streaming_asr_fn=None, asr_fn=None, quota_a=600, quota_b=600
):
    """One app on a fresh DB with two provisioned accounts (A and B)."""
    conn = accounts.connect(tmp_path / "accounts.db")
    tokens = {}
    for label, quota in (("同学A", quota_a), ("同学B", quota_b)):
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


def quota_of(client, token):
    return client.get("/v1/quota", headers={"Authorization": f"Bearer {token}"}).json()


def reuse_rows(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    found = conn.execute("SELECT * FROM transcript_reuse").fetchall()
    conn.close()
    return found


def usage_rows(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    found = conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
    conn.close()
    return found


def account_id_of(tmp_path, label):
    conn = accounts.connect(tmp_path / "accounts.db")
    found = conn.execute("SELECT id FROM accounts WHERE label = ?", (label,)).fetchone()
    conn.close()
    return found["id"]


def strip_reuse_fields(body):
    return {k: v for k, v in body.items() if k not in ("reused", "seconds_charged")}


class FakeVendor:
    """Streaming fake keyed on the exact bytes, with a call counter."""

    def __init__(self, durations=None, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first
        self.durations = dict(durations or {})

    def __call__(self, source, filename):
        self.calls += 1
        if self.fail_first:
            self.fail_first = False
            raise AsrError("vendor down")
        data = source.read()
        return {
            "language": "zh",
            "duration_seconds": self.durations.get(data, 120),
            "segments": [{"start": 0.0, "end": 1.5, "text": f"transcript:{data!r}"}],
        }


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, key, source):
        self.objects[key] = b"".join(iter(lambda: source.read(3), b""))
        return SimpleNamespace(status=200)

    def sign_url(self, method, key, expires, slash_safe):
        assert method == "GET" and expires == 1800 and slash_safe
        return f"https://oss.example/{key}?signature=short-lived"

    def delete_object(self, key):
        self.deleted.append(key)


class FakeTaskApi:
    """Fake DashScope task API counting submits; can fail the first N."""

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
            output={"results": [{"transcription_url": "https://vendor.example/result"}]},
        )


def oss_vendor(bucket, fail_first_n=0):
    """The production streaming vendor shape over a FakeBucket, no network.

    The fake download yields one 1.2s sentence, so the metering payload
    carries ``duration_seconds == 2`` (ceil, as in server/asr.py).
    """
    api = FakeTaskApi(fail_first_n=fail_first_n)
    vendor = BailianFileAsr(
        OssTempStore(bucket),
        "not-a-real-key",
        transcription_api=api,
        download_json=lambda _: {
            "properties": {"language": "zh"},
            "transcripts": [
                {"sentences": [{"begin_time": 0, "end_time": 1200, "text": "第一句"}]}
            ],
        },
    )
    return vendor, api


# --- VAL-DEDUP-010: exact hit, key-for-key, zero extra upstream calls ---


def test_exact_hit_returns_stored_transcript_key_for_key_zero_extra_calls(tmp_path):
    vendor = FakeVendor()
    client, token_a, _ = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    first = upload(client, token_a, audio=b"same-audio-twice")
    assert first.status_code == 200
    second = upload(client, token_a, audio=b"same-audio-twice")
    assert second.status_code == 200
    assert vendor.calls == 1  # the hit performed zero upstream work

    miss, hit = first.json(), second.json()
    assert "duration_seconds" not in miss and "duration_seconds" not in hit
    assert miss["reused"] is False and hit["reused"] is True
    assert miss["seconds_charged"] == 120 and hit["seconds_charged"] == 0
    assert strip_reuse_fields(hit) == strip_reuse_fields(miss)  # key-for-key
    assert hit["segments"] == miss["segments"]


# --- VAL-DEDUP-011: a hit charges nothing, on every billing surface ---


def test_hit_charges_nothing_on_every_billing_surface(tmp_path):
    vendor = FakeVendor()
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    a_before = quota_of(client, token_a)
    b_before = quota_of(client, token_b)
    assert a_before["label"] == "同学A"
    assert a_before["transcribe_seconds_remaining"] == 600
    assert b_before["label"] == "同学B"
    assert b_before["transcribe_seconds_remaining"] == 600

    assert upload(client, token_a, audio=b"hit-billing").status_code == 200  # miss
    assert upload(client, token_b, audio=b"hit-billing").status_code == 200  # hit

    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 480
    assert quota_of(client, token_b) == b_before  # the hit changed nothing for B

    found = usage_rows(tmp_path)
    assert len(found) == 1  # exactly one transcribe usage row overall
    assert found[0]["kind"] == "transcribe"
    assert found[0]["seconds"] == 120
    assert found[0]["account_id"] == account_id_of(tmp_path, "同学A")


# --- VAL-DEDUP-012: a miss charges the ceil'd seconds exactly once ---


def test_miss_charges_ceil_seconds_once_and_distinct_fingerprints_separately(tmp_path):
    vendor = FakeVendor(durations={b"audio-one": 119.5, b"audio-two": 61})
    client, token_a, _ = make_dedup_client(tmp_path, streaming_asr_fn=vendor)

    first = upload(client, token_a, audio=b"audio-one")
    assert first.status_code == 200
    assert first.json()["seconds_charged"] == 120  # ceil(119.5)
    second = upload(client, token_a, audio=b"audio-two")
    assert second.status_code == 200
    assert second.json()["seconds_charged"] == 61

    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 600 - 120 - 61
    found = usage_rows(tmp_path)
    assert [row["seconds"] for row in found] == [120, 61]  # one row per miss
    assert len(reuse_rows(tmp_path)) == 2


# --- VAL-DEDUP-013: a one-byte difference is its own fresh miss ---


def test_audio_differing_by_one_byte_is_own_miss_never_others_payload(tmp_path):
    vendor = FakeVendor()
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    original = b"lecture-audio-exact"
    altered = b"lecture-audio-exacf"  # one byte differs at the end

    first = upload(client, token_a, audio=original)
    second = upload(client, token_b, audio=altered)
    assert first.status_code == 200 and second.status_code == 200
    assert vendor.calls == 2  # its own fresh upstream transcription

    assert second.json()["reused"] is False
    assert second.json()["segments"] != first.json()["segments"]  # never A's payload
    assert second.json()["segments"] == [
        {"start": 0.0, "end": 1.5, "text": f"transcript:{altered!r}"}
    ]
    # each account is charged for its own work
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 480
    assert quota_of(client, token_b)["transcribe_seconds_remaining"] == 480
    assert len(reuse_rows(tmp_path)) == 2


# --- VAL-DEDUP-014: cross-account exact hit, one total vendor call ---


def test_cross_account_exact_hit_is_identical_with_one_total_vendor_call(tmp_path):
    vendor = FakeVendor()
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    first = upload(client, token_a, audio=b"cross-account-audio")
    second = upload(client, token_b, audio=b"cross-account-audio")
    assert first.status_code == 200 and second.status_code == 200

    assert vendor.calls == 1  # one total across both requests
    assert first.json()["segments"] == second.json()["segments"]
    assert (
        json.dumps(second.json()["segments"], ensure_ascii=False)
        == json.dumps(first.json()["segments"], ensure_ascii=False)
    )  # byte-equal segments
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 480
    assert quota_of(client, token_b)["transcribe_seconds_remaining"] == 600


# --- VAL-DEDUP-015: filename and content type never affect the flow ---


def test_different_filename_and_content_type_hit_without_trace_in_the_row(tmp_path):
    vendor = FakeVendor()
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    audio = b"filename-independent-bytes"
    first = upload(
        client, token_a, audio=audio, filename="a.m4a", content_type="audio/mp4"
    )
    second = upload(
        client,
        token_b,
        audio=audio,
        filename="completely-different-name.wav",
        content_type="audio/wav",
    )
    assert first.status_code == 200 and second.status_code == 200

    assert first.json()["reused"] is False
    assert second.json()["reused"] is True  # the digest only saw the bytes
    assert vendor.calls == 1  # hit: no upstream call
    assert second.json()["seconds_charged"] == 0
    assert quota_of(client, token_b)["transcribe_seconds_remaining"] == 600  # no charge

    row = reuse_rows(tmp_path)[0]
    dump = json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False)
    assert "a.m4a" not in dump
    assert "completely-different-name.wav" not in dump
    assert ".m4a" not in dump and ".wav" not in dump  # no filename trace at all


# --- VAL-DEDUP-021: gate order auth -> 402 -> 503 -> lookup -> upstream ---


def test_gate_order_auth_quota_vendor_then_lookup_with_seeded_row(tmp_path):
    audio = b"gate-matrix-audio"
    conn = accounts.connect(tmp_path / "accounts.db")
    a_id = accounts.create_account(conn, "同学A")
    b_id = accounts.create_account(conn, "同学B")  # stays quota-exhausted
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = 600 WHERE id = ?",
        (a_id,),
    )
    conn.commit()
    token_a = accounts.issue_session(conn, a_id)
    token_b = accounts.issue_session(conn, b_id)
    stored_payload = {
        "language": "zh",
        "duration_seconds": 120,
        "segments": [{"start": 0.0, "end": 1.5, "text": "已存储"}],
    }
    # pre-seed: the fingerprint is ALREADY stored
    transcript_store.store_transcript(
        conn, transcript_store.fingerprint_of(audio), stored_payload
    )
    conn.close()

    # unconfigured vendor + stored fingerprint: 503, never a hit
    bare_client = TestClient(relay.create_relay_app(tmp_path / "accounts.db"))
    assert upload(bare_client, token_a, audio=audio).status_code == 503

    vendor = FakeVendor()
    client = TestClient(
        relay.create_relay_app(tmp_path / "accounts.db", streaming_asr_fn=vendor)
    )
    # auth gates the hit path
    assert upload(client, None, audio=audio).status_code == 401
    # quota-exhausted account: no free hits
    assert upload(client, token_b, audio=audio).status_code == 402
    # configured vendor + stored fingerprint: hit 200, zero vendor calls
    hit = upload(client, token_a, audio=audio)
    assert hit.status_code == 200
    assert hit.json()["reused"] is True
    assert hit.json()["seconds_charged"] == 0
    assert hit.json()["segments"] == stored_payload["segments"]
    assert vendor.calls == 0
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 600


# --- VAL-DEDUP-022: explicit reuse/settlement fields, client contract ---


def test_both_responses_carry_segments_and_explicit_reuse_fields(tmp_path):
    vendor = FakeVendor(durations={b"resp-fields-audio": 33})
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    miss = upload(client, token_a, audio=b"resp-fields-audio")
    hit = upload(client, token_b, audio=b"resp-fields-audio")

    for response in (miss, hit):
        assert response.status_code == 200
        body = response.json()
        # the existing client acceptance check (pku_sync/transcription.py)
        assert isinstance(body, dict) and "segments" in body
        assert "duration_seconds" not in body
        assert body["language"] == "zh"

    miss_body, hit_body = miss.json(), hit.json()
    assert miss_body["reused"] is False and miss_body["seconds_charged"] == 33
    assert hit_body["reused"] is True and hit_body["seconds_charged"] == 0
    differing = {
        key
        for key in set(miss_body) | set(hit_body)
        if miss_body.get(key) != hit_body.get(key)
    }
    assert differing == {"reused", "seconds_charged"}  # nothing else differs


# --- VAL-DEDUP-023: OSS deletion preserved on success AND failure ---


def test_oss_object_still_deleted_after_success_and_failure_with_dedup(tmp_path):
    bucket = FakeBucket()
    ok_vendor, ok_api = oss_vendor(bucket)
    client, token_a, _ = make_dedup_client(tmp_path, streaming_asr_fn=ok_vendor)
    ok = upload(client, token_a, audio=b"oss-deletion-ok")
    assert ok.status_code == 200
    assert ok.json()["reused"] is False  # a miss: the upload really happened
    assert ok.json()["seconds_charged"] == 2
    assert ok_api.submits == 1
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 598

    # fresh fingerprint through a failing vendor: still deleted, still 502
    failing_vendor, failing_api = oss_vendor(bucket, fail_first_n=1)
    failing_client = TestClient(
        relay.create_relay_app(
            tmp_path / "accounts.db", streaming_asr_fn=failing_vendor
        )
    )
    failed = upload(failing_client, token_a, audio=b"oss-deletion-fail")
    assert failed.status_code == 502
    assert failing_api.submits == 1

    keys = list(bucket.objects)
    assert len(keys) == 2
    assert bucket.deleted == keys  # every temporary object deleted, no residue
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 598  # no charge
    assert len(usage_rows(tmp_path)) == 1  # only the successful miss metered
    # the failed fingerprint stored nothing
    fingerprints = {row["fingerprint_sha256"] for row in reuse_rows(tmp_path)}
    assert transcript_store.fingerprint_of(b"oss-deletion-fail") not in fingerprints
    assert len(fingerprints) == 1


# --- VAL-DEDUP-024: a hit never uploads to OSS and never invokes the vendor ---


def test_hit_never_uploads_to_oss_or_invokes_the_vendor(tmp_path):
    bucket = FakeBucket()
    vendor, api = oss_vendor(bucket)
    client, token_a, token_b = make_dedup_client(tmp_path, streaming_asr_fn=vendor)

    miss = upload(client, token_a, audio=b"spool-first-audio")
    assert miss.status_code == 200
    assert miss.json()["reused"] is False
    assert len(bucket.objects) == 1  # the miss uploaded exactly one object
    assert api.submits == 1

    hit = upload(client, token_b, audio=b"spool-first-audio")
    assert hit.status_code == 200
    assert hit.json()["reused"] is True
    # spool-first: the hit created no OSS key and never invoked the vendor
    assert len(bucket.objects) == 1
    assert api.submits == 1
    assert bucket.deleted == list(bucket.objects)  # the miss's key was deleted


# --- VAL-DEDUP-025: upstream failure is 502, uncharged, unstored ---


def test_upstream_failure_502_uncharged_unstored_oss_deleted_then_misses_again(tmp_path):
    bucket = FakeBucket()
    vendor, api = oss_vendor(bucket, fail_first_n=1)
    client, token_a, _ = make_dedup_client(tmp_path, streaming_asr_fn=vendor)
    audio = b"flaky-upstream-audio"

    failed = upload(client, token_a, audio=audio)
    assert failed.status_code == 502
    assert api.submits == 1
    assert bucket.deleted == list(bucket.objects)  # the OSS object was deleted
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 600
    assert usage_rows(tmp_path) == []
    assert reuse_rows(tmp_path) == []  # no phantom row

    # a later identical request is a miss again: fresh upstream, own charge
    retry = upload(client, token_a, audio=audio)
    assert retry.status_code == 200
    assert retry.json()["reused"] is False
    assert retry.json()["seconds_charged"] == 2  # the fake result is 1.2s long
    assert api.submits == 2
    assert quota_of(client, token_a)["transcribe_seconds_remaining"] == 598
    assert len(usage_rows(tmp_path)) == 1
    assert len(reuse_rows(tmp_path)) == 1
