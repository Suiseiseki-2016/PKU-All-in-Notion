"""Safeguards for the localhost proxies a desktop shell leaves in the env.

dead_loopback_proxy keeps a proxy that has already exited from breaking
trust_env clients; campus_identity_proxy resolves the desktop app's
explicit IAAA proxy override (default: the machine's own networking).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit


def dead_loopback_proxy() -> bool:
    """Allow direct HTTPS when a configured local proxy has already exited."""
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
    if not proxy:
        return False
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=0.2):
            return False
    except OSError:
        return True


def campus_identity_proxy() -> str | None:
    """The desktop app's explicit IAAA proxy override, or no proxy at all.

    IAAA is reached over the machine's own networking by default: campus
    hosts are normally excluded from any system proxy, and forcing the
    shell's HTTP(S)_PROXY onto the login breaks its TLS handshake. Only
    PKU_CAMPUS_IDENTITY_PROXY is honored ("direct" forces no proxy), so an
    unrelated desktop proxy is never silently inherited.
    """
    candidate = os.environ.get("PKU_CAMPUS_IDENTITY_PROXY", "")
    if not candidate or candidate.casefold() == "direct":
        return None
    url = candidate if "://" in candidate else f"http://{candidate}"
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        try:
            with socket.create_connection((parts.hostname, parts.port or 80), timeout=0.2):
                pass
        except OSError:
            return None
    return url
