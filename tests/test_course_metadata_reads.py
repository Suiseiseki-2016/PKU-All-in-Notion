from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from pku_sync.materials import ToolUnavailable
from pku_sync.tui_data import read_course_metadata


def readers():
    def recordings(client, course_id):
        return [SimpleNamespace(course_id=course_id, title="recording")]

    def materials(client, course_id):
        return ([SimpleNamespace(course_id=course_id, title="material")],
                [SimpleNamespace(course_id=course_id, title="assignment")])

    def announcements(client, course_id):
        return [SimpleNamespace(course_id=course_id, title="notice")]

    return dict(recordings_fn=recordings, materials_fn=materials,
                announcements_fn=announcements)


def test_parallel_courses_share_authenticated_client_with_at_most_four_reads():
    client = object()
    gate = threading.Barrier(4, timeout=3)
    lock = threading.Lock()
    active = peak = 0
    calls = []
    functions = readers()

    def recordings(received, course_id):
        nonlocal active, peak
        assert received is client
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(course_id)
        try:
            gate.wait()
        finally:
            with lock:
                active -= 1
        return [SimpleNamespace(course_id=course_id)]

    functions["recordings_fn"] = recordings
    results = read_course_metadata(client, [f"c{i}" for i in range(8)],
                                   max_workers=20, **functions)
    assert peak == 4
    assert set(calls) == set(results) == {f"c{i}" for i in range(8)}
    for course_id, result in results.items():
        assert result.course_id == course_id
        assert result.recordings[0].course_id == course_id
        assert result.material_items[0].course_id == course_id
        assert result.tree_assignments[0].course_id == course_id
        assert result.notices[0].course_id == course_id
        assert (result.recordings_status, result.materials_status,
                result.notice_status) == ("available", "available", "available")


def test_recording_failure_preserves_exception_and_stops_other_reads():
    failure = RuntimeError("incomplete recordings HTML")
    functions = readers()

    def recordings(*args):
        raise failure

    def forbidden(*args):
        pytest.fail("must not read other sources after recording failure")

    functions.update(recordings_fn=recordings, materials_fn=forbidden,
                     announcements_fn=forbidden)
    result = read_course_metadata(object(), ["a"], **functions)["a"]
    assert result.recordings is None and result.recordings_error is failure
    assert result.recordings_status == "error"
    assert result.material_items is result.tree_assignments is result.notices is None
    assert result.materials_status == result.notice_status == "not_read"


@pytest.mark.parametrize("failure,status", [(ToolUnavailable("disabled"), "unavailable"),
                                           (RuntimeError("malformed"), "error")])
def test_material_failure_still_reads_notices_and_preserves_distinct_notice_failure(failure, status):
    materials_failure = RuntimeError("incomplete materials")
    functions = readers()
    calls = []

    def materials(*args):
        raise materials_failure

    def announcements(*args):
        calls.append("notices")
        raise failure

    functions.update(materials_fn=materials, announcements_fn=announcements)
    result = read_course_metadata(object(), ["a"], **functions)["a"]
    assert calls == ["notices"]
    assert result.materials_error is materials_failure
    assert result.material_items is result.tree_assignments is None
    assert result.materials_status == "error"
    assert result.notices is None and result.notices_error is failure
    assert result.notice_status == status


def test_genuine_empty_lists_are_successful_and_objects_are_not_copied():
    recordings, material_items, assignments, notices = [], [], [], []
    result = read_course_metadata(object(), ["a"],
                                  recordings_fn=lambda *_: recordings,
                                  materials_fn=lambda *_: (material_items, assignments),
                                  announcements_fn=lambda *_: notices)["a"]
    assert result.recordings is recordings and result.material_items is material_items
    assert result.tree_assignments is assignments and result.notices is notices
    assert (result.recordings_status, result.materials_status, result.notice_status) == (
        "available", "available", "available")


def test_failed_materials_do_not_hide_successful_notice_read():
    functions = readers()
    failure = OSError("unreadable material tree")

    def materials(*args):
        raise failure

    functions["materials_fn"] = materials
    result = read_course_metadata(object(), ["a"], **functions)["a"]
    assert result.materials_error is failure and result.material_items is None
    assert result.notices[0].course_id == "a"
    assert result.notice_status == "available" and result.notices_error is None


@pytest.mark.parametrize("kind", ["recordings", "materials", "announcements"])
def test_cross_course_rows_fail_closed(kind):
    functions = readers()
    wrong = [SimpleNamespace(course_id="another-course")]
    functions[f"{kind}_fn"] = (lambda *_: (wrong, [])) if kind == "materials" else lambda *_: wrong
    result = read_course_metadata(object(), ["a"], **functions)["a"]
    error = getattr(result, {"recordings": "recordings_error", "materials": "materials_error",
                             "announcements": "notices_error"}[kind])
    assert isinstance(error, ValueError)
    assert "course" in str(error)


def test_cancel_drains_inflight_reads_without_starting_followup_or_queued_courses():
    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    calls = []
    failure = RuntimeError("cancel requested")
    owner_thread = threading.get_ident()

    def recordings(client, course_id):
        calls.append((course_id, "recordings"))
        started.set()
        assert released.wait(3)
        finished.set()
        return []

    def forbidden(*args):
        calls.append(("unexpected", "other"))
        return []

    def check_cancel():
        assert threading.get_ident() == owner_thread
        if started.is_set():
            raise failure

    timer = threading.Timer(.15, released.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError) as caught:
            read_course_metadata(object(), ["a", "b", "c"], max_workers=1,
                                 recordings_fn=recordings, materials_fn=forbidden,
                                 announcements_fn=forbidden, check_cancel=check_cancel)
        assert caught.value is failure
        assert finished.is_set()
        assert calls == [("a", "recordings")]
        time.sleep(.02)
        assert calls == [("a", "recordings")]
    finally:
        released.set()
        timer.join()


def test_duplicate_course_ids_are_rejected_before_reading():
    functions = {key: lambda *_: pytest.fail("invalid identity must not request")
                 for key in readers()}
    with pytest.raises(ValueError, match="duplicate"):
        read_course_metadata(object(), ["a", "a"], **functions)


def test_parallel_cancel_joins_all_four_readers_and_never_submits_remaining_courses():
    lock = threading.Lock()
    first_window_started = threading.Event()
    release = threading.Event()
    calls = []
    finished = []
    failure = RuntimeError("cancel parallel collection")
    owner_thread = threading.get_ident()

    def recordings(client, course_id):
        with lock:
            calls.append((course_id, "recordings"))
            if len(calls) == 4:
                first_window_started.set()
        assert release.wait(3)
        with lock:
            finished.append(course_id)
        return []

    def forbidden(client, course_id):
        with lock:
            calls.append((course_id, "unexpected"))
        raise AssertionError("must not start another source after cancellation")

    def check_cancel():
        assert threading.get_ident() == owner_thread
        if first_window_started.is_set():
            raise failure

    timer = threading.Timer(.2, release.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError) as caught:
            read_course_metadata(object(), [f"c{i}" for i in range(12)],
                                 recordings_fn=recordings, materials_fn=forbidden,
                                 announcements_fn=forbidden, check_cancel=check_cancel)
        assert caught.value is failure
        assert set(finished) == {f"c{i}" for i in range(4)}
        assert set(calls) == {(f"c{i}", "recordings") for i in range(4)}
        assert not any(thread.name.startswith("campus-metadata")
                       for thread in threading.enumerate())
    finally:
        release.set()
        timer.join()
