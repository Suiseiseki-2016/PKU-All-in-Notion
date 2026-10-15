"""Real isolated draft/cache mutations for formal Windows fault journeys.

No business function or service response is replaced. A native rejection is
checked by the caller separately; observing a mutation alone never passes it.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import zipfile

from .core import ResourceBlocked, sha256
from pku_sync.catalog_snapshots import cache_path

SUPPORTED = {'changed-file', 'changed-target', 'changed-deadline'}


@contextmanager
def inject(run, variant):
    if variant not in SUPPORTED or run.mode != 'fault' or run.context.get('target') != 'windows':
        raise ResourceBlocked('native_draft_fault_windows_fault_required')
    if run.context.get('_draft_fault_armed'):
        raise ResourceBlocked('overlapping_native_draft_fault')
    fixture = run.context.get('_draft_fault_fixture')
    if not isinstance(fixture, dict):
        raise ResourceBlocked('native_draft_fault_fixture_required')
    root = Path(run.need('test_root')).resolve()
    data = (Path(run.config['instances']['primary']['app_dir']) / 'data').resolve()
    payload = Path(fixture['actual_path']).resolve()
    if (data == root or not data.is_relative_to(root)
            or not payload.is_relative_to(data / 'submission-drafts')):
        raise ResourceBlocked('native_draft_fault_path_outside_owned_data')
    runtime = run.runtime('primary')
    installed = Path(runtime['installed_path']).resolve()
    process = runtime.get('process') or {}
    if (runtime.get('alive') is not True or runtime.get('target') != 'windows'
            or runtime.get('artifact_sha256') != run.context['artifact_sha256']
            or type(process.get('session_id')) is not int or process['session_id'] <= 0
            or not installed.is_relative_to(root)
            or Path(process.get('path') or '').resolve() != installed
            or sha256(installed) != runtime.get('installed_sha256')):
        raise ResourceBlocked('native_draft_fault_owned_runtime_required')
    metadata_path = (payload.parent / 'draft.json').resolve()
    if metadata_path.parent != payload.parent or not metadata_path.is_relative_to(data):
        raise ResourceBlocked('native_draft_fault_path_outside_owned_data')
    metadata = json.loads(metadata_path.read_text('utf-8'))
    if (any(metadata.get(key) != fixture.get(key) for key in ('draft_id', 'course_id', 'content_id', 'sha256'))
            or payload.parent.name != fixture['draft_id'] or payload.name != metadata.get('archive_name')
            or sha256(payload) != fixture['sha256']):
        raise ResourceBlocked('native_draft_fault_identity_mismatch')
    with zipfile.ZipFile(payload) as archive:
        if archive.testzip() is not None:
            raise ResourceBlocked('native_draft_fault_original_zip_invalid')
    path = payload
    target = None
    if variant == 'changed-file':
        original = payload.read_bytes()
        injected = original + b'\nE2E real changed-file fault\n'
        boundary = 'actual-isolated-native-written-draft-bytes'
    elif variant == 'changed-target':
        courses = run.session().request_read('/api/campus/courses')['courses']
        other = next((row['id'] for row in courses if not row.get('read_only')
                      and row['id'] != fixture['course_id']), None)
        if other is None:
            raise ResourceBlocked('native_draft_fault_other_course_unverified')
        catalog = run.oracle.get('account', '/v1/catalog/' + other)
        if (type(catalog.get('revision')) is not int or catalog['revision'] <= 0
                or (catalog.get('snapshot') or {}).get('course_id') != other):
            raise ResourceBlocked('native_draft_fault_other_course_unverified')
        path = metadata_path
        original = path.read_bytes()
        metadata['course_id'] = target = other
        injected = json.dumps(metadata, ensure_ascii=False).encode('utf-8')
        boundary = 'actual-isolated-native-draft-metadata'
    else:
        path = cache_path(data, fixture['course_id']).resolve()
        if not path.is_relative_to(data):
            raise ResourceBlocked('native_draft_fault_path_outside_owned_data')
        original = path.read_bytes()
        envelope = json.loads(original)
        if envelope['snapshot']['course_id'] != fixture['course_id']:
            raise ResourceBlocked('native_draft_fault_identity_mismatch')
        rows = [row for row in envelope['snapshot']['sections']['assignments']['items']
                if row.get('content_id') == fixture['content_id']]
        if len(rows) != 1:
            raise ResourceBlocked('native_draft_fault_identity_mismatch')
        rows[0]['due_at'] = ('2026-10-12 20:00' if rows[0].get('due_at') != '2026-10-12 20:00'
                             else '2026-10-13 20:00')
        injected = json.dumps(envelope, ensure_ascii=False).encode('utf-8')
        boundary = 'actual-isolated-native-catalog-cache'
    before = hashlib.sha256(original).hexdigest()
    changed = hashlib.sha256(injected).hexdigest()
    if path.stat().st_nlink != 1:
        raise ResourceBlocked('native_draft_fault_shared_file_refused')
    if before == changed:
        raise ResourceBlocked('native_draft_fault_not_a_real_change')
    receipt = {'variant': variant, 'boundary': boundary, 'observed': False,
               'before_sha256': before, 'injected_sha256': changed, 'restored': False,
               'relative_path': path.relative_to(data).as_posix(),
               'prepared_draft_sha256': fixture['sha256'],
               'changed_target_independent_course_id': target,
               'official_deadline_mutated': False}
    run.context['_draft_fault_armed'] = True
    try:
        path.write_bytes(injected)
        receipt['observed'] = sha256(path) == changed
        if not receipt['observed']:
            raise ResourceBlocked('native_draft_fault_write_not_observed')
        yield receipt
    finally:
        try:
            # Never overwrite a concurrent student edit to "restore" a test.
            if sha256(path) != changed:
                raise ResourceBlocked('native_draft_fault_concurrent_edit_preserved')
            path.write_bytes(original)
            receipt['restored'] = sha256(path) == before
            if not receipt['restored']:
                raise ResourceBlocked('native_draft_fault_restoration_unverified')
        finally:
            run.context.pop('_draft_fault_armed', None)
            run.actions.append({'actual_native_draft_fault': dict(receipt)})
