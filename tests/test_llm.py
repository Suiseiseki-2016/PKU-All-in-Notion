"""Unit tests for the LLM backend layer (no network, no CLIs launched)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pku_sync import llm


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        llm_provider="auto",
        openai_api_key="",
        openai_base_url="https://example.invalid/v1",
        llm_model="",
        llm_timeout=5,
        notes_model="test-model",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_cli_is_a_plain_path_lookup(monkeypatch):
    monkeypatch.setattr(
        llm.shutil, "which", lambda name: f"/usr/bin/{name}" if name != "codex" else None
    )
    assert llm.resolve_cli("claude") == "/usr/bin/claude"
    assert llm.resolve_cli("codex") is None


def test_auto_prefers_api_key():
    assert llm._resolve_provider(make_settings(openai_api_key="sk-x")) == "openai"


def test_auto_falls_through_claude_codex_droid(monkeypatch):
    available: set[str] = set()
    monkeypatch.setattr(
        llm, "resolve_cli", lambda name: "/usr/bin/true" if name in available else None
    )
    available.add("claude")
    assert llm._resolve_provider(make_settings()) == "claude"
    available.clear()
    available.add("codex")
    assert llm._resolve_provider(make_settings()) == "codex"
    available.clear()
    available.add("droid")
    assert llm._resolve_provider(make_settings()) == "droid"


def test_auto_raises_when_nothing_available(monkeypatch):
    monkeypatch.setattr(llm, "resolve_cli", lambda name: None)
    with pytest.raises(RuntimeError, match="没有可用的 LLM 后端"):
        llm._resolve_provider(make_settings())


def test_explicit_provider_is_honored():
    assert llm._resolve_provider(make_settings(llm_provider="droid")) == "droid"
    with pytest.raises(RuntimeError, match="无效"):
        llm._resolve_provider(make_settings(llm_provider="gpt4"))


def test_cli_arg_builders():
    assert llm.claude_args("hi") == ["claude", "-p", "hi", "--output-format", "text"]
    assert llm.claude_args("hi", "m1")[-2:] == ["--model", "m1"]
    assert llm.droid_args("hi") == ["droid", "exec", "hi", "-o", "text"]
    assert llm.droid_args("hi", "m2")[-2:] == ["-m", "m2"]

    argv = llm.codex_args("hi", "/tmp/out.txt")
    assert argv[0] == "codex"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("--output-last-message") + 1] == "/tmp/out.txt"
    assert argv[-1] == "hi"
    assert llm.codex_args("hi", "/tmp/out.txt", "m3")[argv.index("--skip-git-repo-check") + 1 :][
        -3:-1
    ] == ["-m", "m3"]


def test_backends_listing(monkeypatch):
    monkeypatch.setattr(
        llm, "resolve_cli", lambda name: "/usr/bin/true" if name == "droid" else None
    )
    rows = {b.name: b.available for b in llm.backends(make_settings())}
    assert rows == {"openai": False, "claude": False, "codex": False, "droid": True}


def test_auto_candidates_order(monkeypatch):
    monkeypatch.setattr(
        llm, "resolve_cli", lambda name: "/usr/bin/true" if name in ("claude", "droid") else None
    )
    assert llm._auto_candidates(make_settings(openai_api_key="k")) == ["openai", "claude", "droid"]
    assert llm._auto_candidates(make_settings()) == ["claude", "droid"]


def test_complete_auto_falls_back_to_cli(monkeypatch):
    monkeypatch.setattr(llm, "resolve_cli", lambda name: "/usr/bin/true" if name == "droid" else None)
    settings = make_settings(openai_api_key="k")
    monkeypatch.setattr(
        llm, "_openai_complete", lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 503"))
    )
    monkeypatch.setattr(llm, "_cli_complete", lambda provider, system, user, st: f"via-{provider}")
    assert llm.complete("s", "u", settings) == "via-droid"


def test_complete_auto_raises_when_all_fail(monkeypatch):
    monkeypatch.setattr(llm, "resolve_cli", lambda name: "/usr/bin/true" if name == "droid" else None)
    settings = make_settings(openai_api_key="k")

    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm, "_openai_complete", boom)
    monkeypatch.setattr(llm, "_cli_complete", boom)
    with pytest.raises(RuntimeError, match="所有 LLM 后端都失败"):
        llm.complete("s", "u", settings)


def test_openai_retries_empty_content_then_succeeds(monkeypatch):
    class FakeResponse:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]}

    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse("" if calls["n"] == 1 else "ok after retry")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    result = llm._openai_complete("s", "u", make_settings(openai_api_key="k"))
    assert result == "ok after retry"
    assert calls["n"] == 2


def test_openai_raises_after_exhausted_retries(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="content 为空"):
        llm._openai_complete("s", "u", make_settings(openai_api_key="k"))
