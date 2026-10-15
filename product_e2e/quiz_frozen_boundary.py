"""Actual frozen-summary mutation and strict retirement of this run's block.

Only the installed generate POST can arm it. A successful real quiz-directory
read after accepted source reads triggers one real append, never a relay or
product-result substitute. An unknown write is retained and never replayed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .core import ResourceBlocked, sha256

PREFIX = 'PKU 总结验收版本 · '


def marker(row):
    if row.get('type') != 'paragraph':
        return None
    text = ''.join(p.get('text', {}).get('content') or p.get('plain_text', '')
                   for p in row.get('paragraph', {}).get('rich_text', []))
    if not text.startswith(PREFIX):
        return None
    try:
        value = json.loads(text[len(PREFIX):])
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get('schema') == 2 else None


def content_hash(rows):
    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: clean(v) for k, v in value.items() if k not in {'plain_text', 'href', 'expiry_time'}}
        if isinstance(result.get('file'), dict) and isinstance(result['file'].get('url'), str):
            p = urlsplit(result['file']['url'])
            result['file']['url'] = urlunsplit((p.scheme, p.netloc, p.path, '', ''))
        return result
    def canonical(blocks):
        result = []
        for row in blocks:
            if marker(row) is not None:
                continue
            kind = row['type']
            body = dict(row.get(kind, {}))
            if isinstance(body.get('children'), list):
                body['children'] = canonical(body['children'])
            result.append({'type': kind, kind: body})
        return result
    content = canonical(rows)
    return hashlib.sha256(json.dumps(clean(content), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class FrozenInputChange:
    def __init__(self, boundary, fixture, receipt):
        from .notion_fault_boundary import identifier
        self.boundary, self.owner, self.receipt = boundary, boundary.owner, receipt
        self.fixture = dict(fixture)
        for key in ('note_id', 'lecture_id', 'quiz_center_id', 'course_parent_id'):
            self.fixture[key] = identifier(fixture[key])
        if (not re.fullmatch(r'_\d+_\d+', fixture.get('course_id', ''))
                or not re.fullmatch(r'pku:recording:[0-9a-f]{32}', fixture.get('source_id', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', fixture.get('remote_sha256', ''))
                or len({self.fixture[k] for k in ('note_id', 'lecture_id', 'quiz_center_id', 'course_parent_id')}) != 4
                or boundary.parent_id in {self.fixture[k] for k in ('note_id', 'lecture_id', 'quiz_center_id', 'course_parent_id')}):
            raise ResourceBlocked('frozen_input_valid_selected_fixture_required')
        root = Path(self.owner.need('test_root')).resolve()
        app = Path(self.owner.config['instances']['primary']['app_dir']).resolve()
        if app == root or not app.is_relative_to(root):
            raise ResourceBlocked('frozen_input_isolated_profile_required')
        self.journal = app / ('frozen-input-change-' + uuid4().hex + '.json')
        self.client = None
        self.initial_rows = None
        self._lock = threading.RLock()
        receipt.update(boundary='actual-Notion-frozen-source-append-and-strict-retirement',
                       actual_source_changed=False, original_source_content_restored=False,
                       selected_source_reads=0, native_generate_post_observed=False,
                       append_attempts=0, retirement_attempts=0)

    def _runtime(self):
        runtime = self.owner.runtime('primary')
        root = Path(self.owner.need('test_root')).resolve()
        path = Path(runtime.get('installed_path', '')).resolve()
        process = runtime.get('process') or {}
        if (self.owner.mode != 'fault' or self.owner.context.get('target') != 'windows'
                or runtime.get('alive') is not True or runtime.get('target') != 'windows'
                or runtime.get('artifact_sha256') != self.owner.context['artifact_sha256']
                or not path.is_relative_to(root) or sha256(path) != runtime.get('installed_sha256')
                or Path(process.get('path') or '').resolve() != path
                or type(process.get('session_id')) is not int or process['session_id'] <= 0):
            raise ResourceBlocked('frozen_input_owned_interactive_installed_runtime_required')

    def arm_native_request(self, method, url, payload):
        parsed = urlsplit(url)
        origin = urlsplit(self.owner.sessions['primary'].origin)
        expected = '/api/campus/courses/' + self.fixture['course_id'] + '/quizzes'
        if (method != 'POST' or parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
                or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
                or parsed.path != expected or parsed.query or parsed.fragment
                or not isinstance(payload, dict) or payload.get('confirmed') is not True
                or self.fixture['source_id'] not in payload.get('lecture_ids', [])):
            raise ResourceBlocked('frozen_input_actual_native_generate_post_required')
        self._runtime()
        with self._lock:
            if self.receipt['native_generate_post_observed']:
                raise ResourceBlocked('frozen_input_more_than_one_native_generate_post')
            self.receipt['native_generate_post_observed'] = True

    def _save(self, phase):
        self.receipt['phase'] = phase
        value = {**self.receipt, 'fixture': self.fixture}
        if not self.journal.exists():
            with self.journal.open('x', encoding='utf8') as out:
                json.dump(value, out, ensure_ascii=False, indent=2)
        else:
            staged = self.journal.with_suffix('.tmp')
            staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf8')
            staged.replace(self.journal)

    def _mutate(self, client, method, route, body=None):
        row = {'host': 'api.notion.com', 'path': route, 'method': method,
               'boundary': 'actual-frozen-input-mutation', 'fault': 'changed-frozen-input',
               'request_body_sha256': hashlib.sha256(body or b'').hexdigest(),
               'forward_started': True, 'upstream_response_received': False}
        try:
            response = client.request(method, route, content=body,
                headers={'Content-Type': 'application/json'} if body else None)
            row.update(upstream_response_received=True, upstream_status=response.status_code,
                       status=response.status_code, upstream_body_sha256=hashlib.sha256(response.content).hexdigest())
            response.raise_for_status()
            return response
        except Exception as exc:
            row['error_type'] = type(exc).__name__
            raise
        finally:
            with self.boundary._lock:
                self.owner.network.append(row)

    def _rows(self, client, target, seen=None):
        seen = set() if seen is None else seen
        if target in seen or len(seen) >= 64:
            raise ResourceBlocked('frozen_input_complete_source_read_bound')
        seen.add(target)
        rows = deepcopy(self.boundary._children(client, target))
        for row in rows:
            if row.get('has_children'):
                row[row['type']]['children'] = self._rows(client, row['id'], seen)
        return rows

    def _verify(self, client):
        from .notion_fault_boundary import identifier
        f = self.fixture
        for target in (f['note_id'], f['quiz_center_id']):
            if not self.boundary._inside(client, target):
                raise ResourceBlocked('frozen_input_real_test_ancestry_required')
        for target, parent in ((f['note_id'], f['lecture_id']), (f['quiz_center_id'], f['course_parent_id'])):
            page = self.boundary._safety_get(client, '/v1/pages/' + target)
            page.raise_for_status()
            value = page.json()
            if (identifier(value.get('id')) != target or value.get('archived') or value.get('in_trash')
                    or identifier(value.get('parent', {}).get('page_id')) != parent):
                raise ResourceBlocked('frozen_input_original_parent_required')
        rows = self._rows(client, f['note_id'])
        accepted = [marker(r) for r in rows if marker(r) is not None]
        current = accepted[-1] if accepted else {}
        if (current.get('source_id') != f['source_id'] or not current.get('accepted_at')
                or identifier(current.get('note_id', '0' * 32)) != f['note_id']
                or identifier(current.get('lecture_id', '0' * 32)) != f['lecture_id']
                or current.get('remote_sha256') != f['remote_sha256']
                or content_hash(rows) != f['remote_sha256']):
            raise ResourceBlocked('frozen_input_real_accepted_summary_required')
        return rows

    def on_response(self, client, method, path, response):
        if method != 'GET' or response.status_code != 200:
            return
        with self._lock:
            if not self.receipt['native_generate_post_observed'] or self.receipt['append_attempts']:
                return
            if path == '/v1/blocks/' + self.fixture['note_id'] + '/children':
                if self.initial_rows is None:
                    self._runtime()
                    self.initial_rows = self._verify(client)
                self.receipt['selected_source_reads'] += 1
                return
            if (path != '/v1/blocks/' + self.fixture['quiz_center_id'] + '/children'
                    or self.receipt['selected_source_reads'] < 5):
                return
            self._runtime()
            self.initial_rows = self._verify(client)  # Fresh guard immediately before append.
            self.client = client
            before = {row['id'].replace('-', '').lower() for row in self.initial_rows}
            nonce = uuid4().hex
            block = {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [
                {'type': 'text', 'text': {'content': 'PKU 原生冻结来源故障 · ' + nonce}}]}}
            body = json.dumps({'children': [block]}, ensure_ascii=False).encode()
            route = '/v1/blocks/' + self.fixture['note_id'] + '/children'
            if not self.boundary._mutation_allowed(client, 'PATCH', route, body):
                raise ResourceBlocked('frozen_input_append_real_ancestry_rejected')
            self.receipt.update(append_attempts=1, request_body_sha256=hashlib.sha256(body).hexdigest())
            self._save('append_started')  # A lost response never permits a replay.
            try:
                response = self._mutate(client, 'PATCH', route, body)
            except Exception:
                self._save('append_result_unknown')
                raise
            self.boundary._remember_created_blocks(client, self.fixture['note_id'], [block], before)
            now = self._rows(client, self.fixture['note_id'])
            inserted = [r for r in now if r['id'].replace('-', '').lower() not in before]
            if len(inserted) != 1:
                self._save('append_readback_unknown')
                raise ResourceBlocked('frozen_input_unique_created_block_unverified')
            block_id = inserted[0]['id'].replace('-', '').lower()
            if block_id not in self.boundary._created_blocks:
                self._save('append_ownership_unknown')
                raise ResourceBlocked('frozen_input_new_block_ownership_unverified')
            restored = [r for r in now if r['id'].replace('-', '').lower() != block_id]
            if content_hash(restored) != self.fixture['remote_sha256'] or content_hash(now) == self.fixture['remote_sha256']:
                self._save('append_content_unknown')
                raise ResourceBlocked('frozen_input_original_source_changed_concurrently')
            self.receipt.update(observed=True, actual_source_changed=True, generated_block_id=block_id,
                original_source_sha256=self.fixture['remote_sha256'], changed_source_sha256=content_hash(now))
            self._save('append_verified')

    def restore(self):
        with self._lock:
            r = self.receipt
            if r.get('original_source_content_restored'):
                return
            if not r.get('generated_block_id'):
                if r['append_attempts']:
                    raise ResourceBlocked('frozen_input_unknown_append_preserved_without_replay')
                return
            target = r['generated_block_id']
            route = '/v1/blocks/' + target
            if not self.boundary._mutation_allowed(self.client, 'DELETE', route, b''):
                self._save('retirement_guard_rejected')
                raise ResourceBlocked('frozen_input_strict_retirement_guard_rejected')
            if r['retirement_attempts']:
                raise ResourceBlocked('frozen_input_unknown_retirement_preserved_without_replay')
            r['retirement_attempts'] = 1
            self._save('retirement_started')
            try:
                response = self._mutate(self.client, 'DELETE', route)
            except Exception:
                self._save('retirement_result_unknown')
                raise
            actual = self.boundary._safety_get(self.client, route)
            actual.raise_for_status()
            if actual.json().get('archived') is not True:
                raise ResourceBlocked('frozen_input_retirement_independent_readback_failed')
            rows = self._rows(self.client, self.fixture['note_id'])
            if content_hash(rows) != self.fixture['remote_sha256']:
                self._save('original_source_not_restored')
                raise ResourceBlocked('frozen_input_original_content_restore_unverified')
            r.update(original_source_content_restored=True, archived_generated_block_verified=True)
            self._save('restored')
