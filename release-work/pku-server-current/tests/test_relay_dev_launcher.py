"""Relay dev launcher port policy (VAL-CROSS-021): first available of
8800/8801/8802.

The relay dev launcher binds ``127.0.0.1`` on the first approved port that is
genuinely free, surfaces the selected port in its startup output (the live
leg covers the running server + /healthz), never kills or contacts an
occupant, and when all three approved ports are occupied fails with the
pinned actionable copy (a named constant in code and test, naming the three
ports plus a recovery action) — an arbitrary port is never selected.

Every occupancy fixture is a validator-owned loopback listener with a
liveness proof, mirroring the approved client panel port tests
(``tests/test_panel_ports.py`` in the client repo).
"""

from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

import pytest

from server.dev_launcher import (
    RELAY_DEV_HOST,
    resolve_db_path_env,
    scratch_db_path,
    startup_line,
)
from server.dev_ports import (
    RELAY_DEV_PORTS,
    RELAY_DEV_PORTS_BUSY_MESSAGE,
    RelayDevPortError,
    bind_relay_dev,
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
    probe.shutdown(socket.SHUT_WR)
    assert conn.recv(1) == b""
    conn.close()
    probe.close()


# --- the approved port triple and the 8800 → 8801 → 8802 fallback ---


def test_relay_dev_ports_are_exactly_the_approved_triple():
    assert RELAY_DEV_PORTS == (8800, 8801, 8802)


def test_bind_relay_dev_takes_first_available_8800_when_free():
    require_free(8800)
    bound, port = bind_relay_dev()
    try:
        assert port == 8800
        assert bound.getsockname()[0] == "127.0.0.1"  # loopback only
        assert bound.getsockname()[1] == 8800
    finally:
        bound.close()


def test_bind_relay_dev_falls_back_to_8801_when_8800_occupied():
    require_free(8801)
    dummy = occupy_port(8800)
    try:
        bound, port = bind_relay_dev()
        try:
            assert port == 8801
            assert bound.getsockname()[0] == "127.0.0.1"  # loopback only
            assert bound.getsockname()[1] == 8801
        finally:
            bound.close()
        # the occupant survives at 8800: never killed, never contacted
        assert_occupant_alive(dummy, 8800)
    finally:
        dummy.close()


def test_bind_relay_dev_falls_back_to_8802_when_first_two_occupied():
    require_free(8802)
    first = occupy_port(8800)
    second = occupy_port(8801)
    try:
        bound, port = bind_relay_dev()
        try:
            assert port == 8802
            assert bound.getsockname()[0] == "127.0.0.1"
        finally:
            bound.close()
        assert_occupant_alive(first, 8800)
        assert_occupant_alive(second, 8801)
    finally:
        first.close()
        second.close()


def test_all_three_ports_occupied_raises_pinned_copy():
    dummy1 = occupy_port(8800)
    dummy2 = occupy_port(8801)
    dummy3 = occupy_port(8802)
    try:
        with pytest.raises(RelayDevPortError) as excinfo:
            bind_relay_dev()
        # the exact pinned constant, named in code and imported here by name
        assert str(excinfo.value) == RELAY_DEV_PORTS_BUSY_MESSAGE
        assert "8800" in RELAY_DEV_PORTS_BUSY_MESSAGE
        assert "8801" in RELAY_DEV_PORTS_BUSY_MESSAGE
        assert "8802" in RELAY_DEV_PORTS_BUSY_MESSAGE
        assert "关闭占用端口的应用后重试" in RELAY_DEV_PORTS_BUSY_MESSAGE
        # no port was selected and no occupant was touched
        for dummy, port in ((dummy1, 8800), (dummy2, 8801), (dummy3, 8802)):
            assert_occupant_alive(dummy, port)
    finally:
        dummy1.close()
        dummy2.close()
        dummy3.close()


def test_never_selects_an_arbitrary_port():
    for bad_port in (0, 8787, 9999):
        with pytest.raises(RelayDevPortError) as excinfo:
            bind_relay_dev(preferred=bad_port)
        message = str(excinfo.value)
        assert "8800" in message
        assert "8801" in message
        assert "8802" in message
        assert str(bad_port) in message


def test_preferred_port_starts_the_approved_chain():
    require_free(8800)
    require_free(8802)
    dummy = occupy_port(8801)
    try:
        bound, port = bind_relay_dev(preferred=8801)
        try:
            # 8801 occupied: the rest of the approved list follows in order
            assert port == 8800
            assert bound.getsockname()[0] == "127.0.0.1"
        finally:
            bound.close()
        assert_occupant_alive(dummy, 8801)
    finally:
        dummy.close()


# --- the launcher entry: surfaced port + scratch dev DB policy ---


def test_startup_line_surfaces_the_chosen_port():
    assert RELAY_DEV_HOST == "127.0.0.1"  # loopback only, never 0.0.0.0
    for port in RELAY_DEV_PORTS:
        line = startup_line(port)
        assert str(port) in line
        assert line == f"中转服务已启动：http://127.0.0.1:{port}/"


def test_scratch_db_path_is_per_port_and_never_the_deployed_db():
    for port in RELAY_DEV_PORTS:
        path = scratch_db_path(port)
        assert path.name == f"pku-relay-dev-{port}.db"
        assert path.parent == Path(tempfile.gettempdir())


def test_resolve_db_path_env_respects_explicit_and_defaults_scratch(monkeypatch):
    monkeypatch.setenv("RELAY_DB_PATH", r"C:\scratch\explicit.db")
    assert resolve_db_path_env(8801) == r"C:\scratch\explicit.db"
    monkeypatch.delenv("RELAY_DB_PATH", raising=False)
    # a bare dev launch never falls back to the deployed /data/accounts.db
    default = resolve_db_path_env(8801)
    assert default == str(scratch_db_path(8801))
    assert Path(default).name == "pku-relay-dev-8801.db"
