"""Relay dev port policy: first available of 8800/8801/8802, loopback only.

The native relay dev launcher binds ``127.0.0.1`` on the first approved port
that is genuinely free. A port already owned by another process is never
killed or contacted — the bind simply fails and the next approved port is
tried. An arbitrary port is never selected, and when all three approved
ports are occupied the launcher fails with the pinned actionable copy below
(VAL-CROSS-021; mirrors the approved student-panel policy in the client
repo, ``pku_sync/panel/ports.py``).

The returned socket stays open and is handed to uvicorn as a pre-bound
socket, so the port that was probed is the port that serves the relay (no
check-then-rebind race, and on Windows exclusive binding makes our listener
an honest, unstealable occupant).
"""

from __future__ import annotations

import socket
import sys

# The only loopback ports the relay dev service may ever bind, in fallback
# order. Occupants are never killed; a busy port just moves us to the next
# approved one. The caller's preferred port starts the chain, then the rest
# follow in the approved order.
RELAY_DEV_PORTS = (8800, 8801, 8802)

# Pinned, actionable copy when ALL three approved relay dev ports are
# occupied. A named constant (not an inline literal) so the launcher and its
# test share the exact string: it names the three approved ports and the
# recovery action.
RELAY_DEV_PORTS_BUSY_MESSAGE = "本地中转端口 8800、8801、8802 都被占用：关闭占用端口的应用后重试。"


class RelayDevPortError(RuntimeError):
    """The relay dev service cannot bind any approved loopback port."""


def _open_socket(port: int) -> socket.socket:
    """Bind ``127.0.0.1:port`` exclusively; OSError when the port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows SO_REUSEADDR would let a second bind "hijack" an occupied
        # port and defeat the fallback; exclusive use keeps this probe honest
        # (mutually exclusive with SO_REUSEADDR on one socket — never both;
        # verified WinError 10022, see library/environment.md).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(socket.SOMAXCONN)
    return sock


def relay_dev_port_candidates(preferred: int) -> tuple[int, ...]:
    """The fallback chain: ``preferred`` first, then the rest in approved order."""
    return (preferred,) + tuple(p for p in RELAY_DEV_PORTS if p != preferred)


def bind_relay_dev(preferred: int = RELAY_DEV_PORTS[0]) -> tuple[socket.socket, int]:
    """Bind 127.0.0.1 to the first available approved relay dev port.

    ``preferred`` selects where the approved chain starts and must be one of
    ``RELAY_DEV_PORTS``. Returns ``(bound_socket, chosen_port)``; raises
    :class:`RelayDevPortError` with the pinned copy when every approved port
    is occupied, or a clear message when ``preferred`` is not an approved
    port. The returned socket is pre-bound and listening, ready to hand to
    uvicorn.
    """
    if preferred not in RELAY_DEV_PORTS:
        raise RelayDevPortError(
            f"中转端口仅支持 {RELAY_DEV_PORTS[0]}、{RELAY_DEV_PORTS[1]}、{RELAY_DEV_PORTS[2]}"
            f"（不支持 {preferred}）：请使用默认端口重试。"
        )
    last_error: OSError | None = None
    for candidate in relay_dev_port_candidates(preferred):
        try:
            return _open_socket(candidate), candidate
        except OSError as exc:  # occupied: try the next approved port
            last_error = exc
    raise RelayDevPortError(RELAY_DEV_PORTS_BUSY_MESSAGE) from last_error
