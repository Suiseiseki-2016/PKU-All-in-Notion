"""Installed-window learning journeys; writes happen through UI, or not at all.

The independent oracle reads account, portal and Notion services. Local panel GETs
are observations, not an oracle for successful remote writes. Missing fixtures,
cost authority or native capabilities fail closed; configured booleans are never
accepted as evidence of a completed action.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import subprocess
import time
import zipfile
import httpx
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from product_e2e.core import ResourceBlocked
from product_e2e.oss_lifecycle import verified_audio_cleanup


def _block(run, code, detail):
    raise ResourceBlocked(code)


def _capability(run, obj, name):
    method = getattr(obj, name, None)
    if not callable(method):
        _block(run, "software_capability_missing_" + name, name)
    return method


def _action(action, **attributes):
    selector = '[data-action="' + action + '"]'
    for key, value in attributes.items():
        if not re.fullmatch(r"[A-Za-z0-9_:. -]+", str(value)):
            raise ValueError("Unsafe native selector identity")
        selector += '[data-' + key + '="' + str(value) + '"]'
    return selector


def _read(ui, path):
    value = ui.request_read(path)
    if not isinstance(value, dict):
        raise TypeError("Panel GET did not return an object")
    return value


def _open_course(run, course_id, name="primary", *, center=None):
    ui = run.session(name)
    selector = _action("open-campus-course", course=course_id)
    if not ui.visible(selector):
        ui.click(".sidebar " + _action("open-dashboard"))
    ui.wait(lambda: ui.visible(selector))
    ui.click(selector)
    ui.wait(lambda: ui.visible('.course-hero[data-course-id="' + course_id + '"] h1')
            and ui.visible("#course-lessons"))
    section = center or {"JOURNEY-04": "assignments", "JOURNEY-05": "quizzes"}.get(run.scenario["id"])
    if section and ui.visible('[data-action="campus-open-center"]'):
        ui.click(_action("campus-open-center", center=section))
    course = _read(ui, "/api/campus/courses/" + quote(course_id, safe=""))
    if course.get('id') != course_id:
        raise AssertionError('Native course response identity does not match selected course')
    return ui, course


def _lesson_page(ui, recording_id):
    if ui.visible('[data-action="campus-course-home"]'):
        ui.click(_action("campus-course-home"))
    selector = _action("campus-open-lesson", recording=recording_id)
    if not ui.visible(selector):
        raise ResourceBlocked('selected-native-lesson-navigation-control-missing')
    ui.click(selector)
    ui.wait(lambda: ui.visible(_action('campus-process', recording=recording_id))
            or ui.visible(_action('campus-regenerate', recording=recording_id)))


def _task(ui, timeout=180):
    ui.wait(lambda: _read(ui, "/api/recordings/task").get("state") != "running", timeout=timeout)
    return _read(ui, "/api/recordings/task")


def _settings(ui):
    if ui.visible('[data-action="campus-course-settings"]'):
        ui.click(_action("campus-course-settings"))
        return
    if not ui.visible(_action("campus-create-workspace")):
        ui.click(".campus-settings > summary")


def _confirm(run, ui, selector, accepted=True):
    _capability(run, ui, "prepare_dialog")(accepted)
    ui.click(selector)
    _capability(run, ui, "confirm")(accepted)


def _plain(row):
    body = row.get(row.get("type", ""), {})
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                   for part in body.get("rich_text", []))


def _blocks(run, page_id):
    result, cursor = [], None
    for _ in range(100):
        path = "/v1/blocks/" + quote(page_id, safe="") + "/children"
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        value = run.oracle.get("notion", path, params)
        if not isinstance(value.get("results"), list):
            raise TypeError("Independent Notion read is not a block collection")
        result.extend(value["results"])
        if not value.get("has_more"):
            return result
        cursor = value.get("next_cursor")
        if not cursor:
            raise AssertionError("Notion pagination cursor missing")
    raise AssertionError("Notion read exceeded bounded pagination")


def _page(run, page_id, parent_id=None):
    page = run.oracle.get("notion", "/v1/pages/" + quote(page_id, safe=""))
    ok = not page.get("archived") and not page.get("in_trash")
    if parent_id is not None:
        ok = ok and _identity(page.get("parent", {}).get("page_id")) == _identity(parent_id)
    return page, ok


def _identity(value):
    return str(value or "").replace("-", "")


def _markers(rows, prefix):
    result = []
    for row in rows:
        text = _plain(row)
        if text.startswith(prefix):
            value = json.loads(text[len(prefix):])
            if isinstance(value, dict):
                result.append(value)
    return result


def _note_fingerprint(run, page_id):
    rows = _note_content(run, page_id)
    return hashlib.sha256(json.dumps(_normalize(rows), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _published_note_id(run, lesson, lecture_id, course_id):
    """Resolve a publication before acceptance, using local identity only as a candidate."""
    try:
        source = lesson['source_id']
        matches = []
        for path in _data_root(run).rglob('recording.json'):
            _safe_review_file(str(path))
            recording = json.loads(path.read_text('utf-8'))
            key = recording.get('source_id')
            if not key and recording.get('recorded_at'):
                identity = json.dumps({'course': recording.get('course_id'), 'start': recording['recorded_at']},
                                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                key = 'pku:recording:' + hashlib.sha256(identity.encode()).hexdigest()[:32]
            if key == source:
                if recording.get('course_id') != course_id:
                    raise ValueError
                matches.append(path.parent)
        if len(matches) != 1:
            raise ValueError
        version_path = _safe_review_file(str(matches[0] / 'published-note-version.json'))
        notes_path = _safe_review_file(str(matches[0] / 'notes.md'))
        version_bytes = version_path.read_bytes()
        version = json.loads(version_bytes)
        notes = notes_path.read_bytes()
        note_id = version.get('note_id')
        valid_uuid = r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
        if (not isinstance(note_id, str) or not re.fullmatch(valid_uuid, note_id)
                or not re.fullmatch(valid_uuid, str(lecture_id))
                or version.get('source_id') != source
                or _identity(version.get('lecture_id')) != _identity(lecture_id)
                or not notes.strip() or hashlib.sha256(notes).hexdigest() != version.get('notes_sha256')
                or not re.fullmatch(r'[0-9a-f]{64}', str(version.get('remote_sha256')))):
            raise ValueError
        url = urlsplit(lesson.get('notion_note_url', ''))
        tail = unquote(url.path).rstrip('/').rsplit('/', 1)[-1]
        identity_match = re.search(r'([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$', tail)
        official_path = (url.hostname in {'www.notion.so', 'notion.so'}
                         or url.hostname == 'app.notion.com' and bool(re.fullmatch(r'/p/[^/]+', url.path)))
        if (url.scheme != 'https' or not official_path
                or url.username or url.password or url.port or url.query or url.fragment
                or not identity_match or _identity(identity_match[1]) != _identity(note_id)):
            raise ValueError
        page, parent_ok = _page(run, note_id, lecture_id)
        if (not parent_ok or page.get('object') != 'page' or _identity(page.get('id')) != _identity(note_id)
                or _identity(page.get('parent', {}).get('page_id')) != _identity(lecture_id)
                or page.get('archived') or page.get('in_trash')):
            raise ValueError
        expected_url = ('https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action#'
                        + quote(source, safe=''))
        links = [part.get('text', {}).get('link', {}).get('url')
                 for block in _blocks(run, lecture_id) if block.get('type') == 'paragraph'
                 for part in block.get('paragraph', {}).get('rich_text', [])
                 if part.get('text', {}).get('content') == '查看教学网讲次来源']
        if links != [expected_url] or _note_fingerprint(run, note_id) != version['remote_sha256'] \
                or version_path.read_bytes() != version_bytes or notes_path.read_bytes() != notes:
            raise ValueError
        return note_id
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ResourceBlocked('published_note_identity_or_version_independently_unconfirmed') from None


def _note_content(run, page_id):
    rows = []
    for row in _blocks(run, page_id):
        if _markers([row], "PKU 总结验收版本 · "):
            continue
        kind = row["type"]
        body = dict(row.get(kind, {}))
        if row.get("has_children"):
            body["children"] = _note_content(run, row["id"])
        rows.append({"type": kind, kind: body})
    return rows


def _normalize(value):
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _normalize(item) for key, item in value.items()
              if key not in {"plain_text", "href", "expiry_time"}}
    if isinstance(result.get("file"), dict) and isinstance(result["file"].get("url"), str):
        parts = urlsplit(result["file"]["url"])
        result["file"]["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return result


def _ledger(run):
    ledger = run.oracle.operations()
    if not isinstance(ledger, dict):
        _block(run, "independent_operation_ledger_missing", "No remote job/charge ledger")
    for key in ("jobs", "settlements", "submission_posts", "oss_objects"):
        if not isinstance(ledger.get(key), list):
            _block(run, "independent_operation_ledger_missing", key)
    return ledger


def _new_rows(before, after, key):
    old = {json.dumps(row, sort_keys=True) for row in before[key]}
    return [row for row in after[key] if json.dumps(row, sort_keys=True) not in old]


def _verified_processing_charges(before, processed, after, source_id, course_id, duration):
    """One settlement per real ASR/model job, and no new effect after reopening."""
    try:
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            return False
        for snapshot in (before, processed, after):
            if (type(snapshot.get("schema")) is not int or snapshot["schema"] != 1
                    or snapshot.get("complete") is not True
                    or snapshot.get("source") != "account_cumulative_usage_rows"):
                return False
            for key in ("jobs", "settlements"):
                rows = snapshot[key]
                if (not isinstance(rows, list)
                        or len({row["id"] for row in rows}) != len(rows)):
                    return False
        for key in ("jobs", "settlements"):
            old = {row["id"]: row for row in before[key]}
            current = {row["id"]: row for row in processed[key]}
            reopened = {row["id"]: row for row in after[key]}
            if current != reopened or any(current.get(key) != value for key, value in old.items()):
                return False
        jobs = [row for row in processed["jobs"] if row["id"] not in {r["id"] for r in before["jobs"]}]
        charges = [row for row in processed["settlements"] if row["id"] not in {r["id"] for r in before["settlements"]}]
        operations = {row["operation_id"] for row in jobs}
        if (not jobs or len(operations) != len(jobs) or len(jobs) != len(charges)
                or {row["operation_id"] for row in charges} != operations
                or len({row["operation_id"] for row in charges}) != len(charges)
                or operations & {row["operation_id"] for row in before["jobs"] + before["settlements"]}):
            return False
        asr = [row for row in jobs if row["operation_id"].startswith("asr:")]
        llm = [row for row in jobs if row not in asr]
        # The installed transcription pipeline sends at most 600 seconds per part.
        if (len(asr) != math.ceil(duration / 600)
                or len({row["fingerprint"] for row in asr}) != len(asr) or not llm):
            return False
        charge_by_op = {row["operation_id"]: row for row in charges}
        seconds, points = 0, Decimal("0")
        for job in asr:
            charge = charge_by_op[job["operation_id"]]
            value = charge["charged_seconds"]
            if (job["status"] != "done" or type(value) is not int or not 0 < value <= 600
                    or job["seconds_charged"] != value or charge.get("charged_points") is not None):
                return False
            seconds += value
        for job in llm:
            charge = charge_by_op[job["operation_id"]]
            value = Decimal(str(charge["charged_points"]))
            if (job["status"] != "succeeded" or job.get("source_id") != source_id
                    or job.get("course_id") != course_id or job.get("purpose") != "lecture-notes"
                    or not value.is_finite() or value < 0
                    or Decimal(str(job["points_charged"])) != value
                    or charge.get("charged_seconds") is not None):
                return False
            points += value
        if seconds > math.ceil(duration) or points <= 0:
            return False
        for key in ("charged_points", "charged_seconds"):
            if processed[key] != after[key]:
                return False
        return (type(before["charged_seconds"]) is int and type(processed["charged_seconds"]) is int
                and processed["charged_seconds"] - before["charged_seconds"] == seconds
                and Decimal(str(processed["charged_points"])) - Decimal(str(before["charged_points"])) == points)
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def _data_root(run, name="primary"):
    root = Path(run.need("instances." + name + ".app_dir")).resolve() / "data"
    sandbox = Path(run.need("test_root")).resolve()
    if not root.is_relative_to(sandbox) or root == sandbox:
        _block(run, "learning_probe_outside_test_sandbox", "Never inspect a real student's data")
    return root


def _media_probe(run, lesson):
    matches = []
    for path in _data_root(run).rglob("recording.json"):
        recording = json.loads(path.read_text("utf-8"))
        # Native source identities are strings prefixed by the course. Match the
        # immutable recording item, not title or a guessed newest downloaded file.
        recording_key = recording.get("source_id")
        if not recording_key and recording.get("recorded_at"):
            identity = json.dumps({"course": recording.get("course_id"), "start": recording["recorded_at"]},
                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            recording_key = "pku:recording:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if recording_key == lesson["source_id"]:
            media = path.parent / "video.mp4"
            if media.is_file():
                matches.append(media)
    if len(matches) != 1:
        _block(run, "unique_downloaded_source_file_missing", "No unique actual source recording file")
    ffprobe = run.need("tools.ffprobe")
    result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(matches[0])],
                            capture_output=True, timeout=30, check=True)
    probe = json.loads(result.stdout)
    streams = probe.get("streams", [])
    duration = float(probe.get("format", {}).get("duration", 0))
    decoded = subprocess.run([run.need("tools.ffmpeg"), "-v", "error", "-xerror", "-i", str(matches[0]),
                              "-t", "3", "-map", "0:v:0", "-f", "null", "-"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    return {"source_id": lesson["source_id"], "bytes": matches[0].stat().st_size,
            "sha256": hashlib.sha256(matches[0].read_bytes()).hexdigest(),
            "playable": math.isfinite(duration) and duration > 0
                        and any(row.get("codec_type") == "video" for row in streams)
                        and decoded.returncode == 0}


def _download_cached_original(run, ui, recording_id):
    """Use the installed lesson's real download link, never a panel POST substitute."""
    if ui.page is None:
        raise ResourceBlocked("actual_windows_original_download_observer_required")
    path = "/api/recordings/" + quote(recording_id, safe="") + "/video"
    selector = 'a[download][href="' + path + '"]'
    if not ui.visible(selector):
        raise ResourceBlocked("cached_original_native_download_link_missing")
    # Keep the native WebView's actual file download inside this run's Windows
    # evidence directory. This changes only the save destination, not content.
    observer = ui.page.context.browser.new_browser_cdp_session()
    starts, terminals = [], {}
    observer.on("Browser.downloadWillBegin", lambda event: starts.append(event))
    observer.on("Browser.downloadProgress", lambda event: terminals.update({event["guid"]: event}))
    observer.send("Browser.setDownloadBehavior", {
        "behavior": "allowAndName", "downloadPath": str(run.case_dir.resolve()),
        "eventsEnabled": True})
    ui.click(selector)
    ui.wait(lambda: len(starts) == 1 and terminals.get(starts[0]["guid"], {}).get("state")
            in {"completed", "canceled"}, timeout=180)
    start = starts[0]
    terminal = terminals[start["guid"]]
    if (start.get("url") != ui.origin + path or terminal.get("state") != "completed"
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", start["guid"])):
        raise AssertionError("Native original download identity or completion is invalid")
    # allowAndName uses the actual Browser event GUID as the filename. Read
    # that native file, not Playwright's separate CDP-attachment artifact path.
    destination = run.case_dir.resolve() / start["guid"]
    if (not destination.is_file() or destination.is_symlink() or destination.stat().st_size <= 0
            or terminal.get("receivedBytes") != destination.stat().st_size
            or terminal.get("totalBytes") != destination.stat().st_size):
        raise AssertionError("Native original download bytes are incomplete")
    digest = hashlib.sha256()
    with destination.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    result = {"state": "done", "bytes": destination.stat().st_size,
              "sha256": digest.hexdigest(), "recording_id": recording_id,
              "file": destination.name, "browser_download_guid": start["guid"],
              "browser_received_bytes": terminal["receivedBytes"],
              "entry": "owned-installed-webview-cached-original-download"}
    run.actions.append({"native_original_cached_export": result})
    observer.detach()
    return result


def _draft_probe(run, course_id, content_id):
    candidates = []
    for path in (_data_root(run) / "submission-drafts").glob("*/draft.json"):
        row = json.loads(path.read_text("utf-8"))
        if row.get("course_id") == course_id and row.get("content_id") == content_id:
            candidates.append((path.stat().st_mtime_ns, path, row))
    if not candidates:
        _block(run, "native_prepared_draft_missing", "No native-written draft")
    _, path, row = max(candidates, key=lambda value: value[0])
    payload = path.parent / row["archive_name"]
    if payload.resolve().parent != path.parent.resolve():
        raise AssertionError("Unsafe draft archive filename")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    if digest != row.get("sha256"):
        raise AssertionError("Draft metadata hash differs from actual file")
    row["actual_path"] = str(payload)
    return row


def _preview(run, draft, official, cfg):
    payload = Path(draft["actual_path"])
    with zipfile.ZipFile(payload) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise AssertionError("Preview archive has ambiguous duplicate filenames")
        actual = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}
    expected = cfg["expected_member_sha256"]
    requirements = all(draft.get(key) == official.get(key)
                       for key in ("content_id", "due_at", "instructions"))
    return {"source_requirements_match": requirements, "content_match": actual == expected,
            "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}


def _review_text_preflight(run, fixture_key):
    from . import contextual_review
    return contextual_review.preflight(run, fixture_key.split(".", 1)[0], fixture_key)


def _review_text(run, page_id, fixture_key):
    from . import contextual_review
    return contextual_review.check(run, page_id, fixture_key.split(".", 1)[0], fixture_key)


def _grade_check(run, history_id, cfg):
    raw = next((_plain(row) for row in _blocks(run, history_id) if row.get("type") == "code"), None)
    if not raw:
        raise AssertionError("Independent Notion history contains no grading JSON")
    grade = json.loads(raw)
    rows = grade.get("questions", [])
    if len(rows) != 5:
        raise AssertionError("Independent grade lacks five question results")
    actual = {str(row["number"]): row for row in rows}
    objective = cfg["objective_expected"]
    objective_ok = bool(objective) and all(actual[key].get("score") == score
                                           for key, score in objective.items())
    bounds = cfg["subjective_bounds"]
    subjective_ok = bool(bounds) and all(low <= actual[key].get("score", -1) <= high
                                         for key, (low, high) in bounds.items())
    return objective_ok and subjective_ok and grade.get("score") == sum(row["score"] for row in rows)


def _faults(run, action, validate, *, restart_operation_ids=None):
    if run.mode != "fault":
        return []
    from scripts.product_e2e_gate import FAULT_VARIANTS
    outcomes = []
    for variant in run.scenario.get("fault_variants", FAULT_VARIANTS[run.scenario["id"]]):
        before = _ledger(run)
        if variant == 'restart-after-charge':
            run.context['_restart_after_charge_operations'] = (
                restart_operation_ids(before) if callable(restart_operation_ids) else restart_operation_ids)
        try:
            with run.fault(variant) as receipt:
                action(variant)
        except ResourceBlocked as exc:
            if exc.code.startswith(("unavailable_fault_boundary_", "mac_native_offline_controller_")):
                raise ResourceBlocked("software_capability_missing_fault_" + variant) from None
            raise
        finally:
            if variant == 'restart-after-charge':
                run.context.pop('_restart_after_charge_operations', None)
        after = _ledger(run)
        passed = validate(variant, before, after, receipt)
        run.fault_check(variant, passed, "Independent before/after ledger and native recovery")
        outcomes.append(passed)
    return outcomes


def _workspace(run, ui, *, selected_only=False):
    path = "/api/campus/notion/parents" + ("?selected_only=true" if selected_only else "")
    local = _read(ui, path).get("workspace", {})
    remote = run.oracle.profile().get("profile", {}).get("notion_workspace_v2", {})
    if not local.get("id") or _identity(local["id"]) != _identity(remote.get("id")):
        raise AssertionError("Installed device and account workspace identities differ")
    return local


def _same_notion_page(before, after):
    if not all(isinstance(row, dict) and row.get('object') == 'page' for row in (before, after)):
        return False
    # Notion assigns a fresh trace ID to each GET. Preserve every page field,
    # including edit times and nested properties; only this top-level trace varies.
    return {key: value for key, value in before.items() if key != 'request_id'} == {
        key: value for key, value in after.items() if key != 'request_id'}


def _notion_access_faults(run, ui, target, parent):
    """Three captured installed UI requests; independent reads stay unmodified."""
    boundary = run._notion_boundary
    observations = []
    ui.wait(lambda: ui.visible('.sidebar [data-action="campus-account-settings"]'), timeout=180)
    # Startup also loads this resource. Complete its real response and UI
    # delivery before arming, so an already-dispatched read cannot satisfy the
    # next click without exercising the selected external response fault.
    with ui.page.expect_response(lambda response: response.request.method == 'GET'
            and response.url.endswith('/api/campus/notion/parents'), timeout=180000) as warm:
        ui.click('.sidebar [data-action="campus-account-settings"]')
    warm.value.finished()
    if warm.value.status != 200:
        raise ResourceBlocked('notion_access_unfaulted_native_read_failed')
    ui.wait(lambda: ui.visible('[data-campus-parent]'), timeout=30)
    ui.page.wait_for_timeout(100)
    for kind in ('permission', 'archived', 'moved'):
        before = run.oracle.get('notion', '/v1/pages/' + target)
        if (before.get('archived') or before.get('in_trash')
                or _identity(before.get('parent', {}).get('page_id')) != _identity(parent)):
            raise ResourceBlocked('notion_access_original_page_not_active_at_expected_parent')
        boundary.configure_access_case(kind, target, parent)
        start = len(run.network)
        # This actual native read control reaches page verification without a
        # write permit. A blocked old writer cannot stand in for an access fault.
        with ui.page.expect_response(lambda response: response.request.method == 'GET'
                and response.url.endswith('/api/campus/notion/parents'), timeout=180000) as captured:
            ui.click('.sidebar [data-action="campus-account-settings"]')
        response = captured.value
        response.finished()
        if response.status >= 400:
            ui.wait(lambda: ui.visible('[role="alert"]'), timeout=30)
        else:
            ui.wait(lambda: ui.visible('[data-campus-parent]'), timeout=30)
        message = ui.text('[role="alert"]') if ui.visible('[role="alert"]') else ''
        real = [row for row in run.network[start:] if row.get('fault') == 'permission-archive-move'
                and row.get('access_case') == kind]
        after = run.oracle.get('notion', '/v1/pages/' + target)
        checks = {'expected_native_error_status': response.status == 502,
            'real_owned_metadata_response_was_corrupted': bool(real)
                and all(row['upstream_status'] == 200 and row.get('server_page_mutated') is False
                        and row['upstream_body_sha256'] != row['delivered_body_sha256'] for row in real),
            'actual_alert_gives_next_action': bool(message)
                and any(text in message for text in ('请检查', '请核对', '请重新', '请恢复', '请稍后', '后重试', '后再')),
            'independent_remote_page_unchanged': _same_notion_page(before, after),
            'rejected_before_home_descendant_read': not any(row.get('boundary') == 'notion'
                and row.get('method') == 'GET'
                and re.fullmatch(r'/v1/blocks/([0-9a-fA-F-]+)/children', row.get('path', ''))
                and _identity(row['path'].split('/')[3]) == _identity(target)
                for row in run.network[start:]),
            'no_remote_notion_mutation': not any(row.get('boundary') == 'notion'
                and row.get('method') != 'GET' and not (row.get('method') == 'POST'
                and row.get('path') == '/v1/search') for row in run.network[start:])}
        row = {'kind': kind, 'native_status': response.status, 'checks': checks,
               'passed': all(checks.values()), 'native_alert_sha256': hashlib.sha256(message.encode()).hexdigest(),
               'server_page_archive_or_move_claimed': False}
        observations.append(row)
        run.actions.append({'native_notion_access_subcase': row})
        ui.screenshot('native-notion-access-' + kind)
    return observations


def _expired_media_download(run, ui, lesson):
    """One actual installed download, fresh metadata recovery and cached browse."""
    boundary = run._media_boundary
    fixture = boundary.fixture
    if lesson.get('source_id') != fixture['source_id'] or lesson.get('id') != fixture['recording_id']:
        raise ResourceBlocked('expired_media_native_lesson_identity_mismatch')
    # Use the existing real-file observer, including a nonempty student
    # document fixture; an empty equality cannot prove preservation.
    from product_e2e.catalog_journeys import _file_hashes
    before = _file_hashes(run, student_only=True)
    if not before:
        raise ResourceBlocked('expired_media_nonempty_student_content_required')
    start = len(run.network)
    _lesson_page(ui, lesson['id'])
    path = '/api/recordings/' + lesson['id'] + '/download'
    with ui.page.expect_response(lambda response: response.request.method == 'POST'
            and response.url.endswith(path), timeout=30000) as captured:
        _confirm(run, ui, _action('recording-download', recording=lesson['id']))
    captured.value.finished()
    task = _task(ui)  # Preserve the original 180-second formal download limit.
    media = _media_probe(run, lesson) if task.get('state') == 'done' else None
    receipt = boundary.receipt
    events = run.network[start:]
    expired = [row for row in events if row.get('fault') == 'expired-media-url']
    metadata = [row for row in events if row.get('boundary') == 'media-HTTPS'
                and row.get('path', '').endswith('/get-course-subject-info')]
    browsed, course = _open_course(run, fixture['course_id'])
    checks = {
        'actual_native_download_post_accepted': captured.value.status == 200,
        'real_successful_source_request_received_expired_response': bool(expired)
            and all(row.get('source_verified') and row['upstream_status'] in (200, 206)
                    and row['status'] == 403 and row['upstream_tls_verified']
                    and row['server_media_modified'] is False for row in expired),
        'exactly_one_fresh_selected_metadata_recovery': receipt['metadata_reads'] == 2
            and len(metadata) == 2 and all(row.get('source_verified') and row.get('status') == 200 for row in metadata),
        'native_download_clicks_bounded': 1 <= sum(row.get('method') == 'POST' and row.get('path') == path
                                                 for row in events) <= 3,
        'native_download_done_for_real_source': task.get('kind') == 'download' and task.get('state') == 'done',
        'actual_full_media_hash_and_playability': bool(media) and media['source_id'] == fixture['source_id']
            and media['sha256'] == fixture['original_media_sha256'] and media['bytes'] > 0 and media['playable'],
        'cached_course_browsing_still_usable': course['id'] == fixture['course_id']
            and browsed.visible('#course-lessons'),
        'nonempty_student_content_preserved': before == _file_hashes(run, student_only=True)}
    result = {'passed': all(checks.values()), 'checks': checks,
              'source_id': fixture['source_id'], 'native_task_state': task.get('state'),
              'native_failure_id': task.get('failure_id', ''), 'student_file_count': len(before),
              'metadata_reads': receipt['metadata_reads'], 'expired_responses': receipt['expired_responses'],
              'native_media': media, 'actual_server_media_expiry_claimed': False}
    run.actions.append({'actual_native_media_expiry_recovery': result})
    browsed.screenshot('native-expired-media-recovery-and-cached-browse')
    return result


def run_notes(run):
    cfg = run.need("learning")
    _review_text_preflight(run, "learning.expected_summary_anchors")
    ui, course = _open_course(run, cfg["course_id"])
    workspace = _workspace(run, ui)
    projection = workspace.get("courses", {}).get(cfg["course_id"], {})
    root_ok = _page(run, workspace["id"], cfg["notion_test_parent_id"])[1]
    course_ok = _page(run, projection["id"], workspace["id"])[1]
    centers = projection.get("centers", {})
    centers_ok = {"每节课", "作业中心", "通知中心", "小测中心"} <= centers.keys()
    centers_ok = centers_ok and all(_page(run, row["id"], projection["id"])[1]
                                    for row in centers.values())
    lectures_ok = bool(projection.get("lectures"))
    for lecture in projection.get("lectures", {}).values():
        _, ok = _page(run, lecture["id"], centers["每节课"]["id"])
        text = unquote(json.dumps(_blocks(run, lecture["id"]), ensure_ascii=False))
        lectures_ok = lectures_ok and ok and str(lecture["source_id"]) in text
    run.check("home-course-center-lesson-parent-and-marker-readback",
              root_ok and course_ok and centers_ok and lectures_ok,
              "Independent paginated Notion parents and source marker readback")
    if not root_ok or not course_ok:
        raise AssertionError("Refuse native write into unverified test workspace")
    sentinel_before = _blocks(run, cfg["sentinel_page_id"])
    from product_e2e.catalog_journeys import _refresh_course
    refreshed = _refresh_course(ui, cfg["course_id"])
    if refreshed.get("state") != "done":
        raise AssertionError("Selected-course link repair did not complete")
    synced = _workspace(run, ui)
    sentinel_after = _blocks(run, cfg["sentinel_page_id"])
    repair = cfg.get("missing_link_fixture")
    if not repair:
        _block(run, "missing_link_fixture_absent", "Pre-provision a removed index link, not a missing page")
    index = _blocks(run, repair["index_page_id"])
    repaired = any(_identity(repair["lesson_id"]) in _identity(json.dumps(row)) for row in index)
    run.check("repeat-sync-and-missing-link-repair-preserve-student-content",
              synced["id"] == workspace["id"] and sentinel_before == sentinel_after and repaired,
              "Repeated native sync repairs fixture link and preserves student blocks")
    lesson = next(row for row in course["lessons"] if row["source_id"] == cfg["existing_summary_source_id"])
    note_id = lesson.get("summary_acceptance", {}).get("note_id")
    if not note_id:
        _block(run, "published_summary_fixture_missing", "Need a real published summary")
    note_rows = _blocks(run, note_id)
    acceptance = _markers(note_rows, "PKU 总结验收版本 · ")
    button = '[data-action="campus-open-lecture"][data-url="' + lesson["notion_note_url"] + '"]'
    _lesson_page(ui, lesson["id"])
    ui.click(button)
    external = run.external_page(lesson["notion_note_url"])
    external.wait(lambda: external.visible('[contenteditable="true"]'))
    run.check("summary-link-and-source-acceptance-correct",
              bool(acceptance) and acceptance[-1].get("source_id") == lesson["source_id"]
              and _identity(acceptance[-1].get("note_id")) == _identity(note_id)
              and bool(acceptance[-1].get("accepted_at")),
              "Opened real summary URL and independently matched acceptance identity")
    ui, course = _open_course(run, cfg["course_id"])
    short = next(row for row in course["lessons"] if row["id"] == cfg["short_recording_id"])
    _lesson_page(ui, short["id"])
    duration = short.get("duration_seconds")
    if not isinstance(duration, (int, float)) or duration <= 0:
        _block(run, "short_media_duration_unverified", "Read real source duration before cost authorization")
    run.authorize_cost(points=5, seconds=duration)
    before = _ledger(run)
    if short.get("video_available") is True:
        download = _download_cached_original(run, ui, short["id"])
    else:
        _confirm(run, ui, _action("recording-download", recording=short["id"]))
        download = _task(ui)
    media = _media_probe(run, short)
    run.check("explicit-original-download-correct-source-and-playable-on-windows",
              download.get("state") == "done" and media.get("source_id") == short["source_id"]
              and media.get("playable") is True and media.get("bytes", 0) > 0
              and media.get("sha256") == cfg["original_media_sha256"],
              "Native explicit download checked by actual file hash, probe and official source")
    if short.get("video_available") is True and (
            download.get("sha256") != cfg["original_media_sha256"] or download.get("bytes") != media.get("bytes")):
        raise AssertionError("Native cached original export differs from verified source")
    if media.get("sha256") != cfg["original_media_sha256"] or media.get("playable") is not True:
        raise AssertionError("Refuse paid processing of unverified source media")
    ui.click('[data-recording-direct-oss]')
    _confirm(run, ui, _action("campus-process", recording=short["id"]))
    processed = _task(ui, timeout=3600)
    processing_ledger = _ledger(run)
    updated = _read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe=""))
    result = next(row for row in updated["lessons"] if row["id"] == short["id"])
    target = [item for item in projection.get('lectures', {}).values() if item.get('source_id') == short['source_id']]
    if len(target) != 1:
        raise ResourceBlocked('published_note_unique_lecture_parent_missing')
    published_id = _published_note_id(run, result, target[0]['id'], cfg['course_id'])
    reviewed = _review_text(run, published_id, "learning.expected_summary_anchors")
    run.check("short-real-asr-and-summary-reviewed-and-published",
              processed.get("state") == "done" and bool(result.get("transcript_available"))
              and bool(result.get("notion_note_url")) and reviewed,
              "Actual published summary contextually reviewed against original material and frozen source facts")
    _confirm(run, ui, _action("campus-accept-summary", recording=short["id"]))
    run.restart("primary")
    ui, _ = _open_course(run, cfg["course_id"])
    # Completed work is reopened rather than clicking "regenerate", which is
    # explicitly a new paid operation and is not an idempotent retry.
    _lesson_page(ui, short["id"])
    ui.click('[data-action="campus-open-lecture"][data-url="' + result["notion_note_url"] + '"]')
    run.external_page(result["notion_note_url"])
    after = _ledger(run)
    settlements = _new_rows(before, after, "settlements")
    asr_operations = [row.get('operation_id') for row in settlements
                      if row.get('charged_seconds') is not None]
    run.check("retry-reuses-artifacts-one-settlement-and-oss-audio-cleanup",
              _verified_processing_charges(before, processing_ledger, after,
                                           short["source_id"], cfg["course_id"], duration)
              and not any(row.get("source_id") == short["source_id"] for row in after["oss_objects"])
              and verified_audio_cleanup(before, after, short['source_id'], asr_operations),
              "Exactly one settlement per source-bound ASR/model job, unchanged on reopen, plus fresh OSS HEAD 404")
    media_recovery = []
    def retry(variant):
        active, current = _open_course(run, cfg["course_id"])
        if variant == 'expired-media-url':
            fixture = cfg['expired_media_fixture']
            selected = [row for row in current['lessons'] if row['id'] == fixture['recording_id']
                        and row['source_id'] == fixture['source_id']]
            if len(selected) != 1:
                raise ResourceBlocked('expired_media_unique_native_source_missing')
            recovered = _expired_media_download(run, active, selected[0])
            media_recovery.append(recovered)
            if not recovered['passed']:
                raise AssertionError('native_expired_media_recovery_failed')
            return
        if variant == 'permission-archive-move':
            results = _notion_access_faults(run, active, workspace['id'], workspace['parent_id'])
            if not all(row['passed'] for row in results):
                raise AssertionError('native_notion_access_subcase_failed')
            return
        if variant == "restart-after-charge":
            run.restart("primary")
            active, _ = _open_course(run, cfg["course_id"])
        _refresh_course(active, cfg["course_id"])
    faults = _faults(run, retry, lambda variant, before, after, receipt:
                     bool(receipt.get("observed")) and not _new_rows(before, after, "settlements")
                     and _blocks(run, cfg["sentinel_page_id"]) == sentinel_before,
                     restart_operation_ids=[row['operation_id'] for row in _new_rows(before, processing_ledger, 'settlements')])
    denied = cfg.get("permission_fixture")
    if not denied:
        _block(run, "permission_fixture_absent", "Need independently denied/archived/moved real pages")
    denied_ui = run.external_page(denied["url"])
    denied_ui.wait(lambda: denied_ui.visible(denied["actionable_selector"]))
    run.check("permission-archive-move-and-reauth-actionable",
              denied_ui.visible(denied["actionable_selector"])
              and (run.mode != "fault" or all(faults)), "Actual permission fixture and recovery evidence")
    if run.mode == 'fault':
        run.check("expired-media-address-refresh-bounded-and-browsing-usable",
                  len(media_recovery) == 1 and media_recovery[0]['passed'],
                  "Actual source-bound HTTP expiry, one fresh metadata recovery, full file hash and native browse")
        ui.screenshot("notes-download-and-recovery")
        return
    expired = cfg.get("expired_media_fixture")
    if not expired:
        _block(run, "expired_media_fixture_absent", "Need a real expired address, not mocked success")
    ui, _ = _open_course(run, cfg["course_id"])
    _lesson_page(ui, expired["recording_id"])
    _confirm(run, ui, _action("recording-download", recording=expired["recording_id"]))
    refreshed = _task(ui)
    attempts = [row for row in run.network if row.get("path", "").endswith("/download")]
    run.check("expired-media-address-refresh-bounded-and-browsing-usable",
              refreshed.get("kind") == "download" and refreshed.get("state") == "done" and 1 <= len(attempts) <= 3
              and ui.visible("#course-lessons"), "Expired real address refresh bounded by captured requests")
    ui.screenshot("notes-download-and-recovery")


def _prepare(run, ui, cfg):
    ui.click(_action("campus-prepare-assignment", assignment=cfg["content_id"]))
    run.authorize_cost(points=5)
    path = '/api/campus/courses/' + cfg['course_id'] + '/assignments/' + cfg['content_id'] + '/requirements'
    with ui.page.expect_response(lambda response: response.request.method == 'POST'
            and response.url.endswith(path), timeout=720000) as captured:
        _confirm(run, ui, _action('campus-analyze-requirements'))
    ui.wait(lambda: ui.visible('[data-assignment-requirements]')
            or ui.visible('.assignment-preparation [role="alert"]'), timeout=720)
    if not ui.visible('[data-assignment-requirements]'):
        ui.screenshot('native-requirements-failed-before-preview')
        raise AssertionError('native_source_grounded_requirements_not_available')
    ui.fill('[name="notion_url"]', cfg["notion_url"])
    ui.fill('[name="archive_name"]', cfg["archive_name"])
    if cfg.get("code_filename"):
        ui.fill('[name="code_filename"]', cfg["code_filename"])
    _capability(run, ui, "upload")('[name="attachments"]', cfg["attachments"])
    ui.click(_action("campus-build-draft"))
    ui.wait(lambda: ui.visible("#assignment-reviewed"), timeout=90)
    draft = _draft_probe(run, cfg["course_id"], cfg["content_id"])
    response = captured.value
    if response.status != 200:
        raise AssertionError('Native requirements response was not successful')
    draft['native_requirements_response'] = response.json()
    return draft


def _assignment_requirements_verified(draft, official, receipt, before, after,
                                      displayed_current, displayed_original, quotes):
    """Correlate actual UI response with the selected source and one real charge.

    A reused result must already have the identical independently read job and
    settlement before this action; it cannot create a new charge. Comparisons
    use exact decimal values, never rounded UI/account totals or other jobs.
    """
    def points(value, *, display=False):
        if display:
            if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?', value):
                raise ValueError('invalid visible points')
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('invalid recorded points')
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError('invalid points')
        return amount

    try:
        if (type(receipt.get('schema')) is not int or receipt['schema'] != 1
                or type(receipt.get('reused')) is not bool
                or receipt.get('source_grounding_verified') is not True
                or any(receipt.get(key) != draft[key] for key in
                       ('course_id', 'content_id', 'requirements_sha256'))
                or receipt.get('official_instructions') != official['instructions']
                or receipt.get('due_at') != official['due_at']
                or not re.fullmatch('[0-9a-f]{64}', receipt.get('operation_id', ''))
                or before.get('complete') is not True or after.get('complete') is not True):
            return False
        analysis = receipt['analysis']
        if set(analysis) != {'requirements', 'filename_rules', 'file_formats', 'uncertainties'}:
            return False
        for field in ('requirements', 'filename_rules', 'file_formats'):
            rows = analysis[field]
            if not isinstance(rows, list) or len(rows) > 30:
                return False
            for row in rows:
                expected = {'quote', 'value'} if field == 'file_formats' else {'quote'}
                quote = row['quote']
                if (set(row) != expected or not isinstance(quote, str) or not quote.strip()
                        or len(quote) > 2000 or quote not in official['instructions']):
                    return False
                if field == 'file_formats' and (not isinstance(row['value'], str)
                        or not row['value'] or len(row['value']) > 40
                        or row['value'].casefold() not in quote.casefold()):
                    return False
        uncertainties = analysis['uncertainties']
        if (not isinstance(uncertainties, list) or len(uncertainties) > 20
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500
                       for item in uncertainties)
                or not analysis['requirements'] and not uncertainties):
            return False
        if quotes != [row['quote'] for row in analysis['requirements'] + analysis['filename_rules']]:
            return False
        matching = [row for row in after['jobs']
                    if row.get('client_operation_id') == receipt['operation_id']]
        if len(matching) != 1:
            return False
        job = matching[0]
        if (not isinstance(job.get('id'), str) or not job['id']
                or job.get('operation_id') != job['id'] or job.get('status') != 'succeeded'
                or job.get('purpose') != 'assignment-requirements'
                or job.get('course_id') != draft['course_id']
                or job.get('content_id') != draft['content_id']
                or job.get('source_id') != draft['requirements_sha256']):
            return False
        settlements = [row for row in after['settlements'] if row.get('operation_id') == job['operation_id']]
        if len(settlements) != 1 or not isinstance(settlements[0].get('id'), str) or not settlements[0]['id']:
            return False
        charge = points(receipt['receipt_points_charged'])
        if points(job['points_charged']) != charge or points(settlements[0]['charged_points']) != charge:
            return False
        current = points(receipt['points_charged'])
        if (points(displayed_current, display=True) != current
                or points(after['charged_points']) - points(before['charged_points']) != current):
            return False
        prior_jobs = [row for row in before['jobs'] if row.get('client_operation_id') == receipt['operation_id']]
        prior_settlements = [row for row in before['settlements'] if row.get('operation_id') == job['operation_id']]
        if receipt['reused']:
            return (current == 0 and points(displayed_original, display=True) == charge
                    and prior_jobs == matching and prior_settlements == settlements)
        return current == charge and displayed_original is None and not prior_jobs and not prior_settlements
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return False


def _official_submission(run, cfg, require_history=True):
    return _capability(run, run.oracle, "portal_assignment")(cfg["course_id"], cfg["content_id"],
                                                            require_history=require_history)


def _unconfirmed_trial(run, ui, *, double_click=False):
    if ui.is_checked("#assignment-reviewed"):
        raise AssertionError("Draft was already confirmed before the no-submit trial")
    with _capability(run, ui, "guard_submission")() as attempts:
        ui.prepare_dialog(False)
        if double_click:
            ui.double_click(_action("campus-submit-assignment"))
        else:
            ui.click(_action("campus-submit-assignment"))
        ui.wait(lambda: "勾选确认" in ui.text("#course-assignments"))
    return not attempts and not ui.is_checked("#assignment-reviewed")


def _rejected_draft_download(run, ui, draft):
    """Click the actual installed download action while a draft fault is armed.

    Require both the native preflight rejection and a separate process-owned
    file GET rejection. The probe never saves response bytes or submits work.
    """
    check_path = '/api/campus/submission-drafts/' + draft['draft_id'] + '/check'
    file_path = '/api/campus/submission-drafts/' + draft['draft_id'] + '/file'
    selector = 'a[download][data-action="campus-download-draft"]'
    if ui.page.locator(selector).get_attribute('href') != file_path:
        raise AssertionError('Native download does not reference this exact prepared draft')
    with ui.guard_submission() as attempts:
        with ui.page.expect_response(lambda response: response.request.method == 'GET'
                and urlsplit(response.url).path == check_path, timeout=30000) as captured:
            ui.click(selector)
        native_status = captured.value.status
        ui.wait(lambda: ui.visible('.assignment-preparation [role="alert"]'))
        native_error = '预览' in ui.text('.assignment-preparation [role="alert"]')
        independent = httpx.get(ui.origin + file_path, timeout=20, trust_env=False)
    observation = {'boundary': 'owned-installed-native-download-preflight-and-separate-file-GET',
                   'draft_id': draft['draft_id'], 'native_http_status': native_status,
                   'independent_http_status': independent.status_code,
                   'independent_response_sha256': hashlib.sha256(independent.content).hexdigest(),
                   'native_action_error_visible': native_error,
                   'submission_attempts': len(attempts),
                   'passed': native_status == independent.status_code == 409 and native_error and not attempts,
                   'normal_OS_save_download_verified': False}
    run.actions.append({'native_changed_draft_download_rejection': observation})
    ui.screenshot('native-changed-draft-download-rejection')
    return observation


def run_assignment(run):
    cfg = run.need("assignment")
    preview_only = (run.context["target"] == "windows" and
                    "designated-test-assignment-official-history-and-file-readback" not in run.scenario["assertions"])
    if not preview_only:
        authority = run.need("submission_authority")
        if (authority.get("approved") is not True or authority.get("course_id") != cfg["course_id"]
                or authority.get("content_id") != cfg["content_id"] or authority.get("resubmittable") is not True):
            _block(run, "designated_resubmittable_assignment_not_approved", "No real submission authority")
    ui, course = _open_course(run, cfg["course_id"])
    target = next(row for row in course["assignments"] if row["content_id"] == cfg["content_id"])
    official_before = _official_submission(run, cfg, require_history=not preview_only)
    fields = ("course_id", "content_id", "source_id", "due_at", "instructions")
    target_identity = {**target, "course_id": course["id"]}
    run.check("official-target-and-deadline-not-title-only",
              all(target_identity.get(key) == official_before.get(key) for key in fields)
              and target.get("source") != "calendar", "Independent portal source, target and requirements")
    if not all(target_identity.get(key) == official_before.get(key) for key in fields):
        raise AssertionError("Refuse native preparation for mismatched official target")
    unmatched = [row for row in course["assignments"] if row.get("source") == "calendar"]
    historical = _capability(run, run.oracle, "historical_assignment")(cfg["course_id"])
    blocked = all(not ui.visible(_action("campus-prepare-assignment", assignment=row["content_id"]))
                  for row in unmatched if row.get("content_id"))
    run.check("unmatched-and-historical-work-cannot-submit",
              blocked and historical.get("read_only") is True,
              "Actual calendar controls and independently identified historical target")
    before = _ledger(run)
    draft = _prepare(run, ui, cfg)
    preview = _preview(run, draft, official_before, cfg)
    after_analysis = _ledger(run)
    displayed = ui.text('[data-assignment-requirements]')
    current_cost = re.search(r'本次用量：\s*([0-9]+(?:\.[0-9]+)?)\s*AI 点', displayed)
    original_cost = re.search(r'原任务用量：\s*([0-9]+(?:\.[0-9]+)?)\s*AI 点', displayed)
    ai_requirements = _assignment_requirements_verified(draft, official_before,
        draft.get('native_requirements_response', {}), before, after_analysis,
        current_cost.group(1) if current_cost else None,
        original_cost.group(1) if original_cost else None,
        ui.page.locator('[data-assignment-requirements] li').all_text_contents())
    run.check("ai-requirements-and-preview-file-content-and-hash-verified",
              ai_requirements and preview.get("source_requirements_match") is True and preview.get("content_match") is True
              and preview.get("sha256") == draft["sha256"],
              "Actual archive members/hash and source-grounded requirements review")
    if not ai_requirements or not preview["source_requirements_match"] or not preview["content_match"]:
        raise AssertionError("Refuse real submission with failed source/file/requirements verification")
    unchecked = _unconfirmed_trial(run, ui)
    no_confirm = _ledger(run)
    run.check("no-submission-before-explicit-confirmation",
              unchecked and not _new_rows(before, no_confirm, "submission_posts")
              and (preview_only or _official_submission(run, cfg).get("history") == official_before.get("history")),
              "Unchecked action produced no UI POST or ledger submission; full mode also checks portal history")
    if not unchecked or _new_rows(before, no_confirm, "submission_posts"):
        raise AssertionError("Unconfirmed native action attempted a real submission")
    if preview_only:
        run.actions.append({"event": "official_submission_deferred", "status": "deferred-unverified"})
        draft_faults = {'changed-file', 'changed-target', 'changed-deadline'}
        download_observations = {}
        def prepare_fault(variant):
            if variant == 'double-click':
                if not _unconfirmed_trial(run, ui, double_click=True):
                    raise AssertionError('Unconfirmed double click attempted submission')
                return
            if variant in draft_faults:
                download_observations[variant] = _rejected_draft_download(run, ui, draft)
                return
            _prepare(run, ui, cfg)
        run.context['_draft_fault_fixture'] = draft
        try:
            _faults(run, prepare_fault, lambda variant, old, new, receipt:
                    bool(receipt.get("observed")) and not _new_rows(old, new, "submission_posts")
                    and (variant not in draft_faults or (receipt.get('restored') is True
                         and download_observations[variant]['passed'] is True
                         and not _new_rows(old, new, 'jobs') and not _new_rows(old, new, 'settlements')))
                    and _preview(run, _draft_probe(run, cfg["course_id"], cfg["content_id"]),
                                 _official_submission(run, cfg, require_history=False), cfg)["content_match"])
        finally:
            run.context.pop('_draft_fault_fixture', None)
        ui.screenshot("assignment-preview-no-official-submission")
        return
    ui.click("#assignment-reviewed")
    _confirm(run, ui, _action("campus-submit-assignment"))
    ui.wait(lambda: _official_submission(run, cfg).get("history") != official_before.get("history"), timeout=120)
    verified = _official_submission(run, cfg)
    history = verified.get("history", [])
    run.check("designated-test-assignment-official-history-and-file-readback",
              bool(history) and history[-1].get("content_id") == cfg["content_id"]
              and history[-1].get("file_sha256") == draft["sha256"],
              "Independent portal history and uploaded file bytes after explicit native confirmation")
    def submit_fault(variant):
        active, _ = _open_course(run, cfg["course_id"])
        if variant in {"two-devices", "restart-pending-submission"}:
            run.restart("secondary" if variant == "two-devices" else "primary")
            active, _ = _open_course(run, cfg["course_id"], "secondary" if variant == "two-devices" else "primary")
        _prepare(run, active, cfg)
        active.click("#assignment-reviewed")
        _confirm(run, active, _action("campus-submit-assignment"))
        active.wait(lambda: active.visible("[role=alert]") or active.visible(".submission-receipts"))
    faults = _faults(run, submit_fault, lambda variant, old, new, receipt:
                     bool(receipt.get("observed")) and not _new_rows(old, new, "submission_posts")
                     and _official_submission(run, cfg).get("history") == history)
    if run.mode != "fault":
        unsafe = cfg.get("unsafe_attempts")
        if not unsafe:
            _block(run, "changed_draft_fixture_required", "Need real pending/changed-draft native recovery fixtures")
        for fixture in unsafe:
            active, _ = _open_course(run, cfg["course_id"], fixture["instance"])
            pre_attempt = _ledger(run)
            _confirm(run, active, fixture["selector"])
            active.wait(lambda active=active: active.visible("[role=alert]"))
            faults.append(not _new_rows(pre_attempt, _ledger(run), "submission_posts"))
    run.check("unknown-result-and-changed-draft-block-repeat-post", bool(faults) and all(faults),
              "Real boundary loss and changed file/target/deadline produced no repeated submission")
    after = _ledger(run)
    run.check("two-devices-one-submission-and-recoverable-receipt",
              len(_new_rows(before, after, "submission_posts")) == 1 and len(history) == len(official_before.get("history", [])) + 1,
              "Both installed devices and one actual portal POST/history receipt")
    secondary, _ = _open_course(run, cfg["course_id"], "secondary")
    secondary.click(_action("campus-assignment-status", assignment=cfg["content_id"]))
    secondary.wait(lambda: "已提交" in secondary.text("#course-assignments"))
    run.check("another-device-reads-official-state-without-resubmission",
              not _new_rows(after, _ledger(run), "submission_posts")
              and _official_submission(run, cfg).get("history") == history,
              "Second installed device reads actual portal submission status without resending")
    secondary.screenshot("assignment-official-state-second-device")


def _quiz_questions(rows):
    questions, answers = [], []
    for row in rows:
        text = _plain(row)
        if text == "教师区（答案）":
            raise AssertionError("Answer key leaked into editable student page")
        if row.get("type") == "heading_3":
            match = re.fullmatch(r"(\d+)\.\s*(选择|判断|填空|简答|论述)", text.strip())
            if match:
                questions.append((int(match[1]), match[2]))
        elif text.startswith("答案："):
            answers.append(row)
    return questions, answers


def _changed_frozen_quiz_input(run, ui, cfg):
    """One actual native generate POST, a real source append and no stale publication."""
    from product_e2e.catalog_journeys import _file_hashes
    course = _read(ui, '/api/campus/courses/' + quote(cfg['course_id'], safe=''))
    selected = [row for row in course['lessons'] if row['source_id'] == cfg['source_ids'][0]]
    if len(selected) != 1:
        raise ResourceBlocked('frozen_input_unique_official_native_lesson_required')
    accepted = selected[0].get('summary_acceptance') or {}
    if not accepted.get('accepted') or not accepted.get('note_id'):
        raise ResourceBlocked('frozen_input_real_accepted_summary_required')
    workspace = _workspace(run, ui)
    projection = workspace['courses'][cfg['course_id']]
    notes = [row for row in projection['lectures'].values() if row['source_id'] == selected[0]['source_id']]
    if len(notes) != 1 or _note_fingerprint(run, accepted['note_id']) != accepted.get('remote_sha256'):
        raise ResourceBlocked('frozen_input_original_acceptance_independent_readback_required')
    center = projection['centers']['小测中心']['id']
    before_center = _blocks(run, center)
    before_ledger = _ledger(run)
    student = _file_hashes(run, student_only=True)
    if not student:
        raise ResourceBlocked('frozen_input_nonempty_student_files_required')
    fixture = {'course_id': cfg['course_id'], 'source_id': selected[0]['source_id'],
        'note_id': accepted['note_id'], 'lecture_id': notes[0]['note_parent_id'],
        'quiz_center_id': center, 'course_parent_id': projection['id'],
        'remote_sha256': accepted['remote_sha256']}
    controller = run._notion_boundary.configure_frozen_input(fixture)
    path = '/api/campus/courses/' + quote(cfg['course_id'], safe='') + '/quizzes'
    def on_request(request):
        if request.method == 'POST' and urlsplit(request.url).path == path:
            controller.arm_native_request(request.method, request.url, json.loads(request.post_data or '{}'))
    start = len(run.network)
    ui.page.on('request', on_request)
    try:
        with ui.page.expect_response(lambda response: response.request.method == 'POST'
                and urlsplit(response.url).path == path, timeout=180000) as captured:
            _confirm(run, ui, _action('campus-generate-quiz'))
        response = captured.value
        response.finished()
    finally:
        ui.page.remove_listener('request', on_request)
    if response.status >= 400:
        ui.wait(lambda: ui.visible('[role="alert"]'), timeout=30)
    message = ui.text('[role="alert"]') if ui.visible('[role="alert"]') else ''
    after_ledger = _ledger(run)
    receipt = controller.receipt
    changed_rows = _blocks(run, accepted['note_id'])
    events = run.network[start:]
    checks = {'actual_native_generate_post_observed': receipt['native_generate_post_observed'],
        'real_frozen_source_changed_and_independently_read': receipt['actual_source_changed']
            and receipt.get('generated_block_id') in {_identity(row['id']) for row in changed_rows}
            and _note_fingerprint(run, accepted['note_id']) != accepted['remote_sha256'],
        'exactly_one_real_source_append': receipt['append_attempts'] == 1
            and len([row for row in events if row.get('boundary') == 'actual-frozen-input-mutation'
                     and row.get('method') == 'PATCH' and row.get('upstream_status') == 200]) == 1,
        'native_rejects_stale_frozen_input': response.status == 409,
        'native_alert_has_next_action': bool(message) and any(word in message for word in ('请重新', '请核对', '请恢复')),
        'no_quiz_created_or_accepted_at_changed_source': before_center == _blocks(run, center)
            and not any(row.get('boundary') == 'notion' and row.get('method') in {'PATCH', 'DELETE'}
                        or row.get('boundary') == 'notion' and row.get('method') == 'POST'
                           and row.get('path') != '/v1/search' for row in events),
        'no_additional_model_job_or_settlement': all(before_ledger[key] == after_ledger[key]
            for key in ('jobs', 'settlements', 'submission_posts', 'charged_points', 'charged_seconds')),
        'nonempty_student_content_preserved': student == _file_hashes(run, student_only=True)}
    result = {'passed': all(checks.values()), 'checks': checks, 'native_status': response.status,
        'source_id': fixture['source_id'], 'native_alert_sha256': hashlib.sha256(message.encode()).hexdigest(),
        'student_file_count': len(student), 'original_source_sha256': fixture['remote_sha256'],
        'changed_source_sha256': receipt.get('changed_source_sha256')}
    run.actions.append({'actual_native_frozen_input_rejection': result})
    ui.screenshot('native-changed-frozen-source-rejected-before-publication')
    return result


def _validate_reviewed_quiz_answers(value, questions, quiz_id, questions_sha, source_ids, facts_sha):
    """Independent pre-grade oracle; never infer expectations from model grades."""
    try:
        if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in (
                ('quiz_id', quiz_id), ('exact_questions_sha256', questions_sha),
                ('source_ids', list(source_ids)), ('source_facts_sha256', facts_sha))):
            raise ValueError
        if len(questions) != 5 or [q['number'] for q in questions] != list(range(1, 6)) \
                or {q['type'] for q in questions} != {'选择', '判断', '填空', '简答', '论述'}:
            raise ValueError
        answers = value['answer_texts']
        if not isinstance(answers, list) or len(answers) != 5 or not all(
                isinstance(answer, str) and answer.strip() and len(answer) <= 20000 for answer in answers):
            raise ValueError
        objective = {str(q['number']) for q in questions if q['type'] in {'选择', '判断', '填空'}}
        subjective = {str(q['number']) for q in questions if q['type'] in {'简答', '论述'}}
        scores, bounds = value['objective_expected'], value['subjective_bounds']
        def finite_score(number):
            return isinstance(number, (int, float)) and not isinstance(number, bool) \
                and math.isfinite(number) and 0 <= number <= 20
        if not isinstance(scores, dict) or set(scores) != objective \
                or not all(finite_score(score) for score in scores.values()):
            raise ValueError
        if not isinstance(bounds, dict) or set(bounds) != subjective or not all(
                isinstance(bound, list) and len(bound) == 2 and all(finite_score(n) for n in bound)
                and bound[0] <= bound[1] and bound != [0, 20] for bound in bounds.values()):
            raise ValueError
        anchors = value['expected_question_anchors']
        body = '\n'.join(q['question'] + '\n' + q['source'] for q in questions)
        if not isinstance(anchors, list) or not anchors or not all(
                isinstance(anchor, str) and anchor.strip() and anchor in body for anchor in anchors):
            raise ValueError
        for key in ('changed_answer', 'review_note'):
            if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 20000:
                raise ValueError
        if value['changed_answer'] == answers[0]:
            raise ValueError
        return {key: value[key] for key in ('answer_texts', 'objective_expected', 'subjective_bounds',
                'expected_question_anchors', 'changed_answer', 'review_note')}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ResourceBlocked('independent_quiz_answer_oracle_binding_invalid') from None


def _reviewed_quiz_questions(rows):
    """Hash only the student questions, preserving IDs and rich text, excluding editable answers."""
    _quiz_questions(rows)  # Retain the original answer-key leakage guard.
    questions, current = [], None
    for row in rows:
        text = _plain(row)
        if row.get('type') == 'heading_2' and text == '学生上传的往年题参考' \
                or text.startswith('PKU 小测生成验证 · '):
            current = None
        match = re.fullmatch(r'(\d+)\.\s*(选择|判断|填空|简答|论述)', text.strip()) \
            if row.get('type') == 'heading_3' else None
        if match:
            current = {'number': int(match[1]), 'type': match[2], 'heading_id': row['id'],
                       'question': '', 'source': '', 'question_blocks': [], 'answer_block_id': ''}
            questions.append(current)
        elif current is not None:
            if text.startswith('答案：'):
                if current['answer_block_id']:
                    raise ResourceBlocked('independent_quiz_question_structure_invalid')
                current['answer_block_id'] = row['id']
            else:
                kind = row.get('type')
                current['question_blocks'].append({'id': row['id'], 'type': kind, 'body': row.get(kind)})
                if text.startswith('来源：'):
                    if current['source']:
                        raise ResourceBlocked('independent_quiz_question_structure_invalid')
                    current['source'] = text[len('来源：'):].strip()
                else:
                    current['question'] += ('\n' if current['question'] else '') + text
    if len(questions) != 5 or [q['number'] for q in questions] != list(range(1, 6)) \
            or {q['type'] for q in questions} != {'选择', '判断', '填空', '简答', '论述'} \
            or any(not q['question'].strip() or not q['source'] or not q['answer_block_id'] for q in questions):
        raise ResourceBlocked('independent_quiz_question_structure_invalid')
    digest = hashlib.sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return questions, digest


def _safe_review_file(path_value, private_root=None):
    try:
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise ValueError
        path = Path(path_value)
        for ancestor in (path, *path.parents):
            if ancestor.exists() or ancestor.is_symlink():
                info = ancestor.lstat()
                if ancestor.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError
        resolved = path.resolve()
        if private_root is not None:
            root = Path(private_root).resolve()
            if resolved == root or not resolved.is_relative_to(root):
                raise ValueError
        return resolved
    except (OSError, ValueError, TypeError):
        raise ResourceBlocked('independent_quiz_review_path_not_isolated') from None


def _reviewed_quiz_preflight(run, cfg):
    if not cfg.get('reviewed_answers_path'):
        return None
    path = _safe_review_file(cfg['reviewed_answers_path'], run.need('test_root'))
    for instance in run.config.get('instances', {}).values():
        if instance.get('app_dir') and path.is_relative_to(Path(instance['app_dir']).resolve()):
            raise ResourceBlocked('independent_quiz_review_path_not_isolated')
    facts_path = _safe_review_file(cfg.get('source_facts_path'))
    try:
        raw = facts_path.read_bytes()
        facts = json.loads(raw)
        if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != cfg.get('source_facts_sha256') \
                or facts.get('course_id') != cfg['course_id'] \
                or cfg['source_ids'] != [facts.get('source_id')]:
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise ResourceBlocked('independent_quiz_pre_model_source_facts_invalid') from None
    return {'path': str(path), 'facts_path': str(facts_path), 'facts_sha': cfg['source_facts_sha256'],
            'reused': path.exists()}


def _reviewed_quiz_answers(run, ui, cfg, quiz, rows, binding):
    questions, digest = _reviewed_quiz_questions(rows)
    review_input = {'quiz_id': quiz['id'], 'exact_questions_sha256': digest,
                    'source_ids': cfg['source_ids'], 'source_facts_sha256': binding['facts_sha'],
                    'questions': questions, 'teacher_answer_key_read': False}
    with (run.case_dir / 'quiz-question-review-input.json').open('x', encoding='utf-8') as handle:
        json.dump(review_input, handle, ensure_ascii=False, indent=2)
    path = Path(binding['path'])
    deadline = time.monotonic() + 600
    ui.wait(lambda: _safe_review_file(str(path), run.need('test_root')).is_file(), timeout=600)
    try:
        raw = path.read_bytes()
        if time.monotonic() > deadline or len(raw) > 131072:
            raise ValueError
        value = json.loads(raw)
        overrides = _validate_reviewed_quiz_answers(value, questions, quiz['id'], digest,
                                                    cfg['source_ids'], binding['facts_sha'])
        if raw != path.read_bytes() or time.monotonic() > deadline:
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ResourceBlocked('independent_quiz_review_file_unstable_or_invalid') from None
    binding.update({'sha256': hashlib.sha256(raw).hexdigest(), 'quiz_id': quiz['id'],
                    'questions_sha256': digest})
    run.actions.append({'event': 'independent_quiz_answers_frozen', 'quiz_id': quiz['id'],
                        'exact_questions_sha256': digest, 'source_facts_sha256': binding['facts_sha'],
                        'answers_sha256': binding['sha256'], 'reused': binding['reused']})
    return {**cfg, **overrides}


def _assert_reviewed_quiz_unchanged(run, cfg, quiz, binding):
    if binding is None:
        return
    try:
        path = _safe_review_file(binding['path'], run.need('test_root'))
        facts = _safe_review_file(binding['facts_path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256'] \
                or hashlib.sha256(facts.read_bytes()).hexdigest() != binding['facts_sha'] \
                or _reviewed_quiz_questions(_blocks(run, quiz['id']))[1] != binding['questions_sha256']:
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ResourceBlocked('independent_quiz_frozen_review_changed') from None


def run_quiz(run):
    cfg = run.need("quiz")
    review_binding = _reviewed_quiz_preflight(run, cfg)
    if review_binding is None:
        _review_text_preflight(run, "quiz.expected_question_anchors")
    ui, course = _open_course(run, cfg["course_id"], center="quizzes")
    available = _read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/quizzes")
    sources = {row["id"]: row for row in available["lectures"]}
    stable = True
    for source_id in cfg["source_ids"]:
        lesson = next(row for row in course["lessons"] if row["source_id"] == source_id)
        marker = lesson.get("summary_acceptance", {})
        remote = _markers(_blocks(run, marker["note_id"]), "PKU 总结验收版本 · ")
        stable = stable and sources[source_id].get("accepted") is True and bool(remote)
        stable = stable and remote[-1].get("remote_sha256") == marker.get("remote_sha256")
        stable = stable and remote[-1].get("remote_sha256") == _note_fingerprint(run, marker["note_id"])
        ui.click('[data-campus-quiz-source="' + source_id + '"]')
    if not stable:
        run.check("only-selected-accepted-stable-summary-sources", False, "Independent published summary version changed")
        raise AssertionError("Refuse paid quiz from an unaccepted or changed summary")
    if review_binding is not None:
        # Recheck the facts immediately before cost authority, after service reads.
        _reviewed_quiz_preflight(run, cfg)
    run.authorize_cost(points=10, seconds=0)
    before = _ledger(run)
    ui.click("#course-quizzes details > summary")
    ui.fill("[data-campus-quiz-paper]", cfg["prior_paper"])
    _confirm(run, ui, _action("campus-generate-quiz"))
    path = "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/quizzes"
    ui.wait(lambda: any(set(row.get("source_ids", [])) == set(cfg["source_ids"])
                        for row in _read(ui, path).get("items", [])), timeout=180)
    quiz = next(row for row in _read(ui, path)["items"] if set(row.get("source_ids", [])) == set(cfg["source_ids"]))
    rows = _blocks(run, quiz["id"])
    marker = _markers(rows, "PKU 课程小测身份 · ")
    questions, answers = _quiz_questions(rows)
    if review_binding is not None:
        cfg = _reviewed_quiz_answers(run, ui, cfg, quiz, rows, review_binding)
    run.check("only-selected-accepted-stable-summary-sources",
              stable and bool(marker) and set(marker[-1].get("source_ids", [])) == set(cfg["source_ids"]),
              "Independent accepted summary versions and generated Notion source identities")
    grounded = (_review_text(run, quiz["id"], "quiz.expected_question_anchors") if review_binding is None else
                all(anchor in '\n'.join(_plain(row) for row in _blocks(run, quiz['id']))
                    for anchor in cfg['expected_question_anchors']))
    run.check("choice-blank-short-answer-and-reference-grounding",
              {kind for _, kind in questions} >= {"选择", "填空", "简答"}
              and cfg["prior_paper"] in "\n".join(_plain(row) for row in rows)
              and grounded, "Question types and pre-authored source fact anchors independently checked")
    ui.click('[data-action="campus-open-summary"][data-url="' + quiz["url"] + '"]')
    notion = run.external_page(quiz["url"])
    _assert_reviewed_quiz_unchanged(run, cfg, quiz, review_binding)
    editable = []
    for row, answer in zip(answers, cfg["answer_texts"], strict=True):
        selector = '[data-block-id="' + row["id"] + '"] [contenteditable="true"]'
        notion.wait(lambda selector=selector: notion.visible(selector))
        editable.append(notion.attr(selector, "contenteditable") == "true")
        notion.fill(selector, "答案：" + answer)
    run.check("notion-editable-answers-without-answer-key-leak", all(editable) and len(answers) == 5,
              "Real Notion answer blocks editable; no teacher key in independent student page")
    expected_answers = ["答案：" + value for value in cfg["answer_texts"]]
    notion.wait(lambda: [_plain(row) for row in _quiz_questions(_blocks(run, quiz["id"]))[1]] == expected_answers)
    notion = run.external_page(quiz["url"])
    single_machine = run.scenario.get("device_scope") == "single-machine"
    if single_machine:
        run.restart("primary")
        reopened, _ = _open_course(run, cfg["course_id"], center="quizzes")
        reopened.click('[data-action="campus-open-summary"][data-url="' + quiz["url"] + '"]')
        run.external_page(quiz["url"])
        run.check("answers-survive-reopen-and-local-restart",
                  [_plain(row) for row in _quiz_questions(_blocks(run, quiz["id"]))[1]] == expected_answers,
                  "Reopened after actual primary restart")
    else:
        secondary, _ = _open_course(run, cfg["course_id"], "secondary", center="quizzes")
        secondary.click('[data-action="campus-open-summary"][data-url="' + quiz["url"] + '"]')
        run.external_page(quiz["url"])
        run.check("answers-survive-reopen-and-cross-device-read",
                  [_plain(row) for row in _quiz_questions(_blocks(run, quiz["id"]))[1]] == expected_answers,
                  "Reopened via both installed devices")
    ui, _ = _open_course(run, cfg["course_id"], center="quizzes")
    _assert_reviewed_quiz_unchanged(run, cfg, quiz, review_binding)
    _confirm(run, ui, _action("campus-grade-quiz", quiz=quiz["id"]))
    reviews_path = "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/quiz-reviews"
    ui.wait(lambda: bool(_read(ui, reviews_path).get("histories")), timeout=180)
    histories = _read(ui, reviews_path)["histories"]
    history = next(row for row in histories if row.get("quiz_id") == quiz["id"])
    history_rows = _blocks(run, history["id"])
    grade_correct = _grade_check(run, history["id"], cfg)
    run.check("objective-oracle-and-bounded-subjective-grade",
              grade_correct,
              "Independent fixture answers and declared subjective rubric checked against real grade")
    history_text = "\n".join(_plain(row) for row in history_rows)
    ui.click(_action("campus-record-review", review=history["id"]))
    _capability(run, ui, "prompt")(cfg["review_note"])
    ui.wait(lambda: cfg["review_note"] in "\n".join(_plain(row) for row in _blocks(run, history["id"])))
    run.check("original-answers-history-and-review-content-preserved",
              all(value in history_text for value in cfg["answer_texts"])
              and all(value in "\n".join(_plain(row) for row in _blocks(run, history["id"]))
                      for value in (*cfg["answer_texts"], cfg["review_note"])),
              "Real Notion history preserves original answers and appended review")
    notion = run.external_page(quiz["url"])
    changed = cfg["changed_answer"]
    notion.fill('[data-block-id="' + answers[0]["id"] + '"] [contenteditable="true"]', "答案：" + changed)
    notion.wait(lambda: _plain(_quiz_questions(_blocks(run, quiz["id"]))[1][0]) == "答案：" + changed)
    ui, _ = _open_course(run, cfg["course_id"], center="quizzes")
    cost_before = _ledger(run)
    _confirm(run, ui, _action("campus-grade-quiz", quiz=quiz["id"]))
    ui.wait(lambda: ui.visible(_action("campus-regrade-quiz", quiz=quiz["id"])))
    run.check("changed-question-or-answer-needs-confirmed-regrade",
              not _new_rows(cost_before, _ledger(run), "settlements")
              and ui.visible(_action("campus-regrade-quiz", quiz=quiz["id"])),
              "Changed real answer requires separate regrade confirmation and no charge before it")
    frozen_rejections = []
    def recover(variant):
        name = "secondary" if variant.startswith("two-devices") else "primary"
        if variant == "restart-after-charge":
            run.restart(name)
        active, _ = _open_course(run, cfg["course_id"], name, center="quizzes")
        def confirm_retry(selector):
            if single_machine and variant.startswith("repeat-"):
                active.prepare_dialog(True)
                active.double_click(selector)
                active.confirm(True)
            else:
                _confirm(run, active, selector)
        if "grade" in variant:
            confirm_retry(_action("campus-grade-quiz", quiz=quiz["id"]))
        else:
            for source_id in cfg["source_ids"]:
                active.click('[data-campus-quiz-source="' + source_id + '"]')
            active.fill("[data-campus-quiz-paper]", cfg["prior_paper"])
            if variant == 'changed-frozen-input':
                frozen_rejections.append(_changed_frozen_quiz_input(run, active, cfg))
                return
            confirm_retry(_action("campus-generate-quiz"))
        active.wait(lambda: not active.visible('[data-action="campus-refresh-quizzes"][disabled]'), timeout=180)
    faults = _faults(run, recover, lambda variant, old, new, receipt:
                     bool(receipt.get("observed")) and not _new_rows(old, new, "settlements")
                     and (variant != 'changed-frozen-input' or bool(frozen_rejections)
                          and frozen_rejections[-1]['passed'] and receipt.get('original_source_content_restored') is True
                          and receipt.get('archived_generated_block_verified') is True)
                     and len([row for row in _read(ui, path)["items"] if row.get("operation_id") == quiz.get("operation_id")]) == 1,
                     restart_operation_ids=lambda current: [row['operation_id'] for row in _new_rows(before, current, 'settlements')])
    if run.mode == "live":
        # The single-machine scope retries from the real primary installation;
        # the unchanged frozen inputs must still cause no second settlement.
        recover("repeat-generate" if single_machine else "two-devices-generate")
        costs = _ledger(run)
        recover("repeat-grade" if single_machine else "two-devices-grade")
        faults.append(not _new_rows(costs, _ledger(run), "settlements"))
    after = _ledger(run)
    new_jobs = _new_rows(before, after, "jobs")
    new_settlements = _new_rows(before, after, "settlements")
    run.check("concurrent-generate-grade-and-recovery-one-job-one-settlement",
              bool(faults) and all(faults) and len(new_jobs) == len(new_settlements) == 2
              and len({row["operation_id"] for row in new_settlements}) == 2,
              "Real generate/grade jobs, scoped native retries and independent unique settlements")
    ui.screenshot("quiz-history-and-regrade-confirmation")


def run_cross_platform(run):
    cfg = run.need("cross_platform")
    if run.context["target"] != "macos":
        _block(run, "real_macos_required", "A second Windows data directory is not a Mac")
    pair = _capability(run, run, "verify_pair")(cfg["windows_evidence"])
    if pair.get("target") != "windows":
        _block(run, "trusted_windows_pair_missing", "Require nonce-bound independent Windows evidence")
    fresh_name = cfg.get("fresh_instance", "fresh")
    fresh_path = _data_root(run, fresh_name).parent
    before_files = [path for path in fresh_path.rglob("*") if path.is_file()]
    run.actions.append({"event": "fresh_mac_profile_before_launch", "instance": fresh_name,
                        "observed_file_count": len(before_files)})
    if before_files:
        _block(run, "fresh_mac_sandbox_not_empty", "Fresh Mac must start before account data exists")
    from product_e2e.catalog_journeys import _login
    ui = run.session(fresh_name)
    _login(run, ui)
    ui, _ = _open_course(run, cfg["course_id"], fresh_name)
    profile = run.oracle.profile()
    workspace = _workspace(run, ui)
    identity = {"home_id": workspace["id"], "courses": {
        key: {"id": row["id"], "lectures": {item["source_id"]: item["id"]
                                               for item in row.get("lectures", {}).values()}}
        for key, row in workspace.get("courses", {}).items()}}
    run.check("windows-and-mac-native-home-course-lesson-identities-match",
              identity == pair.get("projection") and profile.get("revision") == pair.get("profile_revision"),
              "Actual native Mac projection matches trusted Windows home/course/source identities")
    inventory = run.observe().get("local_files", [])
    run.check("fresh-mac-pulls-without-local-copy-or-media-download",
              not before_files and not any(row["instance"] == fresh_name
                      and row["path"].endswith((".mp4", ".m4a", ".wav")) for row in inventory),
              "Actual fresh Mac installed sandbox inventory, no copied Windows cache/media")
    legacy_name = cfg.get("legacy_instance", "legacy")
    legacy = run.session(legacy_name)
    _login(run, legacy)
    legacy_runtime = run.runtime(legacy_name)
    if legacy_runtime.get("target") != "macos":
        _block(run, "legacy_real_macos_required", "Need an actual older Mac install profile")
    legacy, _ = _open_course(run, cfg["course_id"], legacy_name)
    courses = _read(legacy, "/api/campus/courses")["courses"]
    valid = next(row for row in courses if row["id"] == cfg["course_id"])
    extras = [row for row in courses if row["id"] in cfg["legacy_extra_course_ids"]]
    run.check("old-mac-partial-catalog-valid-courses-usable-and-unconfirmed-read-only",
              valid.get("read_only") is False and len(extras) == len(cfg["legacy_extra_course_ids"])
              and all(row.get("read_only") is True for row in extras),
              "Old real Mac cache with extra courses remains per-course usable/read-only")
    course = _read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe=""))
    summary = next(row for row in course["lessons"] if row.get("notion_note_url"))
    url = summary["notion_note_url"]
    _lesson_page(ui, summary["id"])
    ui.click('[data-action="campus-open-lecture"][data-url="' + url + '"]')
    notion = run.external_page(url)
    notion.wait(lambda: notion.visible('[contenteditable="true"]'))
    local_quizzes = _read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/quizzes")
    local_history = _read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/quiz-reviews")
    matching = {row["id"] for row in local_quizzes.get("items", [])} >= set(pair.get("quiz_ids", []))
    matching = matching and {row["id"] for row in local_history.get("histories", [])} >= set(pair.get("history_ids", []))
    download = _action("recording-download", recording=summary["id"])
    run.check("mac-oauth-download-entry-and-notion-quiz-grade-links-correct",
              matching and bool(local_quizzes.get("items")) and bool(local_history.get("histories"))
              and (ui.visible(download) or ui.visible('a[href="/api/recordings/' + summary["id"] + '/video"]')),
              "Actual Mac OAuth-backed Notion opens, media entry and trusted quiz/history identities")
    ui.screenshot("mac-shared-course-and-learning-links")
    def reopen(variant):
        name = legacy_name if variant == "legacy-mac-extra-course" else fresh_name
        run.restart(name)
        _open_course(run, cfg["course_id"], name)
    _faults(run, reopen, lambda variant, before, after, receipt:
            bool(receipt.get("observed")) and not _new_rows(before, after, "settlements"))
