"""Host-specific MCP configuration for Factory, Claude Code, and Codex."""

from __future__ import annotations

import subprocess
import sys
import json
from dataclasses import dataclass
from pathlib import Path

from .llm import resolve_cli

NOTION_MCP_URL = "https://mcp.notion.com/mcp"
LOCAL_SERVER_NAME = "pku-sync"
NOTION_SERVER_NAME = "notion"
HOSTS = ("factory", "claude", "codex")

# Hosts spawn the local server from their own cwd, where the venv's
# editable-install .pth may not fire: Python 3.11+ site.py silently skips
# .pth files flagged UF_HIDDEN (FILE_ATTRIBUTE_HIDDEN on Windows), and
# iCloud's Desktop sync keeps re-applying that flag. The launcher script
# puts the project root on sys.path itself, so registration is immune.
LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_launcher.py"


def host_integration_catalog(python: str | None = None, data_dir: Path | None = None) -> dict:
    """Portable stdio setup snippets; no host installation or config mutation."""
    py = _posix_path(python or sys.executable)
    launcher = LAUNCHER.as_posix()
    root = (data_dir or Path('data')).expanduser().resolve().as_posix()
    env = {'DATA_DIR': root}
    server = {'command': py, 'args': [launcher], 'env': env}
    standard = json.dumps({'mcpServers': {LOCAL_SERVER_NAME: server}}, ensure_ascii=False, indent=2)
    toml = ('[mcp_servers.pku-sync]\ncommand = ' + json.dumps(py) + '\nargs = [' +
            json.dumps(launcher) + ']\n\n[mcp_servers.pku-sync.env]\nDATA_DIR = ' + json.dumps(root))
    prompt = '请调用 pku-sync MCP 工具读取我本学期的课程总结与近期课堂笔记，帮我梳理核心考点与待办作业。'
    hosts = []
    for key, name, path, cli, snippet in [
        ('codex', 'Codex', '~/.codex/config.toml',
         f'codex mcp add pku-sync --env "DATA_DIR={root}" -- "{py}" "{launcher}"', toml),
        ('claude', 'Claude', '~/.claude.json（Claude Code）；Claude Desktop：设置 → 开发者 → 编辑配置',
         f'claude mcp add --scope user --env "DATA_DIR={root}" pku-sync -- "{py}" "{launcher}"', standard),
        ('antigravity', 'Antigravity', '~/.gemini/antigravity/mcp_config.json', '', standard),
        ('oh-my-pi', 'Oh My Pi', '~/.omp/agent/mcp.json', '', standard),
        ('opencode', 'OpenCode', '~/.config/opencode/opencode.json', '',
         json.dumps({'mcp': {LOCAL_SERVER_NAME: {'type': 'local', 'command': [py, launcher],
                     'environment': env, 'enabled': True}}}, ensure_ascii=False, indent=2)),
    ]:
        hosts.append({'id': key, 'name': name, 'badge': '本地只读 · MCP',
                      'description': '读取课程总结、课堂笔记与作业线索。将配置合并到现有文件，保存后重启 AI 工具。',
                      'cli_command': cli, 'config_path': path, 'config_snippet': snippet,
                      'sample_prompt': prompt})
    return {'python_path': py, 'launcher_path': launcher, 'hosts': hosts}


@dataclass(frozen=True)
class HostCommand:
    argv: tuple[str, ...]
    purpose: str


def executable(host: str) -> str:
    names = {"factory": "droid", "claude": "claude", "codex": "codex"}
    if host not in names:
        raise ValueError(f"unknown MCP host {host!r}; choose: {', '.join(HOSTS)}")
    # Same PATH-only resolution as the LLM CLI backends (llm.resolve_cli):
    # the host CLI must be on PATH, scheduled sessions included.
    found = resolve_cli(names[host])
    if not found:
        raise FileNotFoundError(f"{names[host]} is not installed or not on PATH")
    return found


def _posix_path(value: str) -> str:
    """Forward-slash form of a path argument.

    ``droid mcp add`` (verified live 2026-09-16, 0.218.2) strips backslashes
    out of arguments as escape characters, so registering
    ``E:\\remote_project\\...`` stores a broken
    ``E:remote_project...`` command in ~/.factory/mcp.json. Forward slashes
    are valid Windows path separators for CreateProcess and survive every
    host's argument parsing. A plain replace (not ``Path.as_posix``) is
    deliberate: POSIX ``PurePath`` treats ``\\`` as an ordinary filename
    character and would not normalize a Windows path.
    """
    return value.replace("\\", "/")


def configuration_commands(host: str, python: str | None = None) -> list[HostCommand]:
    """Return commands that add the local and official Notion MCP servers."""
    exe = executable(host)
    py = _posix_path(python or sys.executable)
    local = (py, _posix_path(str(LAUNCHER)))
    if host == "factory":
        return [
            HostCommand((exe, "mcp", "add", LOCAL_SERVER_NAME, *local), "local"),
            HostCommand(
                (exe, "mcp", "add", "--type", "http", NOTION_SERVER_NAME, NOTION_MCP_URL),
                "notion",
            ),
        ]
    if host == "claude":
        return [
            HostCommand(
                (exe, "mcp", "add", "--scope", "user", LOCAL_SERVER_NAME, "--", *local),
                "local",
            ),
            HostCommand(
                (
                    exe,
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "--transport",
                    "http",
                    NOTION_SERVER_NAME,
                    NOTION_MCP_URL,
                ),
                "notion",
            ),
        ]
    return [
        HostCommand((exe, "mcp", "add", LOCAL_SERVER_NAME, "--", *local), "local"),
        HostCommand(
            (exe, "mcp", "add", NOTION_SERVER_NAME, "--url", NOTION_MCP_URL),
            "notion",
        ),
    ]


def login_command(host: str) -> tuple[str, ...] | None:
    exe = executable(host)
    if host == "claude":
        return (exe, "mcp", "login", NOTION_SERVER_NAME)
    if host == "codex":
        return (exe, "mcp", "login", NOTION_SERVER_NAME)
    return None  # Factory completes remote-server OAuth from /mcp.


def configure(host: str, python: str | None = None) -> list[str]:
    """Add both servers, tolerating a server that is already configured."""
    messages: list[str] = []
    for command in configuration_commands(host, python):
        result = subprocess.run(
            command.argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or result.stderr).strip()
        if result.returncode == 0:
            messages.append(f"{command.purpose}: added")
            continue
        lower = output.lower()
        if "already" in lower or "exists" in lower or "已存在" in output:
            messages.append(f"{command.purpose}: already configured")
            continue
        raise RuntimeError(
            f"{command.purpose} MCP configuration failed "
            f"(exit {result.returncode}): {output[-400:]}"
        )
    return messages


def authenticate(host: str) -> str:
    """Start the host-owned OAuth flow, or return Factory's one-step guidance."""
    command = login_command(host)
    if command is None:
        return "在 Factory/Droid 中运行 /mcp，选择 notion，然后点击浏览器授权。"
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"{host} Notion MCP OAuth failed (exit {result.returncode})")
    return f"{host} Notion MCP OAuth 已完成"
