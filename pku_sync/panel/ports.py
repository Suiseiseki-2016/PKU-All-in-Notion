"""Panel port policy: first available of 8791/8792/8793, loopback only.

The student panel binds ``127.0.0.1`` on the first approved port that is
genuinely free. A port already owned by another process is never killed or
contacted — the bind simply fails and the next approved port is tried. An
arbitrary port is never selected, and when all three approved ports are
occupied the launcher fails with the pinned actionable copy below.

The returned socket stays open and is handed to uvicorn as a pre-bound socket,
so the port that was probed is the port that serves the panel (no check-then-
rebind race, and on Windows exclusive binding makes our listener an honest,
unstealable occupant).
"""

from __future__ import annotations

import socket

# The only loopback ports the student panel may ever bind, in fallback order.
# Occupants are never killed; a busy port just moves us to the next approved
# one. The caller's preferred port starts the chain, then the rest follow in
# the approved order.
PANEL_PORTS = (8791, 8792, 8793)

# Pinned, actionable copy when ALL three approved panel ports are occupied.
# A named constant (not an inline literal) so the launcher and its test share
# the exact string: it names the three approved ports and the recovery action.
PANEL_PORTS_BUSY_MESSAGE = "本地面板端口 8791、8792、8793 都被占用：关闭占用端口的应用后重试。"


class PanelPortError(RuntimeError):
    """The panel cannot bind any approved loopback port."""


def _open_socket(port: int) -> socket.socket:
    """Bind ``127.0.0.1:port`` exclusively; OSError when the port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_EXCLUSIVEADDRUSE exists only on Windows sockets, so the capability
    # check alone keeps this branch cross-platform without naming an OS
    # (the constant is simply absent on other systems). On Windows,
    # SO_REUSEADDR would let a second bind "hijack" an occupied port and
    # defeat the fallback; exclusive use keeps this probe honest.
    # (SO_EXCLUSIVEADDRUSE and SO_REUSEADDR are mutually exclusive inside
    # one socket, so the exclusive flag is the only option set there — same
    # as the OAuth callback listener.)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # Without the exclusive-use capability, the standard SO_REUSEADDR
        # semantics apply: a quick panel restart re-binds its preferred
        # approved port despite TIME_WAIT remnants (the same default uvicorn
        # uses for its own binds), while a LIVE listener still refuses the
        # bind — two active listeners need SO_REUSEPORT — so the approved
        # fallback and the never-hijack guarantee both stay honest.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(socket.SOMAXCONN)
    return sock


def panel_port_candidates(preferred: int) -> tuple[int, ...]:
    """The fallback chain: ``preferred`` first, then the rest in approved order."""
    return (preferred,) + tuple(p for p in PANEL_PORTS if p != preferred)


def bind_panel(preferred: int = PANEL_PORTS[0]) -> tuple[socket.socket, int]:
    """Bind 127.0.0.1 to the first available approved panel port.

    ``preferred`` selects where the approved chain starts and must be one of
    ``PANEL_PORTS``. Returns ``(bound_socket, chosen_port)``; raises
    :class:`PanelPortError` with the pinned copy when every approved port is
    occupied, or a clear message when ``preferred`` is not an approved port.
    The returned socket is pre-bound and listening, ready to hand to uvicorn.
    """
    if preferred not in PANEL_PORTS:
        raise PanelPortError(
            f"面板端口仅支持 {PANEL_PORTS[0]}、{PANEL_PORTS[1]}、{PANEL_PORTS[2]}"
            f"（不支持 {preferred}）：请使用默认端口重试。"
        )
    last_error: OSError | None = None
    for candidate in panel_port_candidates(preferred):
        try:
            return _open_socket(candidate), candidate
        except OSError as exc:  # occupied: try the next approved port
            last_error = exc
    raise PanelPortError(PANEL_PORTS_BUSY_MESSAGE) from last_error
