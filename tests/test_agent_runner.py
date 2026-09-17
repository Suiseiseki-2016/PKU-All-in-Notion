"""Tests for portable headless agent commands."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pku_sync import agent_runner


@pytest.mark.parametrize("host", ["factory", "claude", "codex"])
def test_command_for_hosts(tmp_path, monkeypatch, host):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the review.", "utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")
    argv, stdin = agent_runner.command_for(host, tmp_path, data, prompt)
    joined = " ".join(argv)
    assert argv[0] == f"/bin/{host}"
    assert str(tmp_path.resolve()) in joined
    if host == "factory":
        assert "--add-tools" in argv
        # Default fallback when the droid help text cannot be inspected:
        # --add-tools is the flag of droid 0.218.x (verified live
        # 2026-09-16); the version-switch case is covered separately.
        assert "--enabled-tools" not in argv
        assert "MCP:notion,MCP:pku-sync" in argv
        assert "Runtime context" in joined
        assert stdin is None
    elif host == "claude":
        assert "--permission-mode" in argv
        assert str(data.resolve()) in argv
        assert "Runtime context" in joined
        assert stdin is None
        # --add-dir is variadic; the prompt element must come before it.
        prompt_at = next(i for i, arg in enumerate(argv) if "Runtime context" in arg)
        assert prompt_at < argv.index("--add-dir")
    else:
        assert argv[-1] == "-"
        assert stdin and "Runtime context" in stdin


def test_command_unknown_host(tmp_path, monkeypatch):
    prompt = tmp_path / "p.md"
    prompt.write_text("x", "utf-8")
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")
    with pytest.raises(ValueError, match="unknown"):
        agent_runner.command_for("other", tmp_path, tmp_path, prompt)


def test_command_for_includes_tui_target_payload(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Organize.", "utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")

    argv, stdin = agent_runner.command_for(
        "factory",
        tmp_path,
        data,
        prompt,
        payload="operation=quiz\ncourse_folder=计算机网络",
    )

    assert stdin is None
    assert "operation=quiz" in argv[-1]
    assert "course_folder=计算机网络" in argv[-1]


def test_factory_tool_flag_follows_installed_droid(tmp_path, monkeypatch):
    # droid 0.114.x registers MCP tools via --enabled-tools; droid 0.218.2
    # uses --add-tools. The runner must pick whichever flag the installed
    # CLI reports in `droid exec --help` (each binary cached separately).
    help_outputs = iter(
        [
            SimpleNamespace(returncode=0, stdout="--enabled-tools --disabled-tools", stderr=""),
            SimpleNamespace(returncode=0, stdout="--add-tools", stderr=""),
        ]
    )
    monkeypatch.setattr(agent_runner.subprocess, "run", lambda *a, **kw: next(help_outputs))
    calls = {"n": 0}

    def exe_by_binary(value):
        calls["n"] += 1
        return f"/bin/droid-{calls['n']}"

    monkeypatch.setattr(agent_runner, "executable", exe_by_binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do it.", "utf-8")
    data = tmp_path / "data"
    data.mkdir()

    argv_enabled, _ = agent_runner.command_for("factory", tmp_path, data, prompt)
    assert "--enabled-tools" in argv_enabled
    assert "--add-tools" not in argv_enabled
    assert "MCP:notion,MCP:pku-sync" in argv_enabled

    argv_add, _ = agent_runner.command_for("factory", tmp_path, data, prompt)
    assert "--add-tools" in argv_add
    assert "--enabled-tools" not in argv_add
    assert "MCP:notion,MCP:pku-sync" in argv_add


def test_run_review_sets_mcp_timeout_for_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="report body", stderr="")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    settings = SimpleNamespace(agent_host="claude", data_dir=tmp_path)
    result = agent_runner.run_review(settings)
    assert captured["env"]["MCP_TIMEOUT"] == "120000"
    assert "PATH" in captured["env"]
    assert result.returncode == 0
    assert result.output_path.exists()
    assert result.output_path.read_text("utf-8") == "report body"


def test_run_review_no_env_override_for_other_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="noise")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    settings = SimpleNamespace(agent_host="codex", data_dir=tmp_path)
    result = agent_runner.run_review(settings)
    assert captured["env"] is None
    assert result.returncode == 0
    assert result.stderr == "noise"


def test_run_lecture_batch_uses_batch_runbook_and_direct_data_note(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(agent_runner, "executable", lambda value: f"/bin/{value}")
    argv_seen: list[list[str]] = []
    prompts_seen: list[str] = []

    def fake_run(argv, **kwargs):
        argv_seen.append(argv)
        prompts_seen.append(__import__("pathlib").Path(argv[argv.index("-f") + 1]).read_text("utf-8"))
        return SimpleNamespace(returncode=0, stdout="batch report", stderr="")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    settings = SimpleNamespace(agent_host="factory", data_dir=tmp_path)
    result = agent_runner.run_lecture_batch(settings)
    assert result.returncode == 0
    assert result.output_path.name.startswith("lecture_")
    assert result.output_path.name.endswith(".md")
    assert result.output_path.read_text("utf-8") == "batch report"
    prompt = prompts_seen[0]
    # The batch runbook itself is the prompt, and its runtime context points
    # the agent at the data tree on disk (keyframes are binary uploads).
    assert "batch" in prompt
    assert "data_root" in prompt
    assert "keyframes" in prompt
    assert "Runtime context" in prompt
