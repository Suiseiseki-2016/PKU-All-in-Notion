"""Panel port policy (VAL-SECU-024): first available of 8791/8792/8793.

The panel binds 127.0.0.1 on the first approved port that is genuinely free,
surfaces the selected port in startup output (the CLI/manual curl leg covers
the running server + /healthz), never kills or contacts an occupant, and when
all three approved ports are occupied fails with the pinned actionable copy
(a named constant in code and test, naming the three ports plus a recovery
action) — an arbitrary port is never selected.

Every occupancy fixture is a validator-owned loopback listener with a liveness
proof, mirroring the 8765/8766 callback-fallback tests.
"""

from __future__ import annotations

import socket
import sys

import pytest

from pku_sync.panel.ports import (
    PANEL_PORTS,
    PANEL_PORTS_BUSY_MESSAGE,
    PanelPortError,
    bind_panel,
)


def require_free(port: int) -> None:
    """Skip honestly when a foreign process already holds an approved port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        pytest.skip(f"port {port} is already occupied by a foreign process")
    finally:
        probe.close()


def occupy_port(port: int) -> socket.socket:
    """A validator-owned loopback listener: a REAL occupant for the fallback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows SO_REUSEADDR would let a second bind "hijack" the port;
        # exclusive use makes this dummy a genuine, unstealable occupant.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    return sock


def assert_occupant_alive(sock: socket.socket, port: int) -> None:
    """Liveness proof: the dummy still owns and serves its port."""
    probe = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = sock.accept()
    conn.close()
    probe.close()


def test_panel_ports_are_exactly_the_approved_triple():
    assert PANEL_PORTS == (8791, 8792, 8793)


def test_bind_panel_takes_first_available_8791_when_free():
    require_free(8791)
    bound, port = bind_panel()
    try:
        assert port == 8791
        assert bound.getsockname()[0] == "127.0.0.1"  # loopback only
        assert bound.getsockname()[1] == 8791
    finally:
        bound.close()


def test_bind_panel_falls_back_to_8792_when_8791_occupied():
    require_free(8792)
    dummy = occupy_port(8791)
    try:
        bound, port = bind_panel()
        try:
            assert port == 8792
            assert bound.getsockname()[0] == "127.0.0.1"  # loopback only
            assert bound.getsockname()[1] == 8792
        finally:
            bound.close()
        # the occupant survives at 8791: never killed, never contacted
        assert_occupant_alive(dummy, 8791)
    finally:
        dummy.close()


def test_bind_panel_falls_back_to_8793_when_first_two_occupied():
    require_free(8793)
    first = occupy_port(8791)
    second = occupy_port(8792)
    try:
        bound, port = bind_panel()
        try:
            assert port == 8793
            assert bound.getsockname()[0] == "127.0.0.1"
        finally:
            bound.close()
        assert_occupant_alive(first, 8791)
        assert_occupant_alive(second, 8792)
    finally:
        first.close()
        second.close()


def test_all_three_ports_occupied_raises_pinned_copy():
    dummy1 = occupy_port(8791)
    dummy2 = occupy_port(8792)
    dummy3 = occupy_port(8793)
    try:
        with pytest.raises(PanelPortError) as excinfo:
            bind_panel()
        # the exact pinned constant, named in code and imported here by name
        assert str(excinfo.value) == PANEL_PORTS_BUSY_MESSAGE
        assert "8791" in PANEL_PORTS_BUSY_MESSAGE
        assert "8792" in PANEL_PORTS_BUSY_MESSAGE
        assert "8793" in PANEL_PORTS_BUSY_MESSAGE
        assert "关闭占用端口的应用后重试" in PANEL_PORTS_BUSY_MESSAGE
        # no port was selected and no occupant was touched
        for dummy, port in ((dummy1, 8791), (dummy2, 8792), (dummy3, 8793)):
            assert_occupant_alive(dummy, port)
    finally:
        dummy1.close()
        dummy2.close()
        dummy3.close()


def test_never_selects_an_arbitrary_port():
    for bad_port in (0, 8787, 9999):
        with pytest.raises(PanelPortError) as excinfo:
            bind_panel(preferred=bad_port)
        message = str(excinfo.value)
        assert "8791" in message
        assert "8792" in message
        assert "8793" in message
        assert str(bad_port) in message


def test_preferred_port_starts_the_approved_chain():
    require_free(8791)
    require_free(8793)
    dummy = occupy_port(8792)
    try:
        bound, port = bind_panel(preferred=8792)
        try:
            # 8792 occupied: the rest of the approved list follows in order
            assert port == 8791
            assert bound.getsockname()[0] == "127.0.0.1"
        finally:
            bound.close()
        assert_occupant_alive(dummy, 8792)
    finally:
        dummy.close()
