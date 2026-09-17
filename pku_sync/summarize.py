"""当日简报：``scripts/summarize_daily.md`` 的 Python 移植。

不再为写一页简报起一个 droid 会话：把 ``logs/latest.daily.log`` 直接喂给
``llm.complete``（API 或已登录的 CLI 后端），由 Python 自己把简报写进
``logs/summary_<YYYYMMDD>.md``（文件已存在则 -2、-3 顺延，永不覆盖）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# Rich table rows land early in the log; errors land at the end. When a very
# long run would blow the context, keep both ends instead of the middle.
MAX_LOG_CHARS = 80_000
HEAD_KEEP = 2_000

SYSTEM_PROMPT = """你是课程同步管道的当日简报员。你只会收到一份管道日志；请只用日志里出现的信息写简报，禁止编造数字，禁止联网或调用任何工具，直接输出 Markdown 正文。

要求：
- 中文，短小，固定五个小节，每节几行：
  1. **同步结果**：同步了多少门课、新增多少公告/课件/作业/截止日期（日志里有计数行）。
  2. **课件与资料下载**：下载与跳过的数量。
  3. **课堂录音**：哪些录像被下载、转写、抽了关键帧，或被跳过/不可用（提到 unavailable_reason）。
  4. **错误与提示**：日志中的错误与说明。
  5. **剩余**：还有多少 video.mp4 在等转写（如日志有提及）。
- 如果日志里 EXIT_CODE 不为 0：简报开头先给醒目的失败横幅，并逐字引用最后几行错误。
- 只输出简报本身，不要客套话，不要重复标题。"""


def build_user_prompt(log_text: str) -> str:
    return f"以下是今日管道日志，请按系统指示写简报：\n\n```\n{_trim(log_text)}\n```"


def _trim(log_text: str) -> str:
    if len(log_text) <= MAX_LOG_CHARS:
        return log_text
    head = log_text[:HEAD_KEEP]
    tail = log_text[-(MAX_LOG_CHARS - HEAD_KEEP) :]
    return head + "\n\n……（日志过长，中间已截断）……\n\n" + tail


def summary_path(logs_dir: Path, day: date) -> Path:
    """Today's summary file, appending -2/-3/… when one already exists."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    candidate = logs_dir / f"summary_{day:%Y%m%d}.md"
    index = 2
    while candidate.exists():
        candidate = logs_dir / f"summary_{day:%Y%m%d}-{index}.md"
        index += 1
    return candidate


def run(log_path: str | Path | None = None, settings=None) -> Path | None:
    """Write today's summary from the latest daily log.

    Returns the summary path, or None when there is no log to summarize
    (the caller decides whether that is worth reporting loudly).
    """
    if settings is None:
        from .config import settings as default_settings

        settings = default_settings

    logs_dir = settings.data_dir / "logs"
    log_file = Path(log_path) if log_path else logs_dir / "latest.daily.log"
    if not log_file.exists() or log_file.stat().st_size == 0:
        return None
    log_text = log_file.read_text("utf-8", errors="replace")

    from .llm import complete

    answer = complete(SYSTEM_PROMPT, build_user_prompt(log_text), settings)

    target = summary_path(logs_dir, date.today())
    target.write_text(answer if answer.endswith("\n") else answer + "\n", "utf-8")
    return target
