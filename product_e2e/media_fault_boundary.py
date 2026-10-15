"""A source-scoped HTTPS request boundary, never a product result substitute.

The installed client explicitly routes only PKU resource/player API hosts here.
Every response comes from a real upstream GET. An armed fault delivers 403 for
the selected media until a second genuine successful metadata read occurs.
Independent oracles retain their own direct clients and never use this route.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .core import ResourceBlocked, sha256

SUPPORTED = {'expired-media-url'}
HOSTS = {'resourcese.pku.edu.cn', 'yjapise.pku.edu.cn'}
INFO_PATH = '/vlabpassportapi/person/course-api/get-course-subject-info'
_HOP = {'host', 'connection', 'proxy-connection', 'proxy-authorization',
        'transfer-encoding', 'keep-alive', 'upgrade', 'te', 'trailer'}


class MediaFaultBoundary:
    def __init__(self, owner, fixture):
        self.owner, self.fixture = owner, dict(fixture)
        if (owner.context.get('target') != 'windows' or owner.mode != 'fault'
                or not re.fullmatch(r'_\d+_\d+', fixture.get('course_id', ''))
                or not re.fullmatch(r'pku:recording:[0-9a-f]{32}', fixture.get('source_id', ''))
                or not all(re.fullmatch(r'[0-9a-f]{64}', fixture.get(k, ''))
                           for k in ('resource_path_sha256', 'original_media_sha256'))
                or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', fixture.get(k, ''))
                           for k in ('platform_course_id', 'subject_id'))):
            raise ResourceBlocked('media_fault_verified_source_fixture_required')
        root = Path(owner.need('test_root')).resolve()
        data = Path(owner.config['instances']['primary']['app_dir']).resolve()
        if (data == root or not data.is_relative_to(root)
                or data == (Path.home() / 'PKU-All-in-Notion').resolve()):
            raise ResourceBlocked('media_fault_owned_isolated_profile_required')
        self.generation = 0
        self.receipt = None
        self._lock = threading.Lock()
        self._client = httpx.Client(verify=True, trust_env=False, timeout=20,
            follow_redirects=False, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))
        cert_dir = root / ('native-media-route-' + uuid4().hex)
        cert_dir.mkdir(mode=0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Owned native media request route')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in sorted(HOSTS)]), False)
                .sign(key, hashes.SHA256()))
        key_path, cert_path = cert_dir / 'key.pem', cert_dir / 'cert.pem'
        for path, value in (
            (key_path, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
            (cert_path, cert.public_bytes(serialization.Encoding.PEM))):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
                out.write(value)
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls.load_cert_chain(str(cert_path), str(key_path))
        boundary = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_CONNECT(self):
                parsed = urlsplit('https://' + self.path)
                if parsed.hostname not in HOSTS or parsed.port != 443 or parsed.path:
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    self.connection = boundary._tls.wrap_socket(self.connection, server_side=True)
                    self.connection.settimeout(30)
                    self.rfile = self.connection.makefile('rb')
                    self.wfile = self.connection.makefile('wb')
                    self.tunnel_host = parsed.hostname
                    self.close_connection = True
                    self.handle_one_request()
                except (ssl.SSLError, OSError):
                    self.close_connection = True
            def do_GET(self): boundary.handle(self)
            def do_POST(self):
                # Consume only a small already-declared body before closing the
                # TLS stream; otherwise unread bytes can reset the connection
                # before the caller receives the explicit write rejection.
                length = self.headers.get('Content-Length', '0')
                if length.isdigit() and int(length) <= 4096:
                    self.rfile.read(int(length))
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
            do_PUT = do_PATCH = do_DELETE = do_POST

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._client.close()

    def _owned_runtime(self):
        runtime = self.owner.runtime('primary')
        root = Path(self.owner.need('test_root')).resolve()
        installed = Path(runtime['installed_path']).resolve()
        process = runtime.get('process') or {}
        if (runtime.get('alive') is not True or runtime.get('target') != 'windows'
                or runtime.get('artifact_sha256') != self.owner.context['artifact_sha256']
                or type(process.get('session_id')) is not int or process['session_id'] <= 0
                or not installed.is_relative_to(root)
                or Path(process.get('path') or '').resolve() != installed
                or sha256(installed) != runtime.get('installed_sha256')):
            raise ResourceBlocked('media_fault_owned_interactive_installed_runtime_required')

    @contextmanager
    def inject(self, variant):
        if variant not in SUPPORTED:
            raise ResourceBlocked('software_capability_missing_media_fault_' + variant)
        self._owned_runtime()
        with self._lock:
            if self.receipt is not None:
                raise ResourceBlocked('overlapping_native_media_fault')
            receipt = {'variant': variant, 'boundary': 'actual-source-scoped-media-HTTPS',
                'observed': False, 'metadata_reads': 0, 'expired_responses': 0,
                'source_id': self.fixture['source_id'], 'server_media_modified': False,
                'initial_generation': self.generation}
            self.receipt = receipt
        try:
            yield receipt
        finally:
            with self._lock:
                self.receipt = None
            if not receipt['observed']:
                raise ResourceBlocked('native_media_expiry_fault_did_not_touch_real_source_request')

    def _metadata(self, parts, body):
        query = parse_qs(parts.query)
        if (parts.hostname != 'yjapise.pku.edu.cn' or parts.path != INFO_PATH
                or query.get('course_id') != [self.fixture['platform_course_id']]
                or query.get('sub_id') != [self.fixture['subject_id']]):
            return False
        try:
            value = json.loads(body)
            address = value['data']['content']['playback']['url']
            original = urlsplit(address)
            return (type(value['code']) is int and value['code'] == 0
                and original.scheme == 'https' and original.hostname == 'resourcese.pku.edu.cn'
                and not original.username and not original.password
                and hashlib.sha256(original.path.encode()).hexdigest() == self.fixture['resource_path_sha256'])
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def handle(self, handler):
        host = getattr(handler, 'tunnel_host', None)
        supplied = urlsplit('https://' + handler.headers.get('Host', ''))
        if (host not in HOSTS or supplied.hostname != host or supplied.port not in (None, 443)
                or not handler.path.startswith('/') or handler.path.startswith('//')):
            handler.send_error(403)
            return
        url = 'https://' + host + handler.path
        parts = urlsplit(url)
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP}
        row = {'host': host, 'method': 'GET', 'path_sha256': hashlib.sha256(parts.path.encode()).hexdigest(),
            'boundary': 'media-HTTPS', 'upstream_tls_verified': True, 'fault': None,
            'upstream_body_consumed_bytes': 0, 'source_verified': False}
        if parts.path == INFO_PATH:
            row['path'] = INFO_PATH
        response = None
        try:
            response = self._client.send(self._client.build_request('GET', url, headers=headers), stream=True)
            row.update(upstream_status=response.status_code, upstream_response_received=True)
            evidence_headers = {key: response.headers.get(key, '') for key in
                                ('content-length', 'content-range', 'content-type', 'etag', 'last-modified')}
            row['upstream_header_sha256'] = hashlib.sha256(json.dumps(evidence_headers, sort_keys=True).encode()).hexdigest()
            if host == 'yjapise.pku.edu.cn':
                buffered = bytearray()
                for chunk in response.iter_bytes(65536):
                    if len(buffered) + len(chunk) > 4 * 1024 * 1024:
                        raise ValueError('media metadata response exceeds bound')
                    buffered.extend(chunk)
                body = bytes(buffered)
                row['upstream_body_consumed_bytes'] = len(body)
                row['upstream_decoded_body_sha256'] = hashlib.sha256(body).hexdigest()
                if response.status_code == 200 and self._metadata(parts, body):
                    with self._lock:
                        self.generation += 1
                        row.update(source_verified=True, metadata_generation=self.generation)
                        if self.receipt is not None:
                            self.receipt['metadata_reads'] += 1
                handler.send_response(response.status_code)
                for k, v in response.headers.items():
                    if k.lower() not in _HOP | {'content-length', 'content-encoding'}:
                        handler.send_header(k, v)
                handler.send_header('Content-Length', str(len(body)))
                handler.send_header('Connection', 'close')
                handler.end_headers()
                handler.wfile.write(body)
                row.update(status=response.status_code, delivered_body_bytes=len(body),
                           delivered_body_sha256=hashlib.sha256(body).hexdigest())
            else:
                selected = row['path_sha256'] == self.fixture['resource_path_sha256']
                with self._lock:
                    receipt = self.receipt
                    expired = (receipt is not None and selected and response.status_code in (200, 206)
                               and self.generation == receipt['initial_generation'] + 1)
                    row.update(source_verified=selected, metadata_generation=self.generation)
                    if expired:
                        receipt.update(observed=True, expired_responses=receipt['expired_responses'] + 1)
                if expired:
                    body = b'<Error><Code>ExpiredToken</Code><Message>Media address expired</Message></Error>'
                    handler.send_response(403)
                    handler.send_header('Content-Type', 'application/xml')
                    handler.send_header('Content-Length', str(len(body)))
                    handler.send_header('Connection', 'close')
                    handler.end_headers()
                    handler.wfile.write(body)
                    row.update(fault='expired-media-url', status=403, server_media_modified=False,
                               delivered_body_bytes=len(body), delivered_body_sha256=hashlib.sha256(body).hexdigest())
                else:
                    handler.send_response(response.status_code)
                    for k, v in response.headers.items():
                        if k.lower() not in _HOP:
                            handler.send_header(k, v)
                    handler.send_header('Connection', 'close')
                    handler.end_headers()
                    digest, size = hashlib.sha256(), 0
                    for chunk in response.iter_raw():
                        size += len(chunk)
                        if size > 4 * 1024 ** 3:
                            raise ValueError('media response exceeds bound')
                        digest.update(chunk)
                        handler.wfile.write(chunk)
                    row.update(status=response.status_code, upstream_body_consumed_bytes=size,
                               delivered_body_bytes=size, upstream_body_sha256=digest.hexdigest(),
                               delivered_body_sha256=digest.hexdigest(), source_body_complete=True)
            handler.wfile.flush()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            row['error_type'] = type(exc).__name__
            if 'upstream_status' not in row:
                handler.send_error(502)
        finally:
            if response is not None:
                response.close()
            handler.close_connection = True
            with self._lock:
                self.owner.network.append(row)
