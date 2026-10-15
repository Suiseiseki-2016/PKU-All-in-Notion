"""Real loopback NXDOMAIN for one delegated request; no system DNS changes."""
import hashlib
import secrets
import socket
import struct
import threading

import httpcore
import httpx

from .core import ResourceBlocked


HOST = 'pku.aeoluswu.info'


class OwnedDNS:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.2)
        self.address = self.socket.getsockname()
        self.stop = threading.Event()
        self.queries = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.socket.close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise ResourceBlocked('owned_dns_server_cleanup_failed')

    @staticmethod
    def question(host):
        if host != HOST:
            raise ResourceBlocked('dns_fault_non_account_host_forbidden')
        return b''.join(bytes([len(label)]) + label.encode('ascii') for label in host.split('.')) + b'\0\0\1\0\1'

    def _serve(self):
        question = self.question(HOST)
        while not self.stop.is_set():
            try:
                raw, peer = self.socket.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(raw) != 12 + len(question) or raw[12:] != question:
                continue
            identity, flags, qd, an, ns, ar = struct.unpack('!6H', raw[:12])
            if flags != 0x0100 or (qd, an, ns, ar) != (1, 0, 0, 0):
                continue
            response = struct.pack('!6H', identity, 0x8103, 1, 0, 0, 0) + question
            self.socket.sendto(response, peer)

    def fail_resolution(self, host):
        question = self.question(host)
        identity = secrets.randbelow(65536)
        raw = struct.pack('!6H', identity, 0x0100, 1, 0, 0, 0) + question
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.sendto(raw, self.address)
            response, peer = client.recvfrom(1024)
        if (peer != self.address or len(response) != len(raw) or response[12:] != question
                or struct.unpack('!6H', response[:12]) != (identity, 0x8103, 1, 0, 0, 0)):
            raise ResourceBlocked('owned_dns_nxdomain_response_invalid')
        self.queries.append({'hostname': host, 'rcode': 3, 'answers': 0,
            'request_sha256': hashlib.sha256(raw).hexdigest(),
            'response_sha256': hashlib.sha256(response).hexdigest(),
            'resolver': 'owned-loopback-udp'})
        raise socket.gaierror(socket.EAI_NONAME, 'Owned DNS returned NXDOMAIN')


class DNSBackend(httpcore.SyncBackend):
    def __init__(self, resolver):
        self.resolver = resolver

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode('ascii')
        if port != 443:
            raise ResourceBlocked('dns_fault_non_https_port_forbidden')
        try:
            self.resolver.fail_resolution(host)
        except socket.gaierror as exc:
            raise httpcore.ConnectError('Owned DNS NXDOMAIN') from exc
        raise ResourceBlocked('dns_fault_resolution_did_not_fail')


class DNSHTTPTransport(httpx.HTTPTransport):
    def __init__(self, resolver):
        self.requests = []
        super().__init__(trust_env=False, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(network_backend=DNSBackend(resolver),
                                              max_connections=1, retries=0)

    def handle_request(self, request):
        self.requests.append({'method': request.method, 'host': request.url.host,
            'path': request.url.path, 'body_sha256': hashlib.sha256(request.read()).hexdigest()})
        return super().handle_request(request)
