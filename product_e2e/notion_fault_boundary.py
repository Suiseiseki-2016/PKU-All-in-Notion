"""Owned HTTP boundary; actual Notion writes precede response-loss faults.

No business functions or independent oracles use substituted state. Every
mutation verifies ancestry against the real service before forwarding.
"""
from contextlib import contextmanager
import hashlib
from datetime import datetime
from email.parser import BytesParser
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .core import ResourceBlocked

SUPPORTED = {'create-lost-response', 'append-lost-response', 'readback-fails', 'lost-notion-response',
             'permission-archive-move', 'changed-frozen-input'}
_ID = re.compile(r'^[0-9a-fA-F]{32}$')


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value.replace('-', '')):
        raise ValueError('invalid Notion identity')
    return value.replace('-', '').lower()


def safe_throttle_fields(response):
    """Observe only allowlisted throttle metadata; leave the response intact."""
    if response.status_code != 429:
        return {}
    try:
        details = response.json().get('additional_data') or {}
    except (ValueError, AttributeError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    delay = None
    for value in (response.headers.get('Retry-After'), details.get('retry_after')):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            number = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(number) and number >= 0:
            delay = number
            break
    reason = details.get('rate_limit_reason')
    return {'retry_after_seconds': delay, 'rate_limit_reason': reason if isinstance(reason, str) and reason in {
        'public_api_request_rate_limit', 'public_api_request_blocked'} else None}


class NotionFaultBoundary:
    def __init__(self, owner, parent_id):
        self.owner, self.parent_id = owner, identifier(parent_id)
        self.variant = self.receipt = None
        self._lock = threading.Lock()
        self._clients = {}
        self._created_blocks = {}
        self._media_files = {}
        self._media_uploads = {}
        self._media_attempts = set()
        self._media_lock = threading.Lock()
        self._frozen_change = None
        boundary = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                boundary.handle(self)

            do_POST = do_PATCH = do_DELETE = do_GET

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for client in self._clients.values():
            client.close()

    def _upstream(self, headers):
        # Keep verified HTTPS connections alive during large directory reads.
        # Pools are separated by credential and API version, never by a page
        # title; credentials stay in memory and never enter evidence.
        key = hashlib.sha256((headers.get('Authorization', '') + '\0' +
                              headers.get('Notion-Version', '')).encode()).hexdigest()
        with self._lock:
            if key not in self._clients:
                self._clients[key] = httpx.Client(base_url='https://api.notion.com', headers=headers,
                    timeout=30, trust_env=False, follow_redirects=False,
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=60))
            return self._clients[key]

    @contextmanager
    def inject(self, variant):
        if variant not in SUPPORTED:
            raise ResourceBlocked('software_capability_missing_notion_fault_' + variant)
        with self._lock:
            if self.variant is not None:
                raise ResourceBlocked('overlapping_notion_fault')
            receipt = {'variant': variant, 'boundary': 'actual-notion-http',
                       'observed': False, 'request_count': 0}
            if variant == 'permission-archive-move':
                receipt.update(server_pages_mutated=False, current_case=None,
                    cases={kind: {'observed': False, 'request_count': 0}
                           for kind in ('permission', 'archived', 'moved')})
            self.variant, self.receipt = variant, receipt
        try:
            yield receipt
        finally:
            try:
                if self._frozen_change is not None:
                    self._frozen_change.restore()
                if variant == 'permission-archive-move' and not all(
                        row['observed'] for row in receipt['cases'].values()):
                    raise ResourceBlocked('notion_access_fault_subcases_incomplete')
                if not receipt['observed']:
                    raise ResourceBlocked('notion_fault_did_not_touch_real_request')
            finally:
                with self._lock:
                    self.variant = self.receipt = self._frozen_change = None

    def configure_frozen_input(self, fixture):
        from .quiz_frozen_boundary import FrozenInputChange
        with self._lock:
            if self.variant != 'changed-frozen-input' or self.receipt is None or self._frozen_change is not None:
                raise ResourceBlocked('frozen_input_fault_not_armed_or_already_configured')
            self._frozen_change = FrozenInputChange(self, fixture, self.receipt)
            return self._frozen_change

    def configure_access_case(self, kind, target, parent):
        target, parent = identifier(target), identifier(parent)
        if kind not in {'permission', 'archived', 'moved'} or target == self.parent_id or target == parent:
            raise ValueError('invalid access fault fixture')
        with self._lock:
            if self.variant != 'permission-archive-move' or self.receipt is None:
                raise ResourceBlocked('notion_access_fault_not_armed')
            self.receipt.update(current_case=kind, target_id=target, parent_id=parent)

    def _access_corruption(self, client, state, method, path, response):
        """Corrupt only successful owned metadata, never the actual remote page."""
        if not state or method != 'GET' or response.status_code != 200:
            return None
        kind, target, parent, receipt = state
        match = re.fullmatch(r'/v1/(pages|blocks)/([0-9a-fA-F-]+)(/children)?', path)
        if not match:
            return None
        path_id = identifier(match[2])
        page_read = match[1] == 'pages' and path_id == target and match[3] is None
        membership_read = kind == 'moved' and match[1] == 'blocks' and path_id == parent and match[3] == '/children'
        if not (page_read or membership_read) or not self._inside(client, target):
            return None
        value = response.json()
        if page_read:
            if (value.get('object') != 'page' or identifier(value.get('id')) != target
                    or value.get('archived') or value.get('in_trash')
                    or identifier((value.get('parent') or {}).get('page_id')) != parent):
                return None
            if kind == 'permission':
                value = {'object': 'error', 'code': 'restricted_resource',
                         'message': 'controlled access failure after real owned page read'}
                status = 403
            elif kind == 'archived':
                value['archived'], status = True, 200
            else:
                value['parent'] = {'type': 'page_id', 'page_id': 'f' * 32}
                status = 200
        else:
            rows = value.get('results')
            if not isinstance(rows, list):
                return None
            kept = [row for row in rows if identifier(row.get('id')) != target]
            if len(kept) == len(rows):
                return None  # An unrelated pagination page is not an observation.
            value['results'], status = kept, 200
        delivered = json.dumps(value, ensure_ascii=False).encode('utf-8')
        with self._lock:
            receipt['observed'] = True
            receipt['request_count'] += 1
            receipt['cases'][kind]['observed'] = True
            receipt['cases'][kind]['request_count'] += 1
        return status, delivered, kind

    def _inside(self, client, target, *, block=False):
        target = identifier(target)
        seen = set()
        for _ in range(30):
            if target == self.parent_id:
                return True
            if target in seen:
                return False
            seen.add(target)
            response = self._safety_get(client, '/v1/' + ('blocks/' if block else 'pages/') + target)
            response.raise_for_status()
            parent = response.json().get('parent', {})
            kind = parent.get('type')
            if kind not in {'page_id', 'block_id', 'database_id'}:
                return False
            # Database mutation is outside this focused page/block boundary.
            if kind == 'database_id':
                return False
            target, block = identifier(parent.get(kind)), kind == 'block_id'
        return False

    def _safety_get(self, client, path, **kwargs):
        started = time.monotonic()
        try:
            response = client.get(path, **kwargs)
        except httpx.HTTPError as exc:
            with self._lock:
                self.owner.network.append({'host': 'api.notion.com', 'path': path, 'method': 'GET',
                    'boundary': 'notion-safety-read', 'status': None, 'upstream_status': None,
                    'upstream_response_received': False, 'error_type': type(exc).__name__,
                    'elapsed_seconds': time.monotonic() - started, 'fault': None})
            raise
        with self._lock:
            self.owner.network.append({'host': 'api.notion.com', 'path': path, 'method': 'GET',
                'boundary': 'notion-safety-read', 'status': response.status_code,
                'upstream_status': response.status_code, 'fault': None,
                'started_monotonic': started, 'elapsed_seconds': time.monotonic() - started,
                'upstream_body_sha256': hashlib.sha256(response.content).hexdigest()})
        return response

    def _children(self, client, target):
        result, seen, cursor = [], set(), None
        while True:
            params = {'page_size': 100}
            if cursor:
                params['start_cursor'] = cursor
            response = self._safety_get(client, '/v1/blocks/' + target + '/children', params=params)
            response.raise_for_status()
            value = response.json()
            result.extend(value['results'])
            if not value.get('has_more'):
                return result
            cursor = value.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError('incomplete Notion ownership read')
            seen.add(cursor)

    def _block_shape(self, client, row):
        if row.get('type') == 'table' and row.get('has_children'):
            row['table'] = {**row['table'], 'children': self._children(client, row['id'])}
        kind = row.get('type')
        # Keep every content field, including images, styles and nested cells.
        # Source-document signatures alone do not cover all block kinds.
        return {'id': identifier(row['id']), 'type': kind,
                'body_sha256': hashlib.sha256(json.dumps(row.get(kind), ensure_ascii=False,
                                                        sort_keys=True).encode()).hexdigest()}

    def _remember_created_blocks(self, client, parent, requested, before):
        from pku_sync.projection_notion_effects import _subset
        actual = self._children(client, parent)
        inserted = [row for row in actual if identifier(row['id']) not in before]
        # A partial/ambiguous effect grants no retirement authority. This
        # in-memory inventory covers only real writes made by this runner.
        if len(inserted) != len(requested):
            return
        shapes = [self._block_shape(client, row) for row in inserted]
        if not all(_subset(wanted, row) for wanted, row in zip(requested, inserted)):
            return
        with self._lock:
            for row, shape in zip(inserted, shapes):
                if row.get('type') not in {'child_page', 'child_database'}:
                    self._created_blocks[identifier(row['id'])] = {'parent': identifier(parent), 'shape': shape}

    def _retirement_allowed(self, client, target):
        with self._lock:
            owned = self._created_blocks.get(identifier(target))
        if owned is None or not self._inside(client, target, block=True):
            return False
        response = self._safety_get(client, '/v1/blocks/' + target)
        response.raise_for_status()
        row = response.json()
        parent = row.get('parent', {})
        kind = parent.get('type')
        return (not row.get('archived') and not row.get('in_trash')
                and kind in {'page_id', 'block_id'} and identifier(parent[kind]) == owned['parent']
                and self._block_shape(client, row) == owned['shape'])

    def configure_media_uploads(self, scope):
        """Freeze exact owned media bytes and their approved lecture ancestor.

        This grants no page retirement and never supplies a native verdict.
        Uploads without this explicit manifest remain rejected.
        """
        if self._media_files or not isinstance(scope, dict) or set(scope) != {'parent_id', 'source_id', 'files'}:
            raise ValueError('invalid or repeated media scope')
        parent = identifier(scope['parent_id'])
        source_id = scope['source_id']
        if parent == self.parent_id or not isinstance(source_id, str) or not re.fullmatch(r'pku:recording:[0-9a-f]{32}', source_id):
            raise ValueError('exact lecture source required')
        root = Path(self.owner.need('test_root')).resolve()
        files = scope['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 64:
            raise ValueError('bounded exact media manifest required')
        prepared = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'content_type'}:
                raise ValueError('invalid media manifest row')
            path = Path(item['path']).resolve(strict=True)
            digest = item['sha256']
            content_type = item['content_type']
            if (not path.is_relative_to(root) or not path.is_file()
                    or path.parent.name not in {'keyframes', 'video-clips'}
                    or content_type not in {'image/jpeg', 'image/png', 'video/mp4'}
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    or not 0 < path.stat().st_size <= 20 * 1024 * 1024
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                    or path.name in prepared):
                raise ValueError('media source outside exact owned scope')
            prepared[path.name] = {'path': path, 'sha256': digest, 'content_type': content_type,
                                   'size': path.stat().st_size, 'parent': parent, 'source_id': source_id}
        self._media_files = prepared

    def _media_lecture_matches(self, client, source):
        from pku_sync.panel.notion_home import _lecture_marker, _source_block_signature
        response = self._safety_get(client, '/v1/pages/' + source['parent'])
        response.raise_for_status()
        page = response.json()
        if (page.get('object') != 'page' or identifier(page.get('id')) != source['parent']
                or page.get('archived') or page.get('in_trash')
                or not self._inside(client, source['parent'])):
            return False
        expected = _source_block_signature(_lecture_marker({'source_id': source['source_id']}))
        rows = self._children(client, source['parent'])
        return sum(_source_block_signature(row) == expected for row in rows) == 1

    @staticmethod
    def _media_credential(client):
        return hashlib.sha256(client.headers.get('Authorization', '').encode()).hexdigest()

    def _media_source(self, client, body):
        value = json.loads(body)
        if (not isinstance(value, dict) or set(value) != {'filename', 'content_type', 'mode'}
                or value['mode'] != 'single_part'):
            return None
        source = self._media_files.get(value['filename'])
        if (source is None or value['content_type'] != source['content_type']
                or not source['path'].is_file() or source['path'].stat().st_size != source['size']
                or hashlib.sha256(source['path'].read_bytes()).hexdigest() != source['sha256']
                or (self._media_credential(client), value['filename']) in self._media_attempts
                or not self._media_lecture_matches(client, source)):
            return None
        return source

    def _media_send_allowed(self, client, path, body, content_type):
        match = re.fullmatch(r'/v1/file_uploads/([0-9a-fA-F-]+)/send', path)
        if not match:
            return False
        upload = self._media_uploads.get(identifier(match[1]))
        if (upload is None or upload['credential'] != self._media_credential(client)
                or upload.get('send_attempted') or upload['status'] != 'pending'
                or upload['expiry'] <= time.time() or not content_type.startswith('multipart/form-data;')):
            return False
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        if not message.is_multipart() or message.defects:
            return False
        parts = list(message.iter_parts())
        if len(parts) != 1:
            return False
        part = parts[0]
        data = part.get_payload(decode=True)
        source = upload['source']
        return (part.get_param('name', header='content-disposition') == 'file'
                and part.get_filename() == source['path'].name
                and part.get_content_type() == source['content_type']
                and isinstance(data, bytes) and len(data) == source['size']
                and hashlib.sha256(data).hexdigest() == source['sha256']
                and source['path'].is_file()
                and hashlib.sha256(source['path'].read_bytes()).hexdigest() == source['sha256']
                and self._media_lecture_matches(client, source))

    @staticmethod
    def _upload_refs(value):
        result = []
        if isinstance(value, dict):
            if value.get('type') == 'file_upload':
                result.append(identifier(value['file_upload']['id']))
            for item in value.values():
                result.extend(NotionFaultBoundary._upload_refs(item))
        elif isinstance(value, list):
            for item in value:
                result.extend(NotionFaultBoundary._upload_refs(item))
        return result

    def _media_attachment_allowed(self, client, target, value):
        refs = self._upload_refs(value)
        if len(set(refs)) != len(refs):
            return False
        for ref in refs:
            upload = self._media_uploads.get(ref)
            if (upload is None or upload['credential'] != self._media_credential(client)
                    or upload['status'] != 'uploaded' or upload.get('attach_attempted')
                    or upload['expiry'] <= time.time()):
                return False
            ancestor, seen = identifier(target), set()
            for _ in range(30):
                if ancestor == upload['source']['parent']:
                    break
                if ancestor in seen or ancestor == self.parent_id:
                    return False
                seen.add(ancestor)
                response = self._safety_get(client, '/v1/blocks/' + ancestor)
                response.raise_for_status()
                page = response.json()
                parent = page.get('parent') or {}
                kind = parent.get('type')
                if (identifier(page.get('id')) != ancestor or page.get('object') not in {'block', 'page'}
                        or page.get('archived') or page.get('in_trash') or kind not in {'page_id', 'block_id'}):
                    return False
                ancestor = identifier(parent[kind])
            else:
                return False
            response = self._safety_get(client, '/v1/file_uploads/' + ref)
            response.raise_for_status()
            current = response.json()
            source = upload['source']
            if (current.get('object') != 'file_upload' or identifier(current.get('id')) != ref
                    or current.get('status') != 'uploaded' or current.get('in_trash') or current.get('archived')
                    or current.get('filename') != source['path'].name
                    or current.get('content_type') != source['content_type']
                    or type(current.get('content_length')) is not int
                    or current['content_length'] != source['size']
                    or not isinstance(current.get('expiry_time'), str)
                    or datetime.fromisoformat(current['expiry_time'].replace('Z', '+00:00')).timestamp() <= time.time()
                    or not self._media_lecture_matches(client, source)):
                return False
        return True

    def _reserve_media(self, client, method, path, body):
        if method == 'POST' and path == '/v1/file_uploads':
            value = json.loads(body)
            self._media_attempts.add((self._media_credential(client), value['filename']))
        match = re.fullmatch(r'/v1/file_uploads/([0-9a-fA-F-]+)/send', path)
        if method == 'POST' and match:
            self._media_uploads[identifier(match[1])]['send_attempted'] = True
        if method in {'POST', 'PATCH'} and not path.startswith('/v1/file_uploads'):
            for ref in self._upload_refs(json.loads(body) if body else {}):
                self._media_uploads[ref]['attach_attempted'] = True

    def _remember_media_response(self, client, method, path, body, value):
        if method == 'POST' and path == '/v1/file_uploads':
            request = json.loads(body)
            source = self._media_files.get(request.get('filename'))
            upload_id = identifier(value['id'])
            expiry = datetime.fromisoformat(value['expiry_time'].replace('Z', '+00:00')).timestamp()
            if (source is None or value.get('object') != 'file_upload' or value.get('status') != 'pending'
                    or expiry <= time.time() or upload_id in self._media_uploads):
                raise ValueError('unknown media creation response')
            self._media_uploads[upload_id] = {'source': source, 'credential': self._media_credential(client),
                'expiry': expiry, 'status': 'pending', 'send_attempted': False, 'attach_attempted': False}
        match = re.fullmatch(r'/v1/file_uploads/([0-9a-fA-F-]+)/send', path)
        if method == 'POST' and match:
            upload_id = identifier(match[1])
            upload = self._media_uploads[upload_id]
            if identifier(value.get('id')) != upload_id or value.get('status') != 'uploaded':
                raise ValueError('unknown media send response')
            upload['status'] = 'uploaded'
        if method in {'POST', 'PATCH'} and not path.startswith('/v1/file_uploads'):
            for ref in self._upload_refs(json.loads(body) if body else {}):
                self._media_uploads[ref]['attachment_response_confirmed'] = True

    def _media_known_throttle(self, client, method, path, body):
        # An explicit throttle did not commit. Unknown/5xx responses keep the
        # attempt reserved and must never authorize an automatic replay.
        with self._media_lock:
            if method == 'POST' and path == '/v1/file_uploads':
                self._media_attempts.discard((self._media_credential(client), json.loads(body)['filename']))
            match = re.fullmatch(r'/v1/file_uploads/([0-9a-fA-F-]+)/send', path)
            if method == 'POST' and match:
                self._media_uploads[identifier(match[1])]['send_attempted'] = False
            if method in {'POST', 'PATCH'} and not path.startswith('/v1/file_uploads'):
                for ref in self._upload_refs(json.loads(body) if body else {}):
                    self._media_uploads[ref]['attach_attempted'] = False

    def media_upload_receipts(self):
        # Upstream response evidence is not independent content verification.
        with self._media_lock:
            snapshot = list(self._media_uploads.items())
        return [{'upload_id': key, 'source_sha256': row['source']['sha256'],
                 'source_size': row['source']['size'], 'parent_id': row['source']['parent'],
                 'source_id': row['source']['source_id'],
                 'status': row['status'], 'send_attempted': row['send_attempted'],
                 'attachment_response_confirmed': row.get('attachment_response_confirmed', False),
                 'independent_content_verified': False}
                for key, row in snapshot]

    def _mutation_allowed(self, client, method, path, body):
        if method == 'GET' or (method == 'POST' and path == '/v1/search'):
            return True
        if method == 'POST' and path == '/v1/pages':
            value = json.loads(body)
            parent = value.get('parent', {})
            return (bool(parent.get('page_id')) and self._inside(client, parent['page_id'])
                    and self._media_attachment_allowed(client, parent['page_id'], value))
        if method == 'POST' and path == '/v1/file_uploads':
            return self._media_source(client, body) is not None
        found = re.fullmatch(r'/v1/(pages|blocks)/([0-9a-fA-F-]+)(/children)?', path)
        if found and method == 'DELETE' and found[1] == 'blocks' and not found[3] and not body:
            return self._retirement_allowed(client, found[2])
        if found and method == 'PATCH':
            value = json.loads(body)
            if value.get('archived') or value.get('in_trash'):
                return False  # Whole pages and pre-existing content stay protected.
            if value.get('parent') and not self._inside(client, value['parent'].get('page_id')):
                return False
            return (self._inside(client, found[2], block=found[1] == 'blocks')
                    and self._media_attachment_allowed(client, found[2], value))
        return False

    @staticmethod
    def _send(handler, status, body, headers=None):
        try:
            handler.send_response(status)
            handler.send_header('Content-Type', (headers or {}).get('content-type', 'application/json'))
            handler.send_header('Content-Length', str(len(body)))
            if (headers or {}).get('retry-after'):
                handler.send_header('Retry-After', headers['retry-after'])
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Closing the actual native window cancels background reads. Do
            # not send another response on the already-disconnected socket.
            handler.close_connection = True

    def handle(self, handler):
        parts = urlsplit(handler.path)
        path, method = parts.path, handler.command
        started = time.monotonic()
        stage, forward_started, response, body = 'request-validation', False, None, b''
        if (parts.scheme or parts.netloc or parts.fragment or '\\' in path
                or '..' in path.split('/') or not path.startswith('/v1/')):
            return self._send(handler, 400, b'{"message":"invalid metadata path"}')
        try:
            size = int(handler.headers.get('Content-Length', '0'))
            media_send = method == 'POST' and re.fullmatch(r'/v1/file_uploads/[0-9a-fA-F-]+/send', path)
            if size < 0 or size > ((20 * 1024 * 1024 + 65536) if media_send else 4 * 1024 * 1024):
                return self._send(handler, 413, b'{"message":"bounded metadata only"}')
            body = handler.rfile.read(size)
            headers = {name: handler.headers[name] for name in
                       ('Authorization', 'Notion-Version', 'Content-Type') if name in handler.headers}
            if not headers.get('Authorization', '').startswith('Bearer '):
                return self._send(handler, 401, b'{"message":"authorization required"}')
            client = self._upstream(headers)
            with self._lock:
                current = self.receipt if self.variant == 'permission-archive-move' else None
                access_state = ((current['current_case'], current['target_id'], current['parent_id'], current)
                    if current and current.get('current_case') else None)
            stage = 'mutation-ancestry'
            # Validate and reserve together: two threads cannot authorize the
            # same file/send/attachment before either reaches upstream.
            with self._media_lock:
                permitted = (self._media_send_allowed(client, path, body, headers.get('Content-Type', ''))
                             if media_send else self._mutation_allowed(client, method, path, body))
                if permitted:
                    self._reserve_media(client, method, path, body)
            if not permitted:
                return self._send(handler, 403, b'{"message":"outside authorized test parent"}')
            scope = None
            value = json.loads(body) if body and method in {'POST', 'PATCH'} and not media_send else {}
            stage = 'ownership-preflight'
            if method == 'PATCH' and re.fullmatch(r'/v1/blocks/[0-9a-fA-F-]+/children', path):
                target = identifier(path.split('/')[-2])
                scope = (target, value.get('children') or [],
                         {identifier(row['id']) for row in self._children(client, target)})
            stage, forward_started = 'upstream-request', True
            response = client.request(method, handler.path, content=body, headers=headers)
            if response.status_code in {429, 529}:
                self._media_known_throttle(client, method, path, body)
            stage = 'ownership-readback'
            if 200 <= response.status_code < 300:
                with self._media_lock:
                    self._remember_media_response(client, method, path, body, response.json())
                if method == 'POST' and path == '/v1/pages' and value.get('children'):
                    scope = (identifier(response.json()['id']), value['children'], set())
                if scope and scope[1]:
                    self._remember_created_blocks(client, *scope)
            access = self._access_corruption(client, access_state, method, path, response)
            with self._lock:
                frozen_change = self._frozen_change
            if frozen_change is not None:
                frozen_change.on_response(client, method, path, response)
            with self._lock:
                variant, receipt = self.variant, self.receipt
                create = method == 'POST' and path == '/v1/pages'
                append = method == 'PATCH' and path.endswith('/children')
                eligible = (variant == 'create-lost-response' and create
                            or variant == 'append-lost-response' and append
                            or variant == 'lost-notion-response' and (create or append)
                            or variant == 'readback-fails' and method == 'GET' and path.endswith('/children'))
                # Loss follows a confirmed real write. Readback failures remain
                # active for every bounded product retry in this fault interval.
                inject = bool(receipt and eligible and 200 <= response.status_code < 300
                              and (variant == 'readback-fails' or not receipt['observed']))
                if inject:
                    receipt['observed'] = True
                    receipt['request_count'] += 1
                record = {'host': 'api.notion.com', 'path': path, 'method': method,
                          'boundary': 'notion', 'upstream_status': response.status_code,
                          'started_monotonic': started, 'elapsed_seconds': time.monotonic() - started,
                          'request_body_sha256': hashlib.sha256(body).hexdigest(),
                          'upstream_body_sha256': hashlib.sha256(response.content).hexdigest(),
                          'fault': variant if inject else None,
                          'status': 503 if inject and variant == 'readback-fails' else response.status_code}
                record.update(safe_throttle_fields(response))
                if access:
                    record.update(fault='permission-archive-move', status=access[0], access_case=access[2],
                                  server_page_mutated=False,
                                  delivered_body_sha256=hashlib.sha256(access[1]).hexdigest())
                self.owner.network.append(record)
            if access:
                return self._send(handler, access[0], access[1], response.headers)
            if inject and variant != 'readback-fails':
                handler.close_connection = True
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
                return
            if inject:
                return self._send(handler, 503, b'{"object":"error","code":"service_unavailable","message":"injected readback failure after real read"}')
            self._send(handler, response.status_code, response.content, response.headers)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as exc:
            # Keep the distinction between a failed pre-write guard, an unknown
            # write response, and a successful write followed by failed ownership
            # verification. Never log credentials, request bodies or exception
            # text (transport exceptions may contain signed URLs).
            record = {'host': 'api.notion.com', 'path': path, 'method': method,
                      'boundary': 'notion-boundary-error', 'stage': stage,
                      'status': 503, 'error_type': type(exc).__name__,
                      'elapsed_seconds': time.monotonic() - started,
                      'forward_started': forward_started,
                      'upstream_response_received': response is not None,
                      'upstream_status': response.status_code if response is not None else None,
                      'request_body_sha256': hashlib.sha256(body).hexdigest(), 'fault': None}
            if response is not None:
                record['upstream_body_sha256'] = hashlib.sha256(response.content).hexdigest()
            with self._lock:
                self.owner.network.append(record)
            self._send(handler, 503, b'{"message":"real upstream or ancestry verification unavailable"}')
