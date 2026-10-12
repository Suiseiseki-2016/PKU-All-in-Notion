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


def campus_media_proxy() -> str | None:
    """Optional explicit route for resources/player metadata, never for IAAA.

    Keep the normal direct route unless the user supplied a valid live proxy.
    This is a network route; it cannot change a job result or its acceptance.
    """
    candidate = os.environ.get("PKU_CAMPUS_MEDIA_PROXY", "")
    if not candidate or candidate.casefold() == "direct":
        return None
    url = candidate if "://" in candidate else "http://" + candidate
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or not port
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        return None
    if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        try:
            with socket.create_connection((parts.hostname, port), timeout=0.2):
                pass
        except OSError:
            return None
    return url


def campus_identity_fallback_proxy() -> str | None:
    """A live Windows user proxy, only after direct IAAA transport fails.

    Direct remains the first route, including hosts in the Windows bypass list.
    This recovery uses only the user's enabled local proxy after that route
    fails. Explicit direct/proxy choices are preserved. Never inherit shell
    proxies or route course/media traffic through this fallback.
    """
    if os.name != "nt" or os.environ.get("PKU_CAMPUS_IDENTITY_PROXY", ""):
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except OSError:
        return None
    if type(enabled) is not int or enabled != 1 or not isinstance(server, str):
        return None
    protocols = dict(part.split("=", 1) for part in server.split(";") if "=" in part)
    candidate = protocols.get("https", "") if protocols else server
    if not candidate:
        return None
    url = candidate if "://" in candidate else "http://" + candidate
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not port or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        return None
    try:
        with socket.create_connection((parts.hostname, port), timeout=0.2):
            pass
    except OSError:
        return None
    return url
