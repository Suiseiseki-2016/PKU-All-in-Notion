"""One-shot LLM calls for the daily chain, without an agent session.

Backends:
- ``openai``  any OpenAI-compatible chat-completions endpoint (httpx, no SDK)
- ``claude``  a logged-in Claude Code CLI, headless: ``claude -p``
- ``codex``   a logged-in Codex CLI: ``codex exec --output-last-message``
- ``droid``   a logged-in Droid CLI: ``droid exec -o text``

``auto`` tries the usable backends in order -- the API when
OPENAI_API_KEY is set, then the claude, codex and droid CLIs -- because a
flaky provider route must not cost the unattended chain its summary. An
explicitly configured provider is never silently switched. The CLIs are
used as plain text backends only: no tools, no MCP; the prompt embeds all
input, so nothing else on the machine is read.

The API path retries transient failures (429/5xx, OpenRouter's
200-with-error-body, empty choices, reasoning routes that leave
``message.content`` blank). All subprocess I/O pins UTF-8 explicitly: a
scheduled Windows session decodes native tool output with the console
codepage (GBK on a Chinese system), which would garble a Chinese summary
even though the CLI printed it correctly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

PROVIDERS = ("openai", "claude", "codex", "droid")

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_ATTEMPTS = 3

@dataclass
class Backend:
    name: str
    available: bool
    detail: str = ""


def resolve_cli(name: str) -> str | None:
    """Locate a CLI binary: a plain PATH lookup (``shutil.which``).

    The CLI must be installed and on PATH — scheduled/minimal environments
    inherit the user's PATH, so no OS-specific well-known dirs are probed.
    """
    return shutil.which(name)


def backends(settings) -> list[Backend]:
    """What is usable right now, in auto-pick order (for UX and tests)."""
    rows = [
        Backend(
            "openai",
            bool(settings.openai_api_key),
            "" if settings.openai_api_key else "OPENAI_API_KEY 未设置",
        )
    ]
    for name in ("claude", "codex", "droid"):
        path = resolve_cli(name)
        rows.append(Backend(name, path is not None, path or "CLI 未安装"))
    return rows


def _auto_candidates(settings) -> list[str]:
    order: list[str] = []
    if settings.openai_api_key:
        order.append("openai")
    for name in ("claude", "codex", "droid"):
        if resolve_cli(name):
            order.append(name)
    if not order:
        raise RuntimeError(
            "没有可用的 LLM 后端：OPENAI_API_KEY 未设置，claude / codex / droid CLI 也未安装。"
            "请在 .env 里配置 OPENAI_API_KEY 或 LLM_PROVIDER，或安装并登录其中一个 CLI。"
        )
    return order


def _resolve_provider(settings) -> str:
    wanted = (settings.llm_provider or "auto").strip().lower()
    if wanted not in PROVIDERS and wanted != "auto":
        raise RuntimeError(
            f"LLM_PROVIDER={settings.llm_provider!r} 无效，可选：auto / {' / '.join(PROVIDERS)}"
        )
    if wanted != "auto":
        return wanted
    return _auto_candidates(settings)[0]


def complete(system: str, user: str, settings=None) -> str:
    """One LLM call. Returns the assistant text; raises RuntimeError on failure."""
    if settings is None:
        from .config import settings as default_settings

        settings = default_settings

    if (settings.llm_provider or "auto").strip().lower() != "auto":
        return _complete_via(_resolve_provider(settings), system, user, settings)

    failures: list[str] = []
    for provider in _auto_candidates(settings):
        try:
            return _complete_via(provider, system, user, settings)
        except RuntimeError as exc:
            failures.append(f"{provider}: {exc}")
    raise RuntimeError("所有 LLM 后端都失败了。" + " | ".join(failures))


def _complete_via(provider: str, system: str, user: str, settings) -> str:
    if provider == "openai":
        return _openai_complete(system, user, settings)
    return _cli_complete(provider, system, user, settings)


def _openai_complete(system: str, user: str, settings) -> str:
    model = settings.llm_model or settings.notes_model
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    detail = ""
    message: dict = {}
    for attempt in range(1, _ATTEMPTS + 1):
        response = httpx.post(
            f"{settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
            timeout=settings.llm_timeout,
        )
        if response.status_code != 200:
            detail = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code in _RETRYABLE_STATUS and attempt < _ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            break
        body = response.json()
        # OpenRouter occasionally answers HTTP 200 with an error object in
        # the body (provider-side failure); treat it like a retryable error.
        if body.get("error"):
            detail = f"provider error: {json.dumps(body['error'], ensure_ascii=False)[:200]}"
            if attempt < _ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            break
        choices = body.get("choices") or []
        if not choices:
            detail = "choices 为空"
            if attempt < _ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            break
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if content:
            return content
        # Reasoning-style routes sometimes burn the whole completion budget
        # on thinking and leave message.content blank; retry before giving
        # up (the detail keeps finish_reason and the message keys for triage).
        detail = (
            f"content 为空（finish={choices[0].get('finish_reason')}，"
            f"message keys={sorted(message)}）"
        )
        if attempt < _ATTEMPTS:
            time.sleep(2 * attempt)
            continue
        break

    raise RuntimeError(f"LLM（{model} @ {settings.openai_base_url}）: {detail}")


def claude_args(prompt: str, model: str = "") -> list[str]:
    """Argv for Claude Code's headless text mode."""
    args = ["claude", "-p", prompt, "--output-format", "text"]
    return args + (["--model", model] if model else [])


def droid_args(prompt: str, model: str = "") -> list[str]:
    """Argv for Droid's non-interactive text mode (read-only is enough here)."""
    args = ["droid", "exec", prompt, "-o", "text"]
    return args + (["-m", model] if model else [])


def codex_args(prompt: str, message_file: str, model: str = "") -> list[str]:
    """Argv for Codex's non-interactive mode.

    ``--skip-git-repo-check``: the daily chain runs from data directories that
    are not Git repositories. ``-s read-only``: this is a text backend, the
    model has no business touching the disk. ``--output-last-message`` keeps
    the answer free of the CLI's progress chatter.
    """
    args = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--output-last-message",
        message_file,
    ]
    return args + (["-m", model] if model else []) + [prompt]


def _cli_complete(provider: str, system: str, user: str, settings) -> str:
    prompt = f"{system}\n\n---\n\n{user}"
    binary = resolve_cli(provider)
    if not binary:
        raise RuntimeError(f"{provider} CLI 不可用（未安装、未登录或不在 PATH）")

    if provider == "claude":
        argv = claude_args(prompt, settings.llm_model)
    elif provider == "droid":
        argv = droid_args(prompt, settings.llm_model)
    else:
        return _codex_complete(binary, prompt, settings)

    argv[0] = binary
    return _run(argv, settings)


def _run(argv: list[str], settings) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.llm_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LLM 后端超时（>{settings.llm_timeout}s）：{argv[0]}"
        ) from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"LLM 后端退出码 {result.returncode}: {tail}")
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError("LLM 后端没有输出任何内容")
    return text


def _codex_complete(binary: str, prompt: str, settings) -> str:
    """codex exec with the final message written to a file (verified 0.154)."""
    marker = Path(tempfile.mkstemp(suffix=".txt")[1])
    try:
        _run([binary, *codex_args(prompt, str(marker), settings.llm_model)[1:]], settings)
        text = marker.read_text("utf-8").strip()
        if not text:
            raise RuntimeError("codex 没有写出最终消息（--output-last-message 为空）")
        return text
    finally:
        marker.unlink(missing_ok=True)
