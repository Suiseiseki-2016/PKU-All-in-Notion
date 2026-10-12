"""Preparation scheduling contracts; no real service/native E2E claims."""
from contextlib import ExitStack
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import catalog_snapshots as catalog, platform, projection_receipts as receipts


def configuration(tmp_path):
    return SimpleNamespace(data_dir=tmp_path / "profile", platform_token="fixture-token",
                           cloud_transcribe_url="https://fixture.invalid/v1/transcribe")


def envelope(course):
    return {"revision": 3, "snapshot": {"schema": 1, "authority": "course.pku.edu.cn", "course_id": course,
            "course": {"course_id": course, "name": course, "code": "", "term": "fixture"},
            "sections": {key: {"complete": True, "items": []} for key in catalog.SECTIONS}}}


def test_current_batch_is_bounded_and_preserves_input_order_and_same_connection_epoch(tmp_path):
    settings = configuration(tmp_path)
    barrier = threading.Barrier(4, timeout=2)
    clients = []
    lock = threading.Lock()
    active = peak = 0

    def read(settings, course):
        nonlocal active, peak
        with lock:
            clients.append(platform._request_client.get())
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
            return envelope(course)
        finally:
            with lock:
                active -= 1

    with platform.request_scope():
        expected_client = platform._request_client.get()
        results = catalog.read_current_batch(settings, [f"c{i}" for i in range(8)], read=read, max_workers=12)
    assert peak == 4
    assert all(client is expected_client for client in clients)
    assert [row["snapshot"]["course_id"] for row in results] == [f"c{i}" for i in range(8)]


@pytest.fixture
def linked(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    courses = [f"_10{i}_1" for i in range(7)]
    snapshots = {course: envelope(course) for course in courses}
    calls = []
    owner_thread = threading.get_ident()
    lock = threading.Lock()

    def record(method, course, suffix):
        with lock:
            calls.append((method, course, suffix, threading.get_ident()))

    def send(settings, method, course, suffix="", body=None):
        record(method, course, suffix)
        if method == "GET":
            return dict(receipts.PROTOCOL) if course == "projection-protocol" else snapshots[course]
        assert threading.get_ident() == owner_thread
        if suffix == "/projection":
            return {"client_request_id": body["client_request_id"], "revision": body["revision"],
                    "permit_id": catalog.fingerprint({"course": course}), "state": "acquired"}
        if suffix == "/projection/release":
            return {"released": True}
        raise AssertionError("unexpected write")

    def identity(url, **kwargs):
        record("GET", "identity", "")
        return httpx.Response(200, json={"email": "fixture@example.test", "email_verified": True})

    monkeypatch.setattr(catalog, "_call", send)
    monkeypatch.setattr(platform, "_get", identity)
    return settings, list(snapshots.values()), calls, snapshots, owner_thread


def test_prepared_reads_preserve_each_identity_and_both_current_reads_with_original_serial_writes(linked):
    settings, envelopes, calls, _, owner_thread = linked
    with platform.request_scope(), receipts.prepared_cohort() as cohort:
        with receipts.prepared_read_phase(settings, envelopes):
            assert not any(method == "POST" for method, _, _, _ in calls)
            with ExitStack() as stack:
                for expected in envelopes:
                    stack.enter_context(catalog.projection(settings, expected))
                assert len(cohort.members) == len(envelopes)
                cohort.begin()
                cohort.complete()
    assert sum(course == "projection-protocol" for _, course, _, _ in calls) == 7
    assert sum(course == "identity" for _, course, _, _ in calls) == 7
    courses = [value["snapshot"]["course_id"] for value in envelopes]
    for course in courses:
        current_reads = [row for row in calls if row[:3] == ("GET", course, "")]
        assert len(current_reads) == 2
        own = [row[:3] for row in calls if row[1] == course]
        assert own == [("GET", course, ""), ("POST", course, "/projection"),
                       ("GET", course, ""), ("POST", course, "/projection/release")]
        assert current_reads[1][3] == owner_thread
    assert [course for method, course, suffix, _ in calls if method == "POST" and suffix == "/projection"] == courses


@pytest.mark.parametrize("mutation", ["token", "endpoint", "root", "age", "order", "snapshot"])
def test_phase_binding_or_expiry_rejects_before_any_acquire_or_journal(linked, mutation, monkeypatch, tmp_path):
    settings, envelopes, calls, _, _ = linked
    with platform.request_scope(), receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
        expected = envelopes[0]
        if mutation == "token":
            settings.platform_token = "different"
        elif mutation == "endpoint":
            settings.cloud_transcribe_url = "https://other.invalid/v1/transcribe"
        elif mutation == "root":
            settings.data_dir = tmp_path / "different"
        elif mutation == "age":
            real_time = time.monotonic
            monkeypatch.setattr(receipts.time, "monotonic", lambda: real_time() + 40)
        elif mutation == "order":
            expected = envelopes[1]
        else:
            expected = {**expected, "snapshot": {**expected["snapshot"], "course": {**expected["snapshot"]["course"], "name": "changed"}}}
        with pytest.raises(catalog.CatalogUnavailable):
            with catalog.projection(settings, expected):
                pytest.fail("mismatched phase must never authorize")
    assert not any(method == "POST" for method, _, _, _ in calls)
    assert not list(tmp_path.rglob("*.json"))


def test_post_acquire_current_read_remains_fresh_and_detects_actual_change(linked, monkeypatch):
    settings, envelopes, calls, snapshots, _ = linked
    original = catalog._call

    def changed(settings, method, course, suffix="", body=None):
        value = original(settings, method, course, suffix, body)
        if method == "POST" and suffix == "/projection":
            snapshots[course] = {**snapshots[course], "revision": 4}
        return value

    monkeypatch.setattr(catalog, "_call", changed)
    with platform.request_scope(), receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
        with pytest.raises(catalog.CatalogUnavailable, match="已更新"):
            with catalog.projection(settings, envelopes[0]):
                pytest.fail("fresh post acquire mismatch must block body")
    assert sum(method == "POST" and suffix == "/projection" for method, _, suffix, _ in calls) == 1
    assert sum(method == "POST" and suffix == "/projection/release" for method, _, suffix, _ in calls) == 1


def test_read_failure_drains_pool_and_never_creates_projection_intent(linked, monkeypatch):
    settings, envelopes, calls, _, _ = linked
    original = catalog._call
    failure = catalog.CatalogTransportUnavailable("fixture disconnected")

    def failing(settings, method, course, suffix="", body=None):
        if course == envelopes[0]["snapshot"]["course_id"]:
            raise failure
        return original(settings, method, course, suffix, body)

    monkeypatch.setattr(catalog, "_call", failing)
    with pytest.raises(catalog.CatalogTransportUnavailable) as caught:
        with receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
            pytest.fail("must not enter body after failed preparation")
    assert caught.value is failure
    assert not any(method == "POST" for method, _, _, _ in calls)
    assert not list(settings.data_dir.rglob("*.json"))
    assert not any(thread.name.startswith("catalog-read") for thread in threading.enumerate())


def test_worker_cannot_issue_a_mutation(tmp_path):
    settings = configuration(tmp_path)

    def forbidden(settings, course):
        return platform._post("https://fixture.invalid/v1/catalog/" + course)

    with pytest.raises(platform.PlatformError, match="只读"):
        catalog.read_current_batch(settings, ["a", "b"], read=forbidden)


def test_cancellation_joins_readers_and_stops_followup_identity_current_or_queued_courses(linked, monkeypatch):
    settings, envelopes, calls, _, _ = linked
    original = catalog._call
    started = threading.Event()
    lock = threading.Lock()
    count = 0
    failure = RuntimeError("explicit cancel")

    def blocked(settings, method, course, suffix="", body=None):
        nonlocal count
        if course == "projection-protocol":
            with lock:
                count += 1
                if count == 4:
                    started.set()
            # Hold these reads until cancellation has actually been observed
            # by the coordinator. A fixed timer can release them before that
            # happens on a busy Windows runner, allowing legitimate pre-cancel
            # identity reads and invalidating the test's ordering premise.
            stop = platform._read_worker_stop.get()
            assert stop is not None and stop.wait(5)
        return original(settings, method, course, suffix, body)

    def cancel():
        if started.is_set():
            raise failure

    monkeypatch.setattr(catalog, "_call", blocked)
    with pytest.raises(RuntimeError) as caught:
        with receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes, check_cancel=cancel):
            pytest.fail("canceled preparation cannot enter the body")
    assert caught.value is failure
    assert count == 4
    assert all(method == "GET" and course == "projection-protocol" for method, course, _, _ in calls)
    assert not any(thread.name.startswith("catalog-read") for thread in threading.enumerate())


def test_phase_rejects_different_cohort_operation_before_any_mutation(linked):
    settings, envelopes, calls, _, _ = linked
    with receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
        token = receipts._cohort.set(receipts._Cohort())
        try:
            with pytest.raises(catalog.CatalogUnavailable):
                with catalog.projection(settings, envelopes[0]):
                    pytest.fail("proof cannot cross operation/cohort")
        finally:
            receipts._cohort.reset(token)
    assert not any(method == "POST" for method, _, _, _ in calls)


def test_unknown_acquire_response_prevents_next_course_using_prefetched_reads(linked, monkeypatch):
    settings, envelopes, calls, _, _ = linked
    original = catalog._call

    def lost(settings, method, course, suffix="", body=None):
        value = original(settings, method, course, suffix, body)
        if method == "POST" and suffix == "/projection":
            raise catalog.CatalogUnavailable("actual fixture acquire response lost")
        return value

    monkeypatch.setattr(catalog, "_call", lost)
    with receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
        with pytest.raises(catalog.CatalogUnavailable):
            with catalog.projection(settings, envelopes[0]):
                pytest.fail("must not enter unknown acquired writer")
        with pytest.raises(catalog.CatalogUnavailable):
            with catalog.projection(settings, envelopes[1]):
                pytest.fail("uncertain previous acquire must prevent later course")
    assert sum(method == "POST" for method, _, _, _ in calls) == 1
    assert len(list(settings.data_dir.rglob("*.json"))) == 1


def test_phase_cannot_be_reused_after_account_temporarily_changes(linked):
    settings, envelopes, calls, _, _ = linked
    with receipts.prepared_cohort(), receipts.prepared_read_phase(settings, envelopes):
        old_token = settings.platform_token
        settings.platform_token = "other"
        with pytest.raises(catalog.CatalogUnavailable):
            with catalog.projection(settings, envelopes[0]):
                pass
        settings.platform_token = old_token
        with pytest.raises(catalog.CatalogUnavailable):
            with catalog.projection(settings, envelopes[0]):
                pytest.fail("changing back is not authority to reuse invalidated evidence")
    assert not any(method == "POST" for method, _, _, _ in calls)


def test_current_batch_keeps_explicit_unpublished_none_and_reads_again_next_operation(tmp_path):
    settings = configuration(tmp_path)
    calls = []

    def read(settings, course):
        calls.append(course)
        return None if course == "unpublished" else envelope(course)

    for _ in range(2):
        result = catalog.read_current_batch(settings, ["known", "unpublished"], read=read)
        assert result[0]["snapshot"]["course_id"] == "known" and result[1] is None
    assert calls.count("known") == calls.count("unpublished") == 2
