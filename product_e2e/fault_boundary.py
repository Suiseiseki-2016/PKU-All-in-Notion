"""Runner-owned loopback boundary forwarding to the real account service.

Only fault runs use this boundary. No business function is replaced, no
credential is logged, and an injection that did not touch a real request fails.
The independent oracle never uses this proxy.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import errno
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx

from .core import ResourceBlocked

CATALOG_FAULTS = {"zero-null", "missing-revision", "bool-revision", "string-revision",
    "negative-revision", "zero-nonempty", "positive-null", "partial-snapshot", "wrong-course",
    "login-html", "profile-ok-catalog-fails", "legacy-extra-course"}
STATUS_FAULTS = {"403": 403, "409": 409, "429": 429, "5xx": 503, "low-balance": 402}
SUPPORTED = CATALOG_FAULTS | set(STATUS_FAULTS) | {"offline", "dns", "proxy", "timeout",
    "malformed-json", "expired-session", "wrong-password", "unverified", "lost-enqueue-response",
    "permit-acquire-lost-response", "permit-release-lost-response",
    "observation-lost-response"}


class AccountFaultBoundary:
    def __init__(self, owner):
        self.owner = owner
        self.variant = None
        self.receipt = None
        self._held_profile = None
        self._inflight = []
        self._lock = threading.Lock()
        self._clients = {}
        self._connections = ExitStack()
        boundary = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                boundary.handle(self)

            do_POST = do_PUT = do_DELETE = do_GET

        port = getattr(owner, 'config', {}).get('owned_fault_boundary_port', 0)
        if type(port) is not int or (port != 0 and not 1024 <= port <= 65535):
            raise ResourceBlocked('invalid_owned_fault_boundary_port')
        # Resume a journal only at its original owned endpoint identity. bind
        # fails if that port now belongs to another process; never replace it.
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
        try:
            if port and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.server.allow_reuse_address = False
                self.server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.server.server_bind()
            self.server.server_activate()
        except BaseException:
            self.server.server_close()
            raise
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        if self._held_profile is not None:
            self._held_profile['resume'].set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._connections.close()

    def _upstream(self, headers):
        # Reuse only real HTTPS transport, never service responses. Keep
        # separate credential pools; every request still reaches the service
        # and all original injection predicates run on its actual response.
        key = hashlib.sha256(next((value for name, value in headers.items()
            if name.lower() == 'authorization'), '').encode()).hexdigest()
        with self._lock:
            if key not in self._clients:
                self._clients[key] = self._connections.enter_context(httpx.Client(
                    timeout=30, trust_env=False, follow_redirects=False))
            return self._clients[key]

    @contextmanager
    def hold_owned_profile_read(self, ready):
        """Hold one successful real GET while the driver crashes its own app.

        The predicate observes owned durable receipts only. No profile body or
        business response is fabricated, and no write request is intercepted.
        """
        if not callable(ready):
            raise ValueError('Owned receipt observation predicate required')
        receipt = {'boundary': 'real-account-profile-read-before-owned-native-crash',
                   'observed': False, 'request_count': 0}
        held = {'ready': ready, 'observed': threading.Event(),
                'resume': threading.Event(), 'receipt': receipt}
        with self._lock:
            if self.variant is not None or self._held_profile is not None:
                raise ResourceBlocked('overlapping_fault_boundaries_forbidden')
            self._held_profile = held
        try:
            yield held
        finally:
            held['resume'].set()
            with self._lock:
                self._held_profile = None
            self.owner.actions.append({'held_real_profile_read': dict(receipt)})
            if not receipt['observed']:
                raise ResourceBlocked('owned_native_crash_read_boundary_not_exercised')

    @contextmanager
    def inject(self, variant):
        if variant not in SUPPORTED:
            raise ResourceBlocked("software_capability_missing_fault_" + variant)
        with self._lock:
            if self.variant is not None or self._held_profile is not None:
                raise ResourceBlocked("overlapping_fault_boundaries_forbidden")
            receipt = {"variant": variant, "boundary": "real-account-http-boundary",
                       "observed": False, "request_count": 0}
            self.variant, self.receipt = variant, receipt
        try:
            yield receipt
        finally:
            with self._lock:
                self.variant = self.receipt = None
                pending = [event for own, event in self._inflight if own is receipt]
            # A real product timeout can finish before our deliberately held
            # request does. Retain its own receipt until that request ends;
            # never misattribute its completion to the next fault.
            deadline = time.monotonic() + 30
            for event in pending:
                if not event.wait(max(0, deadline - time.monotonic())):
                    raise ResourceBlocked('fault_request_did_not_finish_' + variant)
            self.owner.actions.append({"fault_receipt": dict(receipt)})
            if not receipt["observed"]:
                raise ResourceBlocked("fault_boundary_not_exercised_" + variant)

    def _targeted(self, path, method, variant):
        if variant in {'offline', 'dns', 'proxy', 'timeout'}:
            course = getattr(self.owner, 'config', {}).get('network_fault_course_id')
            if course is not None:
                if not isinstance(course, str) or not re.fullmatch(r'_[0-9]+_[0-9]+', course):
                    raise ResourceBlocked('invalid_network_fault_course_id')
                return method == 'GET' and path == '/v1/catalog/' + course
        if variant in CATALOG_FAULTS:
            explicit = getattr(self.owner, 'config', {}).get('catalog_fault_course_id')
            if explicit is not None:
                if not isinstance(explicit, str) or not re.fullmatch(r'_[0-9]+_[0-9]+', explicit):
                    raise ResourceBlocked('invalid_catalog_fault_course_id')
                return method == 'GET' and path == '/v1/catalog/' + explicit
            # Observations and write permits are different protocols. Corrupt
            # only published envelopes, never claim an invalid observation ID
            # or permit response as a snapshot-validation fault.
            if variant in {"login-html", "profile-ok-catalog-fails"} and path == "/v1/catalog":
                return method == "GET"
            if method != "GET" or not re.fullmatch(r"/v1/catalog/_[0-9]+_[0-9]+", path):
                return False
            catalog = getattr(self.owner, 'config', {}).get('catalog', {})
            if variant in {'zero-null', 'legacy-extra-course'}:
                targets = catalog.get('legacy_course_ids', [])
                if targets:
                    return path.rsplit('/', 1)[-1] == targets[0]
            else:
                course_id = catalog.get('existing_course_link', {}).get('course_id')
                if course_id:
                    return path.rsplit('/', 1)[-1] == course_id
            return True
        if variant == "expired-session":
            return method == 'GET' and path in {"/v1/auth/me", "/v1/profile", "/v1/quota"}
        if variant == "wrong-password":
            return method == 'POST' and path == '/v1/auth/login'
        if variant == "unverified":
            return (method, path) in {('POST', '/v1/auth/login'), ('GET', '/v1/auth/me')}
        if variant == "lost-enqueue-response":
            return method == "POST" and path == "/v1/llm/jobs"
        if variant == "observation-lost-response":
            # Focused transport diagnostic, separate from mandatory permit
            # recovery. It may create an expiring observation ticket only.
            course = getattr(self.owner, 'config', {}).get('observation_fault_course_id')
            if not isinstance(course, str) or not re.fullmatch(r'_[0-9]+_[0-9]+', course):
                raise ResourceBlocked('invalid_observation_fault_course_id')
            return method == 'POST' and path == '/v1/catalog/' + course + '/observations'
        if variant.startswith("permit-"):
            suffix = "/projection/release" if "release" in variant else "/projection"
            course = getattr(self.owner, 'config', {}).get('projection_fault_course_id')
            if course is not None:
                if not isinstance(course, str) or not re.fullmatch(r'_[0-9]+_[0-9]+', course):
                    raise ResourceBlocked('invalid_projection_fault_course_id')
                return method == 'POST' and path == '/v1/catalog/' + course + suffix
            return method == "POST" and path.startswith("/v1/catalog/") and path.endswith(suffix)
        return path.startswith("/v1/") and path != "/v1/auth/logout"

    @staticmethod
    def _error_chain(exc):
        rows, seen = [], set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            rows.append({'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
                         'winerror': getattr(exc, 'winerror', None)})
            exc = exc.__cause__ or exc.__context__
        return rows

    def _network_failure(self, variant, method, path, body, headers):
        """Fail the delegated native request, retaining only safe causal data."""
        started = time.monotonic()
        proof = {'variant': variant, 'method': method, 'path': urlsplit(path).path,
            'request_body_sha256': hashlib.sha256(body or b'').hexdigest(),
            'failure_observed': False, 'error_chain': []}
        if variant == 'timeout':
            time.sleep(25)  # strictly exceeds the installed product's 20s timeout
            proof.update(failure_observed=True, mechanism='held-original-native-request')
        elif variant == 'offline':
            proof.update(failure_observed=True, mechanism='disconnect-original-owned-native-connection')
        elif variant == 'dns':
            from .dns_fault import OwnedDNS, DNSHTTPTransport
            with OwnedDNS() as resolver:
                transport = DNSHTTPTransport(resolver)
                with httpx.Client(timeout=10, trust_env=False, transport=transport) as client:
                    try:
                        client.request(method, 'https://pku.aeoluswu.info' + path,
                                       content=body, headers=headers)
                    except httpx.TransportError as exc:
                        proof['error_chain'] = self._error_chain(exc)
                        proof['failure_observed'] = bool(resolver.queries) and transport.requests == [{
                            'method': method, 'host': 'pku.aeoluswu.info', 'path': urlsplit(path).path,
                            'body_sha256': proof['request_body_sha256']}] and any(
                                row['type'] == 'gaierror' for row in proof['error_chain'])
                proof['dns_queries'] = list(resolver.queries)
                proof['delegated_requests'] = list(transport.requests)
            if not proof['failure_observed']:
                raise ResourceBlocked('dns_failure_not_observed')
            proof['mechanism'] = 'original-request-through-owned-nxdomain-resolver'
        elif variant == 'proxy':
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
                # This owned bound socket does not listen and cannot belong
                # to another process. Preserve original method/path/payload.
                with httpx.Client(proxy=f'http://127.0.0.1:{port}', timeout=3, trust_env=False) as client:
                    try:
                        client.request(method, 'https://pku.aeoluswu.info' + path,
                                       content=body, headers=headers)
                    except httpx.TransportError as exc:
                        proof['error_chain'] = self._error_chain(exc)
                        proof['failure_observed'] = any(row['errno'] in {10061, 111, errno.ECONNREFUSED}
                            or row['winerror'] == 10061 for row in proof['error_chain'])
            if not proof['failure_observed']:
                raise ResourceBlocked('proxy_failure_not_observed')
            proof['mechanism'] = 'original-request-through-owned-nonlistening-proxy'
        else:
            raise ValueError('Unknown transport fault')
        proof['elapsed_seconds'] = time.monotonic() - started
        return proof

    @staticmethod
    def corrupt(value, variant):
        """Transform only a real external response; strict product checks stay intact."""
        value = json.loads(json.dumps(value))
        if variant in {"zero-null", "legacy-extra-course"}:
            return {"revision": 0, "snapshot": None}
        if variant == "missing-revision":
            value.pop("revision", None)
        elif variant == "bool-revision":
            value["revision"] = True
        elif variant == "string-revision":
            value["revision"] = str(value.get("revision", 1))
        elif variant == "negative-revision":
            value["revision"] = -1
        elif variant == "zero-nonempty":
            value.update(revision=0, snapshot=value.get("snapshot") or {})
        elif variant == "positive-null":
            value.update(revision=1, snapshot=None)
        elif variant == "partial-snapshot":
            if isinstance(value.get("snapshot"), dict):
                value["snapshot"].pop("sections", None)
            else:
                raise ValueError("partial snapshot fault requires real published snapshot")
        elif variant == "wrong-course":
            if isinstance(value.get("snapshot"), dict):
                value["snapshot"]["course_id"] = "_999999999_1"
            else:
                raise ValueError("wrong course fault requires real published snapshot")
        return value

    def handle(self, handler):
        started = time.monotonic()
        parsed = urlsplit(handler.path)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith(("/v1/", "/healthz")):
            handler.send_error(400)
            return
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 0 or length > 8 * 1024 * 1024:
            handler.send_error(413)
            return
        body = handler.rfile.read(length) if length else None
        headers = {key: value for key, value in handler.headers.items()
                   if key.lower() not in {"host", "connection", "content-length", "proxy-authorization"}}
        with self._lock:
            variant, receipt = self.variant, self.receipt
            targeted = variant is not None and self._targeted(parsed.path, handler.command, variant)
            completed = threading.Event() if targeted else None
            if completed is not None:
                self._inflight.append((receipt, completed))
        status, content_type, upstream_hash, upstream_status = None, "application/json", None, None
        transport_evidence = None
        try:
            if targeted and variant in {"offline", "dns", "proxy", "timeout"}:
                transport_evidence = self._network_failure(variant, handler.command, handler.path, body, headers)
                try:
                    handler.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # The real product may have already closed its timed-out
                    # request. The actual held-request evidence is retained.
                    pass
                handler.connection.close()
                status = "transport-disconnected"
                return
            # Live upstream, including real side effects for lost-response cases.
            client = self._upstream(headers)
            response = client.request(handler.command, "https://pku.aeoluswu.info" + handler.path,
                                      content=body, headers=headers)
            data, status = response.content, response.status_code
            upstream_status = response.status_code
            content_type = response.headers.get("content-type", "application/json")
            upstream_hash = hashlib.sha256(data).hexdigest()
            with self._lock:
                held = self._held_profile
            if (held is not None and handler.command == 'GET' and parsed.path == '/v1/profile'
                    and status == 200 and not held['observed'].is_set() and held['ready']()):
                held['receipt'].update(observed=True, request_count=1,
                    upstream_status=status, upstream_body_sha256=upstream_hash,
                    method='GET', path=parsed.path)
                held['observed'].set()
                # Less than the product's real timeout. A missed crash remains
                # a failed probe, never an indefinitely suspended service call.
                held['resume'].wait(timeout=15)
            if targeted:
                if variant == 'wrong-password':
                    # The stimulus is the deliberately incorrect native form
                    # input. Observe the service's actual rejection, without
                    # manufacturing a failed login from a successful response.
                    targeted = status == 401
                elif variant == 'unverified':
                    # Observe an actual unverified identity, without altering
                    # verification flags, issuing a token, or constructing a
                    # successful login. Registration itself is not a fault.
                    try:
                        identity = response.json() if status == 200 else None
                    except ValueError:
                        identity = None
                    targeted = (isinstance(identity, dict)
                        and identity.get('email_verified') is False
                        and isinstance(identity.get('email'), str)
                        and bool(identity['email'].strip())
                        and (handler.command == 'GET' or (
                            isinstance(identity.get('platform_token'), str)
                            and bool(identity['platform_token'].strip()))))
                elif variant.startswith("permit-") or variant in {"lost-enqueue-response", "observation-lost-response"}:
                    if status not in {200, 201, 202}:
                        targeted = False  # refused writes are not lost successful responses
                    else:
                        if variant == 'observation-lost-response':
                            value = response.json()
                            if (type(value.get('revision')) is not int or value['revision'] < 0
                                    or not isinstance(value.get('observation_id'), str)
                                    or not re.fullmatch(r'[0-9a-f]{64}', value['observation_id'])):
                                raise ResourceBlocked('invalid_real_observation_response')
                            # Keep original bytes before disconnecting. No
                            # response is constructed or substituted here.
                            evidence = self.owner.case_dir / ('oracle-native-observation-response-'
                                + value['observation_id'] + '.json')
                            evidence.write_bytes(data)
                            transport_evidence = {'mechanism': 'successful-real-response-connection-closed',
                                'response_evidence': evidence.relative_to(self.owner.context['run_dir']).as_posix(),
                                'response_sha256': upstream_hash,
                                'request_body_sha256': hashlib.sha256(body or b'').hexdigest(),
                                'revision': value['revision']}
                        handler.connection.shutdown(socket.SHUT_RDWR)
                        handler.connection.close()
                        return
                elif variant == 'expired-session':
                    targeted = status == 200
                    if targeted:
                        status = 401
                        data = b'{"detail":"E2E external boundary expired-session"}'
                elif variant in STATUS_FAULTS:
                    status = STATUS_FAULTS.get(variant, 401)
                    data = json.dumps({"detail": "E2E external boundary " + variant}).encode()
                elif variant == "profile-ok-catalog-fails":
                    status, data = 503, b'{"detail":"catalog temporarily unavailable"}'
                elif variant == "login-html":
                    content_type, data = "text/html", b'<form action="login"><input type="password"></form>'
                elif variant == "malformed-json":
                    data = b'{"revision":'
                elif status == 200 and isinstance(response.json(), dict):
                    data = json.dumps(self.corrupt(response.json(), variant)).encode()
                else:
                    targeted = False
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (httpx.HTTPError, OSError, ValueError, ResourceBlocked):
            # A proxy/upstream failure is never claimed as the requested mutation.
            targeted = False
            try:
                handler.send_error(502)
            except OSError:
                pass
        finally:
            self.owner.network.append({"host": "pku.aeoluswu.info", "path": parsed.path,
                "method": handler.command, "boundary": "account", "status": status,
                "upstream_body_sha256": upstream_hash, "upstream_status": upstream_status,
                "authorization_sha256": hashlib.sha256(next((value for key, value in headers.items()
                    if key.lower() == 'authorization'), '').encode()).hexdigest(),
                "request_body_sha256": hashlib.sha256(body or b'').hexdigest(),
                "fault": variant if targeted else None,
                "started_monotonic": started,
                "elapsed_seconds": time.monotonic() - started,
                "transport_evidence": transport_evidence})
            if targeted and receipt is not None:
                with self._lock:
                    receipt["observed"] = True
                    receipt["request_count"] += 1
            if completed is not None:
                with self._lock:
                    self._inflight.remove((receipt, completed))
                completed.set()
