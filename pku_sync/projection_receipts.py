"""Recover only known no-write/release phases using negotiated durable receipts.

An interrupted Notion writer remains fenced. Request identity recovery never
replays Notion writes and never adopts a relocated profile's pending journal.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import time
import threading

from . import catalog_snapshots as catalog, platform
from .catalog_schema import fingerprint

PROTOCOL = {'protocol': 1, 'request_identity': 'client_request_id', 'release': 'idempotent', 'expiry': None}
_cohort = ContextVar('prepared_catalog_projection_cohort', default=None)
_prepared_reads = ContextVar('phase_scoped_catalog_prepared_reads', default=None)
_READ_MAX_AGE_SECONDS = 20.0


class _PreparedReadPhase:
    def __init__(self, settings, entries):
        self.binding = catalog._read_binding(settings)
        self.operation = secrets.token_hex(32)
        self.phase = secrets.token_hex(32)
        self.thread = threading.get_ident()
        self.cohort = _cohort.get()
        self.entries = entries
        self.position = 0
        self.active = None
        self.closed = False

    def check(self, settings):
        if (self.closed or threading.get_ident() != self.thread
                or _cohort.get() is not self.cohort
                or catalog._read_binding(settings) != self.binding):
            self.closed = True
            raise catalog.CatalogUnavailable('准备读取操作、账号或阶段已变化；禁止复用。')

    def select(self, settings, expected):
        self.check(settings)
        if (self.cohort.started or self.active is not None or self.position >= len(self.entries)
                or len(self.cohort.members) != self.position):
            self.closed = True
            raise catalog.CatalogUnavailable('准备读取只能按原课程顺序使用一次。')
        entry = self.entries[self.position]
        if (fingerprint(expected) != entry['expected_sha256']
                or time.monotonic() - entry['read_at'] > _READ_MAX_AGE_SECONDS):
            self.closed = True
            raise catalog.CatalogUnavailable('课程准备读取已过期、快照变化或课序不一致。')
        self.active = entry
        self.position += 1

    def take(self, settings, kind):
        self.check(settings)
        if self.active is None:
            return None
        if time.monotonic() - self.active['read_at'] > _READ_MAX_AGE_SECONDS:
            self.closed = True
            raise catalog.CatalogUnavailable('课程准备读取已过期；请重新读取。')
        if kind in self.active['consumed']:
            self.closed = True
            raise catalog.CatalogUnavailable('课程准备读取不能重复消费。')
        self.active['consumed'].add(kind)
        return self.active


@contextmanager
def prepared_read_phase(settings, envelopes, *, check_cancel=None, max_workers=4):
    """Prefetch original GETs only; original journal/acquire/post-read stay serial."""
    if (_prepared_reads.get() is not None or _cohort.get() is None or _cohort.get().started
            or _cohort.get().members):
        raise catalog.CatalogUnavailable('课程准备读取必须属于一个未开始写入的独立许可阶段。')
    expected_values = [catalog._envelope(value, value['snapshot']['course_id']) for value in envelopes]
    course_ids = [value['snapshot']['course_id'] for value in expected_values]
    if len(set(course_ids)) != len(course_ids):
        raise ValueError('duplicate prepared course identity')
    with platform.request_scope():
        def fetch(expected):
            started = time.monotonic()
            protocol = discover(settings)
            catalog._check_read_worker()
            owner = owner_key(settings) if protocol else None
            catalog._check_read_worker()
            course_id = expected['snapshot']['course_id']
            # Existing pending journals keep their original main-thread
            # recovery-before-current order; never recover/mutate on a worker.
            pending = catalog.projection_journal_path(settings, course_id).exists()
            if owner is not None:
                pending = pending or journal_path(settings, course_id, owner).exists()
            if not pending:
                catalog.verify_current(settings, expected)
            return {'expected_sha256': fingerprint(expected), 'protocol': protocol, 'owner': owner,
                    'current_checked': not pending, 'read_at': started, 'consumed': set()}

        entries = catalog._read_batch(settings, expected_values, fetch,
                                      check_cancel=check_cancel, max_workers=max_workers)
        if len({entry['protocol'] for entry in entries}) > 1:
            raise catalog.CatalogUnavailable('账号服务协议在同一准备阶段变化。')
        if len({entry['owner'] for entry in entries if entry['owner'] is not None}) > 1:
            raise catalog.CatalogUnavailable('同一准备操作返回了不同账号身份。')
        phase = _PreparedReadPhase(settings, entries)
        token = _prepared_reads.set(phase)
        try:
            yield phase
        finally:
            phase.closed = True
            _prepared_reads.reset(token)


def _select_prepared_course(settings, expected):
    phase = _prepared_reads.get()
    if phase is not None:
        phase.select(settings, expected)


def _invalidate_prepared_read_phase():
    phase = _prepared_reads.get()
    if phase is not None:
        phase.closed = True


def _consume_prepared_current(settings, expected):
    phase = _prepared_reads.get()
    if phase is None:
        return False
    entry = phase.take(settings, 'current')
    if entry is None:
        return False  # Every original post-acquire GET remains a real fresh GET.
    if fingerprint(expected) != entry['expected_sha256']:
        phase.closed = True
        raise catalog.CatalogUnavailable('准备读取课程身份或快照不匹配。')
    phase.active = None
    return entry['current_checked']


class _Cohort:
    """Hold every permit before allowing the directory body to start."""
    def __init__(self, verified_steps=False, tracked_mutations=False):
        self.members = []
        self.started = False
        self.completed = False
        self.verified_steps = verified_steps
        self.active_step = None
        self.uncertain = False
        self.sequence = 0
        self.checkpointed = False
        self.tracked_mutations = tracked_mutations
        self.step_mutated = False

    def _save_phase(self, phase, kind=None):
        for _, path, state in self.members:
            state['phase'] = phase
            if self.verified_steps:
                if state['schema'] == 2:
                    state['schema'] = 3
                state['checkpoint'] = {'version': 1, 'sequence': self.sequence,
                                       'kind': kind or self.active_step or 'before-notion-write'}
            catalog._save_projection_journal(path, state)

    def known_boundary(self):
        return (self.verified_steps and self.checkpointed and not self.uncertain
                and self.active_step is None)

    def start_step(self, kind):
        if self.active_step is not None:
            raise catalog.CatalogUnavailable('目录验收步骤不能嵌套，已有写入保持保护。')
        if not self.checkpointed:
            self.uncertain = True
        self.active_step = kind
        self.checkpointed = False
        self.step_mutated = not self.tracked_mutations
        if self.step_mutated:
            self._save_phase('writing')

    def before_mutation(self):
        # Fully tracked clients identify every real write before transport.
        # Pure reads need no new unknown-writer journal. The same durable
        # writing barrier remains mandatory before the first real mutation.
        if not self.step_mutated:
            self.step_mutated = True
            self._save_phase('writing')

    def finish_step(self, kind):
        if self.active_step != kind:
            raise catalog.CatalogUnavailable('目录验收步骤身份不一致，不能恢复写入。')
        if self.tracked_mutations:
            from .projection_notion_effects import assert_complete
            assert_complete(self)
        self.active_step = None
        if self.uncertain:
            return
        self.sequence += 1
        if self.step_mutated:
            self._save_phase('checkpointed', kind)
        # Set only after every receipt has durably reached the known boundary.
        self.checkpointed = True

    def untracked_write(self):
        self.uncertain = True
        self.checkpointed = False
        self._save_phase('writing')

    def register(self, settings, path, state):
        if self.started:
            raise catalog.CatalogUnavailable('目录已经开始写入，禁止临时追加未准备的课程许可。')
        self.members.append((settings, path, state))

    def begin(self):
        if self.started:
            raise catalog.CatalogUnavailable('课程目录写入阶段已经开始，不能重复进入。')
        for _, path, state in self.members:
            if state['phase'] != 'prepared':
                raise catalog.CatalogUnavailable('课程目录许可尚未全部准备，未开始 Notion 写入。')
        if self.tracked_mutations:
            from .projection_notion_effects import initialize
            initialize(self)
        self._save_phase('checkpointed' if self.verified_steps else 'writing')
        # An interrupted phase update conservatively leaves a writing receipt.
        # Only after every update succeeds may the body perform a mutation.
        self.started = True
        self.checkpointed = self.verified_steps

    def complete(self):
        if not self.started or self.completed:
            raise catalog.CatalogUnavailable('课程目录写入阶段未完成，不能标记恢复结果。')
        if self.tracked_mutations:
            from .projection_notion_effects import assert_complete
            assert_complete(self)
        if self.verified_steps and not self.known_boundary():
            raise catalog.CatalogUnavailable('仍有未核验的 Notion 写入步骤，不能声明目录写入完成。')
        # The caller reaches here only when the entire verified directory body
        # returned normally. A sibling release error cannot change that fact.
        self.completed = True
        for _, path, state in self.members:
            state['phase'] = 'verified'
            catalog._save_projection_journal(path, state)


@contextmanager
def prepared_cohort(*, verified_steps=False, tracked_mutations=False):
    if _cohort.get() is not None:
        raise catalog.CatalogUnavailable('课程目录许可正在准备，禁止嵌套批量写入。')
    group = _Cohort(verified_steps=verified_steps, tracked_mutations=tracked_mutations)
    token = _cohort.set(group)
    try:
        yield group
    finally:
        _cohort.reset(token)


@contextmanager
def verified_workspace_step(kind):
    """A product step whose caller verifies its Notion effects before return.

    Persist an unknown phase before entering the step. Only its normal return
    after independent reads creates a resumable boundary; an exception or any
    write outside such a step keeps the entire cohort fenced.
    """
    if kind not in {'home', 'course', 'center', 'source-section', 'lecture',
                    'lecture-placeholder', 'summary-link', 'recording-index'}:
        raise ValueError('Unknown verified workspace step')
    group = _cohort.get()
    if group is None or not group.verified_steps:
        yield
        return
    if not group.started or group.completed:
        raise catalog.CatalogUnavailable('目录许可阶段不能执行 Notion 验收步骤。')
    try:
        group.start_step(kind)
        yield
        group.finish_step(kind)
    except BaseException:
        group.uncertain = True
        group.checkpointed = False
        group.active_step = None
        raise


def verified_workspace_operation(kind):
    def decorate(operation):
        @wraps(operation)
        def verified(*args, **kwargs):
            with verified_workspace_step(kind):
                return operation(*args, **kwargs)
        return verified
    return decorate


def assert_notion_write_phase(method, url):
    group = _cohort.get()
    # Search/database query are read-only POST operations. Every mutation
    # must wait for the durable begin transition of the entire cohort.
    read = method.upper() == 'GET' or (method.upper() == 'POST'
        and (url == '/v1/search' or re.fullmatch(r'/v1/databases/[0-9a-f-]+/query', url)))
    if group is not None and not group.started and not read:
        raise catalog.CatalogUnavailable('课程目录许可尚未全部准备，禁止提前写入 Notion。')
    if group is not None and group.started and group.verified_steps and not read and group.active_step is None:
        group.untracked_write()
    elif (group is not None and group.started and group.verified_steps
          and group.tracked_mutations and not read):
        group.before_mutation()


def discover(settings):
    catalog._check_read_worker()
    phase = _prepared_reads.get()
    entry = phase.take(settings, 'protocol') if phase is not None else None
    if entry is not None:
        return entry['protocol']
    value = catalog._call(settings, 'GET', 'projection-protocol')
    if (set(value) == {'revision', 'snapshot'} and type(value['revision']) is int
            and value['revision'] == 0 and value['snapshot'] is None):
        return False  # Explicit old route response, never blind extra fields.
    if type(value.get('protocol')) is int and value == PROTOCOL:
        return True
    raise catalog.CatalogUnavailable('课程许可恢复协议未核验；已有课程保持只读，请检查账号服务版本。')


def owner_key(settings):
    catalog._check_read_worker()
    phase = _prepared_reads.get()
    entry = phase.take(settings, 'owner') if phase is not None else None
    if entry is not None:
        if entry['owner'] is None:
            raise catalog.CatalogUnavailable('准备阶段未核验账号身份。')
        return entry['owner']
    try:
        response = platform._get(platform._endpoint(settings.cloud_transcribe_url, '/v1/auth/me'),
            headers=platform._profile_headers(settings), timeout=20)
        if response.status_code == 401:
            raise catalog.CatalogUnavailable('登录已失效，请重新登录；已有课程缓存保持只读。')
        if response.status_code != 200:
            raise catalog.CatalogUnavailable('账号身份暂时无法核验；已有课程缓存保持只读。')
        value = response.json()
        if (not isinstance(value, dict) or value.get('email_verified') is not True
                or not isinstance(value.get('email'), str) or not value['email'].strip()):
            raise catalog.CatalogUnavailable('账号或邮箱验证状态未核验；请先登录并完成邮箱验证。')
    except (platform.httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, catalog.CatalogUnavailable):
            raise
        raise catalog.CatalogUnavailable('账号身份读取失败；已有课程缓存保持只读。') from exc
    return fingerprint({'email': value['email'].strip().casefold(),
                        'catalog_endpoint': platform._endpoint(settings.cloud_transcribe_url, '/v1/catalog')})


def journal_path(settings, course_id, owner):
    return Path(settings.data_dir) / '.projection-receipts' / owner / (fingerprint({'course': course_id}) + '.json')


def root_key(settings):
    return fingerprint({'data_root': os.path.normcase(str(Path(settings.data_dir).resolve()))})


def _hex(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _receipt(value, state):
    if (not isinstance(value, dict) or set(value) != {'client_request_id', 'revision', 'permit_id', 'state'}
            or value['client_request_id'] != state['client_request_id']
            or type(value['revision']) is not int or value['revision'] != state['revision']
            or not _hex(value['permit_id']) or not isinstance(value['state'], str) or value['state'] not in {'acquired', 'released'}
            or ('permit_id' in state and state['permit_id'] != value['permit_id'])):
        raise catalog.CatalogUnavailable('课程许可回读与本机意图不一致；记录已保留，未授权继续写入。')
    return value


def _load(path, settings, course_id, owner):
    try:
        value = json.loads(path.read_text('utf-8'))
        required = {'schema', 'course_id', 'revision', 'snapshot_sha256', 'phase', 'client_request_id', 'owner', 'root'}
        if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'permit_id', 'checkpoint'}
                or type(value['schema']) is not int or value['schema'] not in {2, 3}
                or value['course_id'] != course_id or value['owner'] != owner or value['root'] != root_key(settings)
                or type(value['revision']) is not int or value['revision'] < 1
                or not _hex(value['snapshot_sha256']) or not _hex(value['client_request_id'])
                or value['phase'] not in {'acquiring', 'prepared', 'writing', 'writing-unknown', 'verified', 'releasing', 'checkpointed'}
                or ('permit_id' in value and not _hex(value['permit_id']))
                or (value['phase'] != 'acquiring' and 'permit_id' not in value)):
            raise ValueError('invalid owned receipt')
        checkpoint = value.get('checkpoint')
        if ((value['schema'] == 2 and ('checkpoint' in value or value['phase'] == 'checkpointed'))
                or (value['schema'] == 3 and (not isinstance(checkpoint, dict)
                    or set(checkpoint) != {'version', 'sequence', 'kind'}
                    or type(checkpoint['version']) is not int or checkpoint['version'] != 1
                    or type(checkpoint['sequence']) is not int or checkpoint['sequence'] < 0
                    or checkpoint['kind'] not in {'before-notion-write', 'home', 'course', 'center',
                        'source-section', 'lecture', 'lecture-placeholder', 'summary-link', 'recording-index'}
                    or (value['phase'] == 'checkpointed' and
                        ((checkpoint['sequence'] == 0) != (checkpoint['kind'] == 'before-notion-write')))))):
            raise ValueError('invalid verified checkpoint')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise catalog.CatalogUnavailable('上次课程许可回执损坏、账号不符或测试目录已变化；记录已保留，不能自动恢复。') from exc
    return value


def _release(settings, path, state, permit):
    state.update(permit_id=permit, phase='releasing')
    catalog._save_projection_journal(path, state)
    value = catalog._call(settings, 'POST', state['course_id'], '/projection/release',
        {'client_request_id': state['client_request_id'], 'permit_id': permit})
    if not isinstance(value, dict) or set(value) != {'released'} or value['released'] is not True:
        raise catalog.CatalogUnavailable('课程许可释放回执未确认；记录已保留，请核验后恢复。')
    path.unlink()
    from .projection_notion_effects import intent_path
    intent_path(settings, state).unlink(missing_ok=True)


def _recover(settings, path, state):
    if state['phase'] in {'writing', 'writing-unknown'}:
        from .projection_notion_effects import recover
        if not recover(settings, state):
            raise catalog.CatalogUnavailable('上次 Notion 写入结果尚未核验；已有课程保留，只读核验写入结果前不会释放许可或重放写入。')
    try:
        receipt = catalog._call(settings, 'GET', state['course_id'],
                                '/projection/requests/' + state['client_request_id'])
    except catalog.CatalogUnavailable:
        # One bounded replay of the same server identity is safe only because
        # the exact protocol was negotiated. A late acquire cannot regrant an
        # already released identity; an unknown read never authorizes a writer.
        if state['phase'] == 'acquiring':
            receipt = catalog._call(settings, 'POST', state['course_id'], '/projection',
                {'revision': state['revision'], 'client_request_id': state['client_request_id']})
        else:
            _release(settings, path, state, state['permit_id'])
            return
    receipt = _receipt(receipt, state)
    if receipt['state'] == 'released':
        path.unlink()
    else:
        _release(settings, path, state, receipt['permit_id'])


@contextmanager
def projection(settings, expected):
    course_id = expected['snapshot']['course_id']
    group = _cohort.get()
    if group is not None and group.started:
        raise catalog.CatalogUnavailable('目录已经开始写入，禁止临时追加课程许可。')
    owner = owner_key(settings)
    path = journal_path(settings, course_id, owner)
    with catalog._projection_journal_lock(path):
        if path.exists():
            _recover(settings, path, _load(path, settings, course_id, owner))
        catalog.verify_current(settings, expected)
        state = {'schema': 2, 'course_id': course_id, 'revision': expected['revision'],
            'snapshot_sha256': fingerprint(expected['snapshot']), 'phase': 'acquiring',
            'client_request_id': secrets.token_hex(32), 'owner': owner, 'root': root_key(settings)}
        catalog._save_projection_journal(path, state)
        permit = catalog._call(settings, 'POST', course_id, '/projection',
            {'revision': state['revision'], 'client_request_id': state['client_request_id']})
        permit = _receipt(permit, state)
        if permit['state'] != 'acquired':
            raise catalog.CatalogUnavailable('新的课程写入意图未获有效许可；记录已保留。')
        state.update(permit_id=permit['permit_id'], phase='prepared' if group is not None else 'writing')
        entered_writer = False
        try:
            catalog._save_projection_journal(path, state)
            catalog.verify_current(settings, expected)
            if group is not None:
                group.register(settings, path, state)
            entered_writer = True
            yield expected
        except BaseException:
            if entered_writer and not (group is not None and
                    (not group.started or group.completed or group.known_boundary())):
                state['phase'] = 'writing-unknown'
                try:
                    catalog._save_projection_journal(path, state)
                except OSError:
                    pass  # Earlier durable writing phase still fences recovery.
            else:
                _release(settings, path, state, permit['permit_id'])
            raise
        else:
            _release(settings, path, state, permit['permit_id'])
