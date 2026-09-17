"""Run the MCP-backed Notion runbooks through Factory, Claude Code, or Codex.

The deterministic pipeline (sync/download/process/summarize) stays pure
Python. This module only adds the steps that need Notion credentials: it
hands a portable runbook (``scripts/daily_review_mcp.md`` for the morning
review, ``scripts/lecture_batch_creator.md`` for the idempotent
lecture-page backfill) to a non-interactive agent session whose host owns
the official Notion MCP OAuth token, so pku-sync itself never stores any
Notion secret.
"""

from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .mcp_setup import executable

REVIEW_RUNBOOK = "daily_review_mcp.md"
LECTURE_RUNBOOK = "lecture_batch_creator.md"
NOTES_RUNBOOK = "tui_notes_organizer.md"
REORGANIZE_RUNBOOK = "tui_notion_reorganizer.md"
REORGANIZE_SETUP_RUNBOOK = "e2e_notion_reorganize_setup.md"
REORGANIZE_VERIFY_RUNBOOK = "e2e_notion_reorganize_verify.md"
QUIZ_RUNBOOK = "tui_quiz_builder.md"
GRADE_RUNBOOK = "tui_quiz_grader.md"
QUIZ_ANSWER_SETUP_RUNBOOK = "e2e_notion_quiz_answer_setup.md"
GRADE_VERIFY_RUNBOOK = "e2e_notion_grade_verify.md"
QUIZ_VERIFY_RUNBOOK = "e2e_notion_quiz_verify.md"

# How each runbook is expected to read local course state. The review
# runbook goes through the read-only pku-sync MCP tools; the lecture
# runbook additionally needs the binary keyframe files for Notion upload,
# so it reads the data tree directly (every host grants that: --add-dir
# for claude/codex, plain file access for droid).
_DATA_NOTES = {
    REVIEW_RUNBOOK: "Read local course state only through the pku-sync MCP tools.",
    LECTURE_RUNBOOK: (
        "Read the local data tree under data_root directly (notes.md / "
        "transcript.json / recording.json / keyframes are files on disk; "
        "keyframes are binary images destined for Notion upload). The "
        "pku-sync MCP tools remain available for cross-checking."
    ),
    NOTES_RUNBOOK: (
        "Read the targeted recording directory under data_root directly; "
        "notes.md and transcript.json are text, and keyframes may be uploaded "
        "through the official Notion MCP file-upload tools."
    ),
    REORGANIZE_RUNBOOK: (
        "Read the targeted local recording files and the existing Notion page "
        "through their respective MCP tools; preserve existing Notion URLs."
    ),
    REORGANIZE_SETUP_RUNBOOK: (
        "Use the official Notion MCP to create one explicitly marked E2E child "
        "page under the authoritative course page and add one E2E comment."
    ),
    REORGANIZE_VERIFY_RUNBOOK: (
        "Read the target Notion page, parent and comments through MCP. This is "
        "read-only verification; do not create, edit, move, or delete anything."
    ),
    QUIZ_RUNBOOK: (
        "Read the selected course's local notes/transcripts under data_root "
        "and use the official Notion MCP tools for quiz pages and comments."
    ),
    GRADE_RUNBOOK: (
        "Read the selected course's local source notes and the existing Notion "
        "quiz/answers through MCP; write grading results only to the target page."
    ),
    QUIZ_ANSWER_SETUP_RUNBOOK: (
        "Use the official Notion MCP to append one explicitly marked E2E answer "
        "fixture to the target quiz page; do not touch other pages."
    ),
    GRADE_VERIFY_RUNBOOK: (
        "Read the target quiz page, parent and grading blocks through MCP. This "
        "is read-only verification; do not create, edit, move, or delete."
    ),
    QUIZ_VERIFY_RUNBOOK: (
        "Read the target Notion quiz page and the selected local source notes "
        "through MCP. This is a read-only verification; do not create, edit, "
        "move, or delete any Notion content."
    ),
}


@dataclass(frozen=True)
class AgentRun:
    host: str
    output_path: Path
    returncode: int
    stderr: str = ""
    output: str = ""

    @property
    def tui_succeeded(self) -> bool:
        """New TUI runbooks must explicitly report a completed Notion action."""
        return self.returncode == 0 and "TUI_RESULT=success" in self.output


@functools.lru_cache(maxsize=4)
def _factory_tool_flag(exe: str) -> str:
    """Pick the MCP tool flag the installed droid CLI actually supports.

    droid 0.114.x registers extra tools via ``--enabled-tools``; droid
    0.218.2 removed that flag and uses ``--add-tools`` instead (both live
    verified). Parse ``droid exec --help`` once per binary and prefer
    whichever flag is present; fall back to ``--add-tools`` if the CLI
    cannot be inspected.
    """
    try:
        help_text = subprocess.run(
            [exe, "exec", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        help_text = ""
    return "--enabled-tools" if "--enabled-tools" in help_text else "--add-tools"


def command_for(
    host: str,
    project_root: Path,
    data_dir: Path,
    prompt_file: Path,
    data_note: str | None = None,
    payload: str = "",
) -> tuple[list[str], str | None]:
    """Build one host's non-interactive command and optional stdin prompt."""
    exe = executable(host)
    root = project_root.resolve()
    data = data_dir.expanduser().resolve()
    prompt = prompt_file.read_text("utf-8")
    note = data_note or _DATA_NOTES.get(prompt_file.name, _DATA_NOTES[REVIEW_RUNBOOK])
    context = (
        "\n\n## Runtime context (authoritative)\n"
        f"- project_root: {root}\n"
        f"- data_root: {data}\n"
        f"- {note}\n"
        "- Read/write Notion only through the official notion MCP tools.\n"
    )
    target = ""
    if payload:
        target = (
            "## TUI operation target (authoritative; parse before anything else)\n"
            + payload.strip()
            + "\n\n"
        )
    full_prompt = target + prompt + context
    if host == "factory":
        # Pick the tool flag by the installed droid version: 0.114.x uses
        # --enabled-tools, 0.218.2 uses --add-tools (verified live for both;
        # see _factory_tool_flag). The prompt stays the last positional
        # argument.
        return (
            [
                exe,
                "exec",
                "--cwd",
                str(root),
                "--auto",
                "high",
                _factory_tool_flag(exe),
                "MCP:notion,MCP:pku-sync",
                "-o",
                "text",
                full_prompt,
            ],
            None,
        )
    if host == "claude":
        # The prompt must precede --add-dir: that flag is variadic and would
        # swallow a trailing prompt as another directory (verified live
        # 2026-09-15: "Input must be provided either through stdin or as a
        # prompt argument when using --print").
        return (
            [
                exe,
                "-p",
                full_prompt,
                "--permission-mode",
                "auto",
                "--add-dir",
                str(data),
            ],
            None,
        )
    if host == "codex":
        return (
            [
                exe,
                "exec",
                "-C",
                str(root),
                "--sandbox",
                "workspace-write",
                "--add-dir",
                str(data),
                "-",
            ],
            full_prompt,
        )
    raise ValueError(f"unknown agent host: {host}")


def run_runbook(
    settings,
    runbook: str,
    output_prefix: str,
    timeout: int = 3600,
    payload: str = "",
) -> AgentRun:
    """Execute one portable runbook and persist the agent's report."""
    root = Path(__file__).resolve().parent.parent
    prompt = root / "scripts" / runbook
    if not prompt.exists():
        raise FileNotFoundError(f"runbook not found: {prompt}")
    logs = settings.data_dir.expanduser().resolve() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    output = logs / f"{output_prefix}_{datetime.now().strftime('%Y%m%d')}.md"
    argv, input_text = command_for(
        settings.agent_host,
        root,
        settings.data_dir,
        prompt,
        payload=payload,
    )
    prompt_path: Path | None = None
    if settings.agent_host == "factory":
        # Long/CJK positional prompts are unreliable through Windows
        # CreateProcess. The runner used by manual lecture notes already
        # proved that droid's UTF-8 -f transport is stable.
        full_prompt = argv.pop()
        prompt_path = logs / f".{output_prefix}_prompt_{os.getpid()}.md"
        prompt_path.write_text(full_prompt, "utf-8")
        argv += ["-f", str(prompt_path)]
    # Claude Code's headless session can see slow HTTP MCP servers (notion) as
    # "still connecting" and give up before their tools register. MCP_TIMEOUT
    # (ms) widens that window; verified live 2026-09-15: the default lost the
    # race in a short session, 120s connected on the first check.
    env = None
    if settings.agent_host == "claude":
        env = {**os.environ, "MCP_TIMEOUT": "120000"}
    try:
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=root,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{settings.agent_host} {runbook} timed out after {timeout}s"
            ) from exc
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
    output.write_text(result.stdout or "", "utf-8")
    return AgentRun(
        host=settings.agent_host,
        output_path=output,
        returncode=result.returncode,
        stderr=(result.stderr or "")[-2000:],
        output=result.stdout or "",
    )


def run_review(settings, timeout: int = 3600) -> AgentRun:
    """Morning Notion reconciliation (``scripts/daily_review_mcp.md``)."""
    return run_runbook(settings, REVIEW_RUNBOOK, "review", timeout)


def run_lecture_batch(settings, timeout: int = 3600) -> AgentRun:
    """Idempotent lecture-page backfill (``scripts/lecture_batch_creator.md``)."""
    return run_runbook(settings, LECTURE_RUNBOOK, "lecture", timeout)


def run_notes_organizer(settings, payload: str, timeout: int = 3600) -> AgentRun:
    """Create a Notion lecture page from one locally ready recording."""
    return run_runbook(settings, NOTES_RUNBOOK, "tui_notes", timeout, payload)


def run_notion_reorganizer(settings, payload: str, timeout: int = 3600) -> AgentRun:
    """Comment on and reorganize an existing Notion lecture page."""
    return run_runbook(settings, REORGANIZE_RUNBOOK, "tui_reorganize", timeout, payload)


def run_e2e_reorganize_setup(settings, payload: str, timeout: int = 600) -> AgentRun:
    """Create one marked E2E page and comment for the reorganization scenario."""
    return run_runbook(
        settings, REORGANIZE_SETUP_RUNBOOK, "e2e_reorganize_setup", timeout, payload
    )


def run_e2e_reorganize_verifier(settings, payload: str, timeout: int = 600) -> AgentRun:
    """Read-only verification of the comment-reorganization E2E page."""
    return run_runbook(
        settings, REORGANIZE_VERIFY_RUNBOOK, "e2e_reorganize_verify", timeout, payload
    )


def run_quiz_builder(settings, payload: str, timeout: int = 3600) -> AgentRun:
    """Create or refresh a Notion quiz for the selected course scope."""
    return run_runbook(settings, QUIZ_RUNBOOK, "tui_quiz", timeout, payload)


def run_quiz_grader(settings, payload: str, timeout: int = 3600) -> AgentRun:
    """Grade answers already present in a Notion quiz page."""
    return run_runbook(settings, GRADE_RUNBOOK, "tui_grade", timeout, payload)


def run_e2e_quiz_answer_setup(settings, payload: str, timeout: int = 600) -> AgentRun:
    """Append a marked answer fixture to a real E2E quiz page."""
    return run_runbook(
        settings, QUIZ_ANSWER_SETUP_RUNBOOK, "e2e_quiz_answer_setup", timeout, payload
    )


def run_e2e_grade_verifier(settings, payload: str, timeout: int = 600) -> AgentRun:
    """Read-only verification of a real quiz grading E2E."""
    return run_runbook(settings, GRADE_VERIFY_RUNBOOK, "e2e_grade_verify", timeout, payload)


def run_quiz_verifier(settings, payload: str, timeout: int = 600) -> AgentRun:
    """Read-only verification of a real E2E quiz page."""
    return run_runbook(settings, QUIZ_VERIFY_RUNBOOK, "tui_quiz_verify", timeout, payload)
