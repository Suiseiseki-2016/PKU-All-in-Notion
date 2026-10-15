"""Removed workflows must be unavailable through the actual CLI registry."""
import pytest
from typer.testing import CliRunner

from pku_sync.cli import app


@pytest.mark.parametrize("command", ["daily", "automate", "summarize", "review", "lecture-batch", "tui"])
def test_retired_workflow_is_not_callable(command):
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_current_workspace_and_processing_commands_remain_available():
    for command in ("panel", "sync", "download", "process", "submit"):
        assert CliRunner().invoke(app, [command, "--help"]).exit_code == 0
