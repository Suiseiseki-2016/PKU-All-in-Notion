"""Relay dedup under genuinely overlapping requests (shared-loop harness).

Pins the concurrency contract (library/relay-dedup-design.md
"Concurrency"): two overlapping identical misses cost exactly one
upstream call and one charge (VAL-DEDUP-017/018), eight simultaneous
distinct fingerprints proceed independently without SQLite's "database
is locked" (VAL-DEDUP-019, the busy_timeout hardening in
accounts.connect), and an upstream failure is never inherited — the
waiter runs its own attempt and charges only on its own success
(VAL-DEDUP-020).

Harness rule (library/user-testing.md gotcha, binding): every test
here drives REAL overlap through ONE shared event loop — a single
httpx.AsyncClient with ONE ASGITransport for the whole test, all
requests in flight at once via asyncio.gather. Per-thread
ASGITransport instances are forbidden (each binds its own event loop
and breaks the thread-based in-flight lock). Overlap is proven, not
assumed: the fake vendor blocks its first upstream call until the
other request is visibly parked on the per-fingerprint in-flight lock,
so a passing test proves the second request genuinely passed the
auth/quota/vendor gates and the first store lookup while the first
call was still in flight.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import httpx

from server import accounts, relay, transcript_store
from server.asr import AsrError

OVERLAP_TIMEOUT_SECONDS = 10.0
HARNESS_TIMEOUT_SECONDS = 60.0  # a leaked lock fails the test, never hangs it


# --- fixtures: provisioned accounts, one shared-loop upload gather ---


def make_app(tmp_path, streaming_asr_fn=None, provisioned=(("同学A", 600), ("同学B", 600))):
    """A fresh DB + app with provisioned accounts; returns (app, {label: token})."""
    conn = accounts.connect(tmp_path / "accounts.db")
    tokens = {}
    for label, quota in provisioned:
        account_id = accounts.create_account(conn, label)
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
            (quota, account_id),
        )
        conn.commit()
        tokens[label] = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db", streaming_asr_fn=streaming_asr_fn
    )
    return app, tokens


async def _gather_uploads(app, specs):
    """All requests in flight at once on ONE shared event loop.

    One AsyncClient + one ASGITransport for the entire test: the app
    runs directly on this loop, exactly as the binding harness pattern
    requires — no per-thread transports anywhere.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://relay.test"
    ) as client:

        async def upload(spec):
            return await client.post(
                "/v1/transcribe",
                headers={"Authorization": f"Bearer {spec['token']}"},
                files={
                    "audio": (
                        spec.get("filename", "a.m4a"),
                        spec["audio"],
                        "audio/mp4",
                    )
                },
            )

        return list(await asyncio.gather(*(upload(spec) for spec in specs)))


def run_overlapping(app, specs):
    """Drive the specs through the shared loop; bounded so bugs fail, not hang."""
    return asyncio.run(
        asyncio.wait_for(_gather_uploads(app, specs), timeout=HARNESS_TIMEOUT_SECONDS)
    )


def rows(tmp_path, query):
    conn = accounts.connect(tmp_path / "accounts.db")
    found = conn.execute(query).fetchall()
    conn.close()
    return found


def reuse_rows(tmp_path):
    return rows(tmp_path, "SELECT * FROM transcript_reuse")


def usage_rows(tmp_path):
    return rows(tmp_path, "SELECT * FROM usage ORDER BY id")


def account_row(tmp_path, label):
    conn = accounts.connect(tmp_path / "accounts.db")
    found = conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone()
    conn.close()
    return found


def wait_until(predicate, timeout=OVERLAP_TIMEOUT_SECONDS):
    """Bounded polling from a worker thread; False on timeout (never hangs)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


class OverlapVendor:
    """Streaming fake that forces and records genuine overlap.

    The first upstream call blocks until another request is parked on
    the same fingerprint's in-flight lock (or the bounded wait expires),
    so ``overlapped`` proves the requests really overlapped in time —
    and the double-check lookup, not timing luck, decides who runs
    upstream. With ``fail_first`` the first call raises after the
    rendezvous, pinning the waiter fall-through policy.
    """

    def __init__(self, fingerprint, fail_first=False):
        self.fingerprint = fingerprint
        self.fail_first = fail_first
        self.calls = 0
        self.threads = []
        self.overlapped = False
        self.registry = None  # bound after the app exists

    def bind(self, app):
        self.registry = app.state.in_flight

    def __call__(self, source, filename):
        self.calls += 1
        self.threads.append(threading.get_ident())
        if self.calls == 1:
            self.overlapped = wait_until(
                lambda: self.registry.waiting(self.fingerprint) >= 1
            )
            if self.fail_first:
                raise AsrError("vendor down")
        data = source.read()
        return {
            "language": "zh",
            "duration_seconds": 120,
            "segments": [{"start": 0.0, "end": 1.5, "text": f"transcript:{data!r}"}],
        }


class SlowDistinctVendor:
    """Eight distinct fingerprints in flight at once; returns per-content durations."""

    def __init__(self, durations):
        self.durations = dict(durations)
        self.calls = 0
        self.threads = []

    def __call__(self, source, filename):
        self.calls += 1
        self.threads.append(threading.get_ident())
        time.sleep(0.02)  # widen the window: the charges genuinely overlap
        data = source.read()
        return {
            "language": "zh",
            "duration_seconds": self.durations[data],
            "segments": [{"start": 0.0, "end": 1.5, "text": f"transcript:{data!r}"}],
        }


# --- busy_timeout hardening in accounts.connect (VAL-DEDUP-019 prerequisite) ---


def test_connect_enables_busy_timeout_on_every_connection(tmp_path):
    """PRAGMA busy_timeout >= 5000, pinned explicitly in accounts.connect."""
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert busy_timeout >= 5000


def test_parallel_store_writes_from_eight_threads_never_lock_the_database(tmp_path):
    """Real cross-thread SQLite contention through the pinned write pattern.

    Eight threads, each with its own connection (exactly how
    concurrent per-request connections behave), hammer the store +
    charge writes concurrently. Without the busy timeout, parallel
    writers fail with sqlite3.OperationalError("database is locked");
    with it they queue and every write lands.
    """
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.close()

    workers = 8
    writes_per_worker = 5
    barrier = threading.Barrier(workers)  # maximize write overlap
    errors = []

    def writer(worker_index):
        own = accounts.connect(tmp_path / "accounts.db")
        try:
            barrier.wait(10)
            for i in range(writes_per_worker):
                fingerprint = transcript_store.fingerprint_of(
                    f"contended-audio-{worker_index}-{i}".encode()
                )
                try:
                    accounts.record_transcription(own, account_id, 1)
                    transcript_store.store_transcript(
                        own, fingerprint, {"language": "zh", "segments": []}
                    )
                except sqlite3.OperationalError as exc:
                    errors.append(str(exc))
        finally:
            own.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()

    assert errors == []  # no "database is locked" anywhere
    assert len(reuse_rows(tmp_path)) == workers * writes_per_worker
    assert len(usage_rows(tmp_path)) == workers * writes_per_worker
    assert account_row(tmp_path, "同学A")["transcribe_seconds_remaining"] == -(
        workers * writes_per_worker
    )  # every charge landed exactly once


# --- VAL-DEDUP-017: concurrent identical misses, two accounts ---


def test_two_accounts_identical_overlapping_misses_one_call_one_charge(tmp_path):
    audio = b"overlap-audio-two-accounts"
    vendor = OverlapVendor(transcript_store.fingerprint_of(audio))
    app, tokens = make_app(tmp_path, streaming_asr_fn=vendor)
    vendor.bind(app)
    labels = ["同学A", "同学B"]

    responses = run_overlapping(
        app,
        [
            {"token": tokens["同学A"], "audio": audio, "filename": "a.m4a"},
            {
                "token": tokens["同学B"],
                "audio": audio,
                "filename": "same-bytes-other-name.wav",  # multipart never keys
            },
        ],
    )

    assert [response.status_code for response in responses] == [200, 200]
    bodies = [response.json() for response in responses]
    assert vendor.calls == 1  # exactly one upstream call in total
    assert vendor.overlapped is True  # the second request was genuinely in flight
    assert set(vendor.threads) == {vendor.threads[0]}  # a single in-flight call

    miss_index, hit_index = (
        (0, 1) if bodies[0]["reused"] is False else (1, 0)
    )  # who ran upstream is not fixed; what happened to each is
    miss, hit = bodies[miss_index], bodies[hit_index]
    assert miss["reused"] is False and miss["seconds_charged"] == 120
    assert hit["reused"] is True and hit["seconds_charged"] == 0
    assert miss["segments"] == hit["segments"]  # identical payloads to both callers

    assert len(reuse_rows(tmp_path)) == 1  # exactly one row
    found = usage_rows(tmp_path)
    assert len(found) == 1  # exactly one usage row...
    assert found[0]["kind"] == "transcribe"
    assert found[0]["seconds"] == 120
    assert found[0]["account_id"] == account_row(tmp_path, labels[miss_index])["id"]
    # ...on the account that ran upstream; the other account is unchanged
    assert (
        account_row(tmp_path, labels[miss_index])["transcribe_seconds_remaining"]
        == 480
    )
    assert (
        account_row(tmp_path, labels[hit_index])["transcribe_seconds_remaining"]
        == 600
    )


# --- VAL-DEDUP-018: concurrent identical misses, same account ---


def test_same_account_identical_overlapping_misses_charged_once(tmp_path):
    audio = b"overlap-audio-same-account"
    vendor = OverlapVendor(transcript_store.fingerprint_of(audio))
    app, tokens = make_app(
        tmp_path, streaming_asr_fn=vendor, provisioned=(("同学A", 600),)
    )
    vendor.bind(app)

    responses = run_overlapping(
        app,
        [
            {"token": tokens["同学A"], "audio": audio},
            {"token": tokens["同学A"], "audio": audio},
        ],
    )

    assert [response.status_code for response in responses] == [200, 200]
    bodies = [response.json() for response in responses]
    assert vendor.calls == 1  # one upstream call in total
    assert vendor.overlapped is True
    assert sorted(body["reused"] for body in bodies) == [False, True]
    miss = next(body for body in bodies if body["reused"] is False)
    hit = next(body for body in bodies if body["reused"] is True)
    assert miss["seconds_charged"] == 120
    assert hit["seconds_charged"] == 0
    assert miss["segments"] == hit["segments"]  # identical payloads

    assert len(usage_rows(tmp_path)) == 1  # one charge, not two
    assert len(reuse_rows(tmp_path)) == 1
    remaining = account_row(tmp_path, "同学A")["transcribe_seconds_remaining"]
    assert remaining == 480  # 600 - 120, never 360


# --- VAL-DEDUP-019: 8 simultaneous distinct fingerprints ---


def test_eight_simultaneous_distinct_audios_independent_without_db_lock(tmp_path):
    audios = [f"parallel-audio-{i}".encode() for i in range(8)]
    durations = {audio: 30 + i for i, audio in enumerate(audios)}
    vendor = SlowDistinctVendor(durations)
    app, tokens = make_app(
        tmp_path, streaming_asr_fn=vendor, provisioned=(("同学A", 600),)
    )

    responses = run_overlapping(
        app, [{"token": tokens["同学A"], "audio": audio} for audio in audios]
    )

    assert [response.status_code for response in responses] == [200] * 8
    assert vendor.calls == 8  # 8 upstream calls, one per distinct audio
    # all 8 were in flight at the vendor at the same time: the sleep
    # overlaps them, so they must have run on 8 distinct worker threads
    assert len(set(vendor.threads)) == 8

    expected_charges = {audio: 30 + i for i, audio in enumerate(audios)}
    for audio, response in zip(audios, responses):
        body = response.json()
        assert body["reused"] is False  # every fingerprint was its own miss
        assert body["seconds_charged"] == expected_charges[audio]
        assert body["segments"] == [
            {"start": 0.0, "end": 1.5, "text": f"transcript:{audio!r}"}
        ]

    found = usage_rows(tmp_path)
    assert sorted(row["seconds"] for row in found) == sorted(
        expected_charges.values()
    )  # 8 correct charges
    fingerprints = {row["fingerprint_sha256"] for row in reuse_rows(tmp_path)}
    assert fingerprints == {
        transcript_store.fingerprint_of(audio) for audio in audios
    }  # 8 rows
    assert len(reuse_rows(tmp_path)) == 8
    remaining = account_row(tmp_path, "同学A")["transcribe_seconds_remaining"]
    assert remaining == 600 - sum(expected_charges.values())  # every charge landed


# --- VAL-DEDUP-020: first-attempt failure falls through to the waiter ---


def test_failed_first_attempt_falls_through_waiter_runs_own_upstream(tmp_path):
    audio = b"fall-through-audio"
    vendor = OverlapVendor(
        transcript_store.fingerprint_of(audio), fail_first=True
    )
    app, tokens = make_app(tmp_path, streaming_asr_fn=vendor)
    vendor.bind(app)
    labels = ["同学A", "同学B"]

    responses = run_overlapping(
        app,
        [
            {"token": tokens["同学A"], "audio": audio},
            {"token": tokens["同学B"], "audio": audio},
        ],
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 502]  # the first attempt failed, the waiter succeeded
    assert vendor.calls == 2  # the waiter ran its OWN upstream attempt
    assert vendor.overlapped is True  # genuine overlap: parked during the failure
    assert len(set(vendor.threads)) == 2  # two distinct in-flight attempts

    failed = next(r for r in responses if r.status_code == 502)
    succeeded = next(r for r in responses if r.status_code == 200)
    failed_label = labels[responses.index(failed)]
    succeeded_label = labels[responses.index(succeeded)]

    body = succeeded.json()
    assert body["reused"] is False  # the waiter's own upstream call stored it
    assert body["seconds_charged"] == 120

    assert len(reuse_rows(tmp_path)) == 1  # one stored row, no phantom from the 502
    found = usage_rows(tmp_path)
    assert len(found) == 1  # zero charges for the 502; the waiter charged itself
    assert found[0]["seconds"] == 120
    assert found[0]["account_id"] == account_row(tmp_path, succeeded_label)["id"]
    assert (
        account_row(tmp_path, failed_label)["transcribe_seconds_remaining"] == 600
    )  # the 502 was uncharged
    assert (
        account_row(tmp_path, succeeded_label)["transcribe_seconds_remaining"] == 480
    )
