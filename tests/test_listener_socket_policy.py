"""Cross-platform socket-option policy for the loopback listeners.

Recorded red (CI, 2026-09-23, runs 35807825732 / 35808171061): on the
posix runners the port-occupancy suite failed because the panel's bound
socket set no ``SO_REUSEADDR``, so TIME_WAIT remnants from earlier tests in
the same session made ``bind_panel`` fall past its preferred approved port
(``Errno 98`` / ``Errno 48`` at the occupant binds, and wrong-fallback
assertions). Windows cannot reproduce that red (a Windows bind ignores
TIME_WAIT remnants by default), so this file pins the socket-option policy
deterministically on every platform via capability detection:

- Windows (``SO_EXCLUSIVEADDRUSE`` exists): the listener binds EXCLUSIVELY
  and must not set ``SO_REUSEADDR`` (which would let a second bind hijack an
  occupied port and defeat the approved-port fallback).
- Other platforms: the listener sets ``SO_REUSEADDR`` — the standard
  TIME_WAIT-only reuse that keeps a quick restart on its preferred approved
  port, exactly like uvicorn's own binds. A LIVE listener still blocks the
  bind (two active binds need ``SO_REUSEPORT``), so fallback honesty and the
  genuine-occupant guarantee are preserved.

The OAuth callback listener keeps the complementary half of the policy:
exclusive on Windows, ``http.server``'s default ``allow_reuse_address``
(TIME_WAIT-only reuse) elsewhere. Both halves are pinned here.
"""

from __future__ import annotations

import socket

from pku_sync.notion_login import _CallbackServer
from pku_sync.panel.ports import bind_panel

HAS_EXCLUSIVE = hasattr(socket, "SO_EXCLUSIVEADDRUSE")


def test_panel_listener_socket_option_policy():
    bound, port = bind_panel()
    try:
        assert bound.getsockname()[0] == "127.0.0.1"  # loopback only
        if HAS_EXCLUSIVE:
            assert bound.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        else:
            assert bound.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        bound.close()


def test_callback_listener_socket_option_policy():
    server = _CallbackServer(0, "state-pin")
    try:
        assert server.server_address[0] == "127.0.0.1"  # loopback only
        if HAS_EXCLUSIVE:
            assert server.socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR
            ) == 0
        else:
            assert server.socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR
            ) == 1
    finally:
        server.server_close()
