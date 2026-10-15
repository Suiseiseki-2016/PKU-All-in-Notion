"""Account canonical metadata, observation CAS and shared projection permits.

A PUT requires the revision obtained *before* the live portal fetch. A
conflict never retries old data against a newly fetched revision. Unsupported
relay versions and incomplete portal reads permit local reading only.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import copy_context
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import secrets
import sqlite3
import time
from threading import Event
from pathlib import Path
from urllib.parse import quote

from . import platform
from .catalog_schema import SECTIONS, FIELDS, fingerprint, validate_snapshot


class CatalogUnavailable(ValueError):
    pass


class CatalogTransportUnavailable(CatalogUnavailable):
    """The account connection failed, rather than rejecting one course."""


class CatalogSourceRemovalRequired(CatalogUnavailable):
    """The portal must be read again before replacing obsolete cloud entries."""


class CatalogProjectionBusy(CatalogUnavailable):
    """An exact service rejection identifies an unresolved shared writer."""

    def __init__(self):
        super().__init__('上次共享目录写入许可尚未释放；已有课程可继续查看。请先等待正在运行的同步结束；若已结束仍出现此提示，需要核对上次 Notion 写入后恢复许可，反复点击更新不会解除。')


def recording_source_id(recording) -> str:
    # Blackboard exposes no permanent ID in its recording list. The campus
    # start timestamp identifies the session, irrespective of title, folder
    # or rotating CAS token. Ambiguous duplicate timestamps fail validation.
    if recording.source_id:
        return recording.source_id
    if not recording.recorded_at:
        raise ValueError("录像缺少教学网讲次时间，无法建立稳定源标识。")
    return "pku:recording:" + fingerprint({"course": recording.course_id,
                                           "start": recording.recorded_at})[:32]


def _call(settings, method: str, course_id: str, suffix="", body=None) -> dict:
    _check_read_worker()
    if platform._read_only_worker.get() and method != 'GET':
        raise CatalogUnavailable('课程准备线程只读，禁止发出修改请求。')
    headers = platform._profile_headers(settings)
    configured = getattr(settings, "cloud_transcribe_url", None)
    if not headers or not isinstance(configured, str):
        raise CatalogUnavailable("账号级课程快照尚不可用；课程缓存保持只读。请重新登录产品账号后重试。")
    url = platform._endpoint(configured, "/v1/catalog" + ("/" + quote(course_id, safe="") if course_id else "") + suffix)
    kwargs = {"headers": headers, "timeout": 20}
    if body is not None:
        kwargs["json"] = body
    try:
        sender = {"GET": platform._get, "PUT": platform._put, "POST": platform._post}[method]
        response = sender(url, **kwargs)
        if response.status_code != 200:
            if response.status_code == 409:
                try:
                    rejected = response.json()
                except ValueError:
                    rejected = {}
                detail = rejected.get('detail') if isinstance(rejected, dict) else None
                # Only exact protocol reasons select user-facing copy. Never
                # expose arbitrary server bodies, tokens or student content.
                if detail in {'another shared projection is running', 'shared projection in progress'}:
                    raise CatalogProjectionBusy()
                if detail in {'stale projection revision', 'stale course revision; do not rebase cached data'}:
                    raise CatalogUnavailable('课程目录版本已变化；已有记录已保留。请重新读取教学网目录后重试，不会将旧快照覆盖到新版本。')
                if detail == 'missing source IDs need explicit portal removal review':
                    raise CatalogSourceRemovalRequired('教学网当前目录与云端历史目录不同，需要再次完整读取后核对过时条目。')
            if response.status_code == 401:
                message = "产品账号登录已失效；已有课程缓存已保留。请重新登录后重试。"
            elif response.status_code == 402:
                message = "账号可用额度不足；已有课程缓存已保留。请检查当前额度后重试。"
            elif response.status_code == 403:
                message = "账号暂不允许读取课程快照；已有课程缓存已保留。请检查账号权限或重新登录后重试。"
            elif response.status_code == 409:
                message = "账号课程目录发生冲突；已有记录已保留。请重新读取教学网目录后重试，不会用旧快照覆盖新版本。"
            elif response.status_code == 429:
                message = "账号服务请求过于频繁；已有课程缓存已保留。请稍后重试。"
            else:
                message = "账号课程快照服务暂不可用；已有课程缓存已保留。请检查网络后稍后重试。"
            raise CatalogUnavailable(message)
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("invalid response")
        return value
    except (platform.httpx.HTTPError, ValueError) as exc:
        from . import projection_receipts
        projection_receipts._invalidate_prepared_read_phase()
        if isinstance(exc, CatalogUnavailable):
            raise
        if isinstance(exc, platform.httpx.TransportError):
            raise CatalogTransportUnavailable("账号课程快照连接中断；已有课程记录已保留。请检查网络后重试。") from exc
        raise CatalogUnavailable("账号课程快照读取失败；缓存保持只读。请检查网络后稍后重试。") from exc


def _envelope(value: dict, course_id: str) -> dict:
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        raise CatalogUnavailable("账号服务没有返回有效课程 revision。")
    try:
        snapshot = validate_snapshot(value.get("snapshot"))
    except ValueError as exc:
        raise CatalogUnavailable("账号服务返回的课程快照不完整。") from exc
    if snapshot["course_id"] != course_id:
        raise CatalogUnavailable("账号课程快照身份不匹配。")
    return {"revision": revision, "snapshot": snapshot}


def begin_observation(settings, course_id: str) -> dict:
    value = _call(settings, "POST", course_id, "/observations", {})
    if type(value.get("revision")) is not int or value["revision"] < 0 or not isinstance(value.get("observation_id"), str) or not value["observation_id"]:
        raise CatalogUnavailable("账号服务未返回有效读取凭据。")
    return value


def current(settings, course_id: str) -> dict:
    return _envelope(_call(settings, "GET", course_id), course_id)


def read_recording_history(settings, course_id: str, current_envelope: dict,
                           expected_previous_sources=None) -> dict:
    """Fresh read-only historical evidence; never canonical/processing authority."""
    from .recording_history import verified_history
    return verified_history(settings, course_id, current_envelope, expected_previous_sources)


def published_current(settings, course_id: str) -> dict | None:
    """Read the service's explicit never-published state during batch discovery.

    This is not authority to write: current(), projection(), publish() and
    save_cache() retain their positive-revision, complete-snapshot contract.
    Missing/null/malformed fields must never be treated as an empty course.
    """
    value = _call(settings, "GET", course_id)
    if type(value.get("revision")) is int and value["revision"] == 0 and "snapshot" in value and value["snapshot"] is None:
        return None
    return _envelope(value, course_id)


def publish(settings, course_id: str, observation: dict, snapshot: dict, *, removal_confirmation=None) -> dict:
    snapshot = validate_snapshot(snapshot)
    if snapshot["course_id"] != course_id:
        raise ValueError("course identity mismatch")
    body = {
        "revision": observation["revision"], "observation_id": observation["observation_id"],
        "snapshot": snapshot,
    }
    if removal_confirmation is not None:
        body['removal_confirmation'] = removal_confirmation
    value = _call(settings, "PUT", course_id, body=body)
    value = _envelope(value, course_id)
    if value["revision"] <= observation["revision"] or value["snapshot"] != snapshot:
        raise CatalogUnavailable("账号服务未确认新课程快照；未授权共享写入。")
    return value


def verify_current(settings, expected: dict) -> dict:
    _check_read_worker()
    from . import projection_receipts
    if projection_receipts._consume_prepared_current(settings, expected):
        return expected
    course_id = expected["snapshot"]["course_id"]
    try:
        actual = current(settings, course_id)
    except BaseException:
        projection_receipts._invalidate_prepared_read_phase()
        raise
    if actual != expected:
        projection_receipts._invalidate_prepared_read_phase()
        raise CatalogUnavailable("课程快照已更新，请刷新；旧快照禁止写入共享目录。")
    return actual


def _read_binding(settings):
    configured = getattr(settings, 'cloud_transcribe_url', None)
    token = getattr(settings, 'platform_token', None)
    if not isinstance(configured, str) or not isinstance(token, str) or not token.strip():
        raise CatalogUnavailable('账号准备读取身份未确认；已有数据保持只读。')
    return (platform._endpoint(configured, '/v1/catalog'), fingerprint({'session': token}),
            os.path.normcase(str(Path(settings.data_dir).resolve())), id(platform._request_client.get()))


def _check_read_worker():
    stopped = platform._read_worker_stop.get()
    if stopped is not None and stopped.is_set():
        raise CatalogUnavailable('课程准备读取已取消。')


def _read_batch(settings, values, read, *, check_cancel=None, max_workers=4):
    """Only independent GETs; join every owned worker before returning/raising."""
    if type(max_workers) is not int or max_workers < 1:
        raise ValueError('max_workers must be positive')
    values = tuple(values)
    stopped = Event()
    with platform.request_scope():
        binding = _read_binding(settings)

        def check():
            if check_cancel is not None:
                check_cancel()
            if _read_binding(settings) != binding:
                raise CatalogUnavailable('账号、服务或数据目录已变化；禁止复用准备读取。')

        def fetch(value):
            if stopped.is_set() or _read_binding(settings) != binding:
                raise CatalogUnavailable('课程准备读取已取消或身份变化。')
            token = platform._read_only_worker.set(True)
            stop_token = platform._read_worker_stop.set(stopped)
            try:
                result = read(value)
                if stopped.is_set() or _read_binding(settings) != binding:
                    raise CatalogUnavailable('课程准备读取已取消或身份变化。')
                return result
            finally:
                platform._read_worker_stop.reset(stop_token)
                platform._read_only_worker.reset(token)

        check()
        if len(values) <= 1 or max_workers == 1:
            result = []
            for value in values:
                check()
                result.append(fetch(value))
            check()
            return result
        workers = min(max_workers, 4, len(values))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='catalog-read')
        pending, result = {}, {}
        iterator = iter(enumerate(values))
        try:
            for _ in range(workers):
                index, value = next(iterator)
                context = copy_context()
                pending[pool.submit(context.run, fetch, value)] = index
            while pending:
                check()
                completed, _ = wait(pending, timeout=.02, return_when=FIRST_COMPLETED)
                check()
                for future in completed:
                    result[pending.pop(future)] = future.result()
                for _ in completed:
                    check()
                    following = next(iterator, None)
                    if following is not None:
                        index, value = following
                        context = copy_context()
                        pending[pool.submit(context.run, fetch, value)] = index
            check()
            return [result[index] for index in range(len(values))]
        except BaseException:
            stopped.set()
            for future in pending:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def read_current_batch(settings, course_ids, *, read=None, check_cancel=None, max_workers=4):
    """Run the original per-course readers, preserving order and explicit None."""
    identifiers = tuple(course_ids)
    if len(set(identifiers)) != len(identifiers) or any(not isinstance(cid, str) or not cid for cid in identifiers):
        raise ValueError('invalid or duplicate course identities')
    reader = current if read is None else read

    def fetch(course_id):
        value = reader(settings, course_id)
        if value is not None:
            _envelope(value, course_id)
        return value

    return _read_batch(settings, identifiers, fetch, check_cancel=check_cancel, max_workers=max_workers)


def _replace_local_file(temporary: Path, path: Path) -> None:
    """Wait briefly for Windows readers, without repeating a network write.

    Keep the sealed temporary file and original destination throughout. A
    persistent permission error still fails instead of deleting the old file.
    """
    delays = (.01, .02, .04, .08, .08)
    for attempt in range(len(delays) + 1):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def activate_account(settings) -> None:
    token = getattr(settings, "platform_token", "") or ""
    namespace = fingerprint({"session": token}) if token else "signed-out"
    path = Path(settings.data_dir) / ".account-catalog" / "active-account"
    # Re-entering the same operation/account is not an account switch. Retain
    # its epoch so fresh scoped history stays valid; A->B->A still replaces it.
    try:
        if path.read_text("ascii") == namespace:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("active-account." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(namespace, "ascii")
        _replace_local_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cache_path(root: Path, course_id: str) -> Path:
    directory = Path(root) / ".account-catalog"
    try:
        namespace = (directory / "active-account").read_text("ascii")
    except OSError:
        namespace = "unscoped"
    if namespace not in {"unscoped", "signed-out"} and (len(namespace) != 64 or any(c not in "0123456789abcdef" for c in namespace)):
        raise CatalogUnavailable("本地账号课程缓存身份无效。")
    return directory / namespace / (fingerprint({"course": course_id}) + ".json")


def save_cache(root: Path, envelope: dict) -> None:
    course_id = envelope["snapshot"]["course_id"]
    envelope = _envelope(envelope, course_id)
    path = cache_path(root, course_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temp.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), "utf-8")
        _replace_local_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def cached(root: Path, course_id: str) -> dict | None:
    try:
        return _envelope(json.loads(cache_path(root, course_id).read_text("utf-8")), course_id)
    except (OSError, ValueError):
        return None


def read_status(root: Path, complete: bool, reason: str = "", *, course_id: str = "") -> None:
    path = Path(root) / "portal-read-status.json"
    try:
        previous = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        previous = {}
    if course_id:
        # A selected refresh cannot prove that the whole portal was refreshed.
        # Retain only this account's existing per-course confirmations; otherwise
        # completing one course could relabel another account's cache as usable.
        namespace = cache_path(root, course_id).parent.name
        same_account = (isinstance(previous, dict)
                        and previous.get("account_namespace") == namespace
                        and namespace not in {"unscoped", "signed-out"})
        courses = previous.get("courses", {}) if same_account else {}
        value = {"complete": False, "reason": reason, "scope": "course",
                 "course_id": course_id, "account_namespace": namespace,
                 "courses": courses if isinstance(courses, dict) else {}}
        _save_read_status(path, value)
        return
    value = {"complete": complete, "reason": reason}
    if reason == "reading":
        value["courses"] = {}
    elif reason in {"complete", "partial", "account_unconfirmed"} and "courses" in previous:
        value["courses"] = previous["courses"]
        value["account_namespace"] = previous.get("account_namespace", "unscoped")
    _save_read_status(path, value)


def _save_read_status(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        _replace_local_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def course_read_status(root: Path, course_id: str, complete: bool, reason: str,
                       *, issue: str = "") -> None:
    if issue not in {"", "projection_busy"} or (complete and issue):
        raise ValueError("invalid course diagnostic")
    path = Path(root) / "portal-read-status.json"
    value = json.loads(path.read_text("utf-8"))
    value.setdefault("courses", {})[course_id] = {"complete": complete, "reason": reason}
    if issue:
        # Persist a fixed diagnostic code, never a server body or credential.
        value["courses"][course_id]["issue"] = issue
    value["account_namespace"] = cache_path(root, course_id).parent.name
    _save_read_status(path, value)


def assert_portal_readable(root: Path, course_id: str | None = None) -> None:
    path = Path(root) / "portal-read-status.json"
    if path.exists():
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。") from exc
        if course_id is not None and "courses" in value:
            if value.get("account_namespace") != cache_path(root, course_id).parent.name:
                raise CatalogUnavailable("教学网读取账号已变化；课程缓存保持只读。")
            value = value["courses"].get(course_id, {})
        if value.get("complete") is not True:
            raise CatalogUnavailable("最近一次教学网读取失败或不完整；课程缓存保持只读。")


def projection_readable_course_ids(root: Path, course_ids) -> list[str]:
    """Authorize a batch's complete courses without authorizing its failed ones.

    A partial ledger is usable only with explicit per-course results in the
    current account namespace. Authentication failures, in-progress reads and
    malformed/legacy failure ledgers retain the global fail-closed behavior.
    """
    identifiers = list(course_ids)
    path = Path(root) / "portal-read-status.json"
    if path.exists():
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。") from exc
        if isinstance(value, dict) and value.get("complete") is False and value.get("reason") == "partial":
            statuses = value.get("courses")
            if not isinstance(statuses, dict) or not statuses:
                raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。")
            readable = []
            for cid in identifiers:
                if value.get("account_namespace") != cache_path(root, cid).parent.name:
                    raise CatalogUnavailable("教学网读取账号已变化；课程缓存保持只读。")
                status = statuses.get(cid)
                if not isinstance(status, dict) or type(status.get("complete")) is not bool:
                    raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。")
                if status["complete"]:
                    assert_portal_readable(root, cid)
                    readable.append(cid)
            return readable
    assert_portal_readable(root)
    return identifiers


def pull(settings) -> dict:
    """A new device consumes canonical metadata without fetching any media."""
    activate_account(settings)
    value = _call(settings, "GET", "")
    rows = value.get("courses")
    if not isinstance(rows, list):
        raise CatalogUnavailable("账号课程列表不完整。")
    clean = []
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("snapshot"), dict):
            raise CatalogUnavailable("账号课程列表不完整。")
        identifier = row["snapshot"].get("course_id")
        envelope = _envelope(row, identifier)
        if identifier in identities:
            raise CatalogUnavailable("账号课程列表存在重复源标识。")
        identities.add(identifier)
        clean.append(envelope)
    root = Path(settings.data_dir)
    from .models import Course
    from .course_storage import plan_course_directories
    folders = plan_course_directories(root, [Course.model_validate(envelope["snapshot"]["course"])
                                           for envelope in clean])
    for envelope in clean:
        snapshot = envelope["snapshot"]
        identifier = snapshot["course_id"]
        save_cache(root, envelope)
        folder = folders[identifier]
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "course.json").exists():
            (folder / "course.json").write_text(json.dumps(snapshot["course"], ensure_ascii=False), "utf-8")
        index = folder / "recordings" / "index.json"
        if not index.exists():
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text("[]", "utf-8")
    return {"available": True, "status": "pulled", "courses": len(clean)}


def build_snapshot(course, recordings, materials, assignments, announcements) -> dict:
    groups = {
        "recordings": [{**{k: v for k, v in row.model_dump().items() if k in FIELDS["recordings"]},
                        "source_id": recording_source_id(row)} for row in recordings],
        "materials": materials, "assignments": assignments, "announcements": announcements,
    }
    for section in SECTIONS[1:]:
        groups[section] = [{k: v for k, v in row.items() if k in FIELDS[section]} for row in groups[section]]
        for row in groups[section]:
            source = (row.get("id") if section == "announcements"
                      else row.get("content_id") or row.get("source_id"))
            if not source:
                raise ValueError(f"教学网 {section} 条目缺少稳定源 ID，禁止共享写入。")
            row["source_id"] = f"pku:{course.course_id}:{section}:{source}"
            if "source_links" in row:
                from urllib.parse import urlsplit
                row["source_links"] = [link for link in row["source_links"]
                                       if urlsplit(link.get("url", "")).hostname == "course.pku.edu.cn"
                                       and not urlsplit(link["url"]).query]
    return validate_snapshot({
        "schema": 1, "authority": "course.pku.edu.cn", "course_id": course.course_id,
        "course": {k: getattr(course, k) for k in ("course_id", "name", "code", "term")},
        "sections": {name: {"complete": True, "items": groups[name]} for name in SECTIONS},
    })


def projection_journal_path(settings, course_id: str) -> Path:
    namespace = fingerprint({'session': getattr(settings, 'platform_token', '')})
    return Path(settings.data_dir) / '.projection-permits' / namespace / (fingerprint({'course': course_id}) + '.json')


def _save_projection_journal(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        # Windows readers can briefly hold a handle without FILE_SHARE_DELETE.
        # Retry only this sealed, already-fsynced file's atomic replacement,
        # never a network write. Persistent ACL errors retain the old receipt.
        _replace_local_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _projection_journal_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path.with_suffix('.lock.sqlite'), timeout=1)
    try:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            raise CatalogUnavailable('本机另一进程正在更新该课程目录，请等待完成；已有课程可继续查看。') from exc
        yield
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def projection(settings, expected: dict):
    """Server serializes revisions and projections; a crash fails closed.

Permits do not expire automatically: a timed-out Notion request can still
commit. An operator must verify a crashed writer is stopped before clearing
its lock. A lease timeout alone cannot safely fence a direct Notion writer.
"""
    from . import projection_receipts
    projection_receipts._select_prepared_course(settings, expected)
    course_id = expected["snapshot"]["course_id"]
    path = projection_journal_path(settings, course_id)
    with _projection_journal_lock(path):
        if path.exists():
            raise CatalogUnavailable('上次课程写入许可回执尚未核验，已有记录已保留。请核对账号服务及上次 Notion 写入后恢复许可；不会自动重放申请或写入。')
        from . import projection_receipts
        if projection_receipts.discover(settings):
            with projection_receipts.projection(settings, expected) as authorized:
                yield authorized
            return
        group = projection_receipts._cohort.get()
        if group is not None and group.started:
            raise CatalogUnavailable('目录已经开始写入，禁止临时追加课程许可。')
        verify_current(settings, expected)
        state = {'schema': 1, 'course_id': course_id, 'revision': expected['revision'],
                 'snapshot_sha256': fingerprint(expected['snapshot']), 'phase': 'acquiring',
                 'local_receipt_id': secrets.token_hex(32)}
        # Persist intent before an acquire can commit. This is a local audit
        # ID, not a claim that the old server supports idempotent acquisition.
        _save_projection_journal(path, state)
        permit = _call(settings, 'POST', course_id, '/projection', {'revision': expected['revision']})
        if not isinstance(permit.get('permit_id'), str) or not permit['permit_id']:
            raise CatalogUnavailable('服务端未授权共享目录写入；申请回执已保留待核验。')
        state.update(permit_id=permit['permit_id'], phase='prepared' if group is not None else 'writing')
        def release_known():
            # A failed disk write must not prevent attempting the safe release
            # of our known permit. Its earlier acquiring intent stays durable.
            state['phase'] = 'releasing'
            disk_error = None
            try:
                _save_projection_journal(path, state)
            except OSError as exc:
                disk_error = exc
            released = _call(settings, 'POST', course_id, '/projection/release', {'permit_id': permit['permit_id']})
            if not isinstance(released, dict) or set(released) != {'released'} or released['released'] is not True:
                raise CatalogUnavailable('课程写入许可释放未确认，回执已保留；请核验后恢复。')
            path.unlink()
            from .projection_notion_effects import intent_path
            intent_path(settings, state).unlink(missing_ok=True)
            if disk_error:
                raise disk_error
        entered_writer = False
        try:
            _save_projection_journal(path, state)
            verify_current(settings, expected)
            if group is not None:
                group.register(settings, path, state)
            entered_writer = True
            yield expected
        except BaseException:
            if entered_writer and not (group is not None and
                    (not group.started or group.completed or group.known_boundary())):
                state['phase'] = 'writing-unknown'
                try:
                    _save_projection_journal(path, state)
                except OSError:
                    pass  # The earlier durable writing receipt still fences it.
            else:
                release_known()
            raise
        else:
            release_known()
