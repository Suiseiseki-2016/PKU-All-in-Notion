"""Pure and mocked tests for MCP host configuration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pku_sync import mcp_setup


@pytest.mark.parametrize("host,binary", [("factory", "droid"), ("claude", "claude"), ("codex", "codex")])
def test_configuration_commands(monkeypatch, host, binary):
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda name: f"/bin/{name}")
    commands = mcp_setup.configuration_commands(host, "/venv/python")
    assert len(commands) == 2
    assert commands[0].purpose == "local"
    assert commands[1].purpose == "notion"
    assert commands[0].argv[0] == f"/bin/{binary}"
    # The launcher must arrive in forward-slash form on every platform
    # (droid mcp add eats backslashes; see the posix-path test below).
    assert commands[0].argv[-2:] == (
        "/venv/python",
        str(mcp_setup.LAUNCHER).replace("\\", "/"),
    )
    assert mcp_setup.NOTION_MCP_URL in commands[1].argv


def test_configuration_commands_use_posix_paths(monkeypatch):
    # droid mcp add strips backslashes as escape characters (verified live
    # 2026-09-16, droid 0.218.2): registering E:\remote_project\... stored a
    # broken "E:remote_project..." command in ~/.factory/mcp.json. Registered
    # paths must therefore always arrive in forward-slash form.
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda name: "C:/npm/droid.cmd")
    commands = mcp_setup.configuration_commands(
        "factory", r"E:\remote_project\pku-course-sync\.venv\Scripts\python.exe"
    )
    local = commands[0].argv
    assert local[-2] == "E:/remote_project/pku-course-sync/.venv/Scripts/python.exe"
    assert local[-1].endswith("scripts/mcp_launcher.py")
    assert "\\" not in "".join(local)


def test_factory_and_host_login_commands(monkeypatch):
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda name: f"/bin/{name}")
    assert mcp_setup.login_command("factory") is None
    assert mcp_setup.login_command("claude") == ("/bin/claude", "mcp", "login", "notion")
    assert mcp_setup.login_command("codex") == ("/bin/codex", "mcp", "login", "notion")


def test_unknown_or_missing_host(monkeypatch):
    with pytest.raises(ValueError, match="unknown"):
        mcp_setup.executable("other")
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda _name: None)
    with pytest.raises(FileNotFoundError):
        mcp_setup.executable("factory")


def test_configure_success_and_existing(monkeypatch):
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda name: f"/bin/{name}")
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="added", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="server already exists"),
        ]
    )
    monkeypatch.setattr(mcp_setup.subprocess, "run", lambda *a, **kw: next(results))
    assert mcp_setup.configure("factory", "/py") == [
        "local: added",
        "notion: already configured",
    ]


def test_configure_failure(monkeypatch):
    monkeypatch.setattr(mcp_setup, "resolve_cli", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        mcp_setup.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=2, stdout="", stderr="bad config"),
    )
    with pytest.raises(RuntimeError, match="exit 2"):
        mcp_setup.configure("codex", "/py")
