"""Unit tests for the first-run setup wizard core and the .env writer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import envfile, notion as notion_mod, notion_login, setup as setup_mod
from pku_sync.notion import NotionError


def isolated_env(monkeypatch, tmp_path: Path) -> Path:
    """Point both env search paths at temp dirs so tests never touch the repo .env."""
    monkeypatch.chdir(tmp_path)
    pkg_root = tmp_path / "fake-pkg-root"
    pkg_root.mkdir()
    monkeypatch.setattr(notion_login, "_PACKAGE_ROOT", pkg_root)
    return pkg_root


# -- envfile.write_env_values --------------------------------------------------


def test_write_env_values_creates_file(tmp_path):
    env = tmp_path / ".env"
    envfile.write_env_values(env, {"PKU_USERNAME": "stu1", "PKU_PASSWORD": "pw"})
    assert env.read_text("utf-8") == "PKU_USERNAME=stu1\nPKU_PASSWORD=pw\n"


def test_write_env_values_upserts_and_preserves(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# top comment\nPKU_USERNAME=old\nOPENAI_API_KEY=sk-keep\n# mid comment\n",
        encoding="utf-8",
    )
    envfile.write_env_values(env, {"PKU_USERNAME": "new", "NOTION_TOKEN": "secret_t"})
    text = env.read_text("utf-8")
    assert "PKU_USERNAME=new" in text
    assert "PKU_USERNAME=old" not in text
    assert "NOTION_TOKEN=secret_t" in text
    assert "OPENAI_API_KEY=sk-keep" in text  # untouched lines stay
    assert "# top comment" in text
    assert "# mid comment" in text
    assert text.index("PKU_USERNAME=new") < text.index("NOTION_TOKEN=secret_t")  # new keys append


def test_write_env_values_empty_mapping_is_noop(tmp_path):
    env = tmp_path / ".env"
    envfile.write_env_values(env, {})
    assert not env.exists()


def test_write_env_token_delegates(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KEEP=1\n", encoding="utf-8")
    notion_login.write_env_token(env, "secret_a")
    text = env.read_text("utf-8")
    assert "KEEP=1" in text and "NOTION_TOKEN=secret_a" in text


# -- resolve_client (self-hosted REST fallback only) ----------------------------


def test_resolve_client_uses_env_values():
    settings = SimpleNamespace(notion_oauth_client_id="own-id", notion_oauth_client_secret="own-secret")
    assert notion_login.resolve_client(settings) == ("own-id", "own-secret")


def test_resolve_client_raises_when_missing():
    settings = SimpleNamespace(notion_oauth_client_id="", notion_oauth_client_secret="")
    with pytest.raises(NotionError, match="OAuth 客户端"):
        notion_login.resolve_client(settings)


# -- setup.apply_setup / summarize ----------------------------------------------


def test_apply_setup_writes_values(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path)
    path = setup_mod.apply_setup({"PKU_USERNAME": "stu1"}, None)
    assert path == tmp_path / ".env"
    assert "PKU_USERNAME=stu1" in path.read_text("utf-8")


def test_apply_setup_empty_keeps_file_untouched(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path)
    path = setup_mod.apply_setup({}, None)
    assert path == tmp_path / ".env"
    assert not path.exists()  # nothing to write → no file created


def test_summarize_masks_secrets():
    full = SimpleNamespace(
        pku_username="2300012345",
        pku_password="hunter2",
        openai_api_key="abcdef123456skfixture",  # obvious fake; only a prefix may leak
        llm_provider="",
        agent_host="factory",
        notion_token="secret_token",
        data_dir=Path("data"),
        schedule="0 6 * * *",
    )
    lines = "\n".join(setup_mod.summarize(full))
    assert "2300012345" in lines  # student id shows in full
    assert "hunter2" not in lines  # short password: fully hidden
    assert "abcdef123456" not in lines  # long key: only a prefix leaks
    assert "secret_token" not in lines
    assert "官方 MCP" in lines
    assert "REST fallback：token 已设置" in lines
    assert "MCP 宿主：factory" in lines
    assert "abcdef…" in lines  # _masked keeps a 6-char prefix; full key stays hidden

    empty = SimpleNamespace(
        pku_username="",
        pku_password="",
        openai_api_key="",
        llm_provider="",
        agent_host="codex",
        notion_token="",
        data_dir=Path("data"),
        schedule="0 6 * * *",
    )
    lines2 = "\n".join(setup_mod.summarize(empty))
    assert "未设置" in lines2 and "REST fallback：未配置" in lines2
    assert "MCP 宿主：codex" in lines2


# -- verify_token ----------------------------------------------------------------


def test_verify_token_constructs_fresh_settings(monkeypatch):
    from pku_sync import config as config_mod

    captured = {}

    class FakeClient:
        def __init__(self, settings):
            captured["settings"] = settings

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def whoami(self):
            return {"name": "pku-sync", "bot": {"owner": {"workspace_name": "测试空间"}}}

    monkeypatch.setattr(notion_mod, "get_client", lambda settings=None: FakeClient(settings))
    monkeypatch.setattr(config_mod, "Settings", lambda: "fresh-settings")

    assert notion_login.verify_token() == "pku-sync @ 测试空间"
    assert captured["settings"] == "fresh-settings"  # re-read .env, not the cached instance
