"""First-run setup wizard core (the interactive parts live in the CLI).

Target-user experience: ``pku-sync setup`` asks for the PKU account once,
configures the selected MCP host, and lets that host own the one-time Notion
OAuth session. No file editing, ids, client secrets, or copied tokens.
"""

from __future__ import annotations

from pathlib import Path

from .envfile import write_env_values
from .notion_login import env_file_path


def apply_setup(values: dict[str, str], settings=None) -> Path:
    """Persist the provided config values; keys not given stay untouched."""
    if not values:
        return env_file_path(settings)
    return write_env_values(env_file_path(settings), values)


def _masked(value: str, keep: int = 6) -> str:
    if not value:
        return "未设置"
    if len(value) < keep + 6:  # short secrets: don't reveal most of them
        return "已设置"
    return f"{value[:keep]}…"


def summarize(settings) -> list[str]:
    """Masked one-line-per-item summary shown at the end of the wizard."""
    llm_backend = settings.llm_provider or "auto"
    return [
        f"教学网账号：{settings.pku_username or '未设置'}",
        f"教学网密码：{'已设置' if settings.pku_password else '未设置'}",
        f"LLM 简报：OPENAI_API_KEY {_masked(settings.openai_api_key)}（LLM_PROVIDER={llm_backend}）",
        f"MCP 宿主：{settings.agent_host}",
        "Notion：官方 MCP（OAuth 由宿主 keyring 管理）",
        f"REST fallback：{'token 已设置' if settings.notion_token else '未配置'}",
        f"数据目录：{settings.data_dir}",
    ]
