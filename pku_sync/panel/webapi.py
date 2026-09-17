"""The localhost panel: data-tree status plus daily/automate triggers.

Server-rendered with zero external assets (an offline campus laptop is a
first-class environment), and the page polls an HTML fragment so a
running job surfaces without a reload. Binds loopback only — the panel
serves the machine's own user and carries no auth of its own
(docs/SERVICE_PLAN.md §5.3).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..pipeline import collect_jobs, recording_stage
from .jobs import JobRunner
from .pipelines import ACTIONS, make_runner

if TYPE_CHECKING:
    from ..config import Settings

EXIT_RE = re.compile(r"=== EXIT_CODE=(\d+) ===")
LOG_TAIL_CHARS = 4000


def latest_daily(logs_dir: Path) -> dict:
    """Exit code + tail of ``logs/latest.daily.log``, or an absence marker."""
    log_file = logs_dir / "latest.daily.log"
    if not log_file.exists():
        return {"exists": False}
    text = log_file.read_text("utf-8", errors="replace")
    codes = EXIT_RE.findall(text)
    return {
        "exists": True,
        "exit_code": int(codes[-1]) if codes else None,
        "tail": text[-LOG_TAIL_CHARS:],
    }


def recording_status(data_dir: Path) -> list[dict]:
    """Every indexed recording's five-stage ladder, grouped per course."""
    courses: dict[str, list[dict]] = {}
    for job in collect_jobs(data_dir):
        stage, unavailable = recording_stage(job)
        courses.setdefault(job.course_name, []).append(
            {
                "title": job.recording.title,
                "date": job.recording.date,
                "stage": stage,
                "unavailable_reason": unavailable,
            }
        )
    return [
        {"name": name, "recordings": recordings} for name, recordings in courses.items()
    ]


REPORT_GLOBS = {"晨检": "review_*.md", "批量建页": "lecture_*.md"}


def notion_reports(logs_dir: Path) -> list[dict]:
    """Latest Notion-runbook report per kind, straight from the data tree."""
    reports = []
    for kind, pattern in REPORT_GLOBS.items():
        matches = sorted(logs_dir.glob(pattern)) if logs_dir.is_dir() else []
        if matches:
            latest = matches[-1]
            head = latest.read_text("utf-8", errors="replace")[:600]
            reports.append({"kind": kind, "file": latest.name, "head": head})
        else:
            reports.append({"kind": kind, "file": None, "head": ""})
    return reports


def _status_payload(settings, runner: JobRunner) -> dict:
    running = runner.current()
    last = runner.last()
    return {
        "running": running.as_dict() if running else None,
        "last": last.as_dict() if last else None,
        "daily": latest_daily(settings.data_dir / "logs"),
        "recordings": recording_status(settings.data_dir),
        "notion": notion_reports(settings.data_dir / "logs"),
    }


def render_status(payload: dict) -> str:
    """The status area, as one HTML fragment (also embedded on first load)."""
    parts: list[str] = []
    running = payload["running"]
    if running:
        parts.append(
            f'<div class="running">▶ 正在跑 <b>{html.escape(running["kind"])}</b>'
            f'（{running["started"]} 开始）</div>'
        )
    else:
        parts.append('<div class="dim">空闲</div>')
    last = payload["last"]
    if last:
        state = "✅" if last["state"] == "done" else "❌"
        error = f' — {html.escape(last["error"])}' if last["error"] else ""
        parts.append(
            f"<div>上次任务：{html.escape(last['kind'])} {state}"
            f"（exit {last['exit_code']}）{error}</div>"
        )
    daily = payload["daily"]
    if daily.get("exists"):
        code = daily.get("exit_code")
        mark = "✅" if code == 0 else "❌"
        parts.append(f"<div>最近一次 daily：{mark} EXIT_CODE={code}</div>")
    else:
        parts.append('<div class="dim">还没有 daily 日志</div>')
    parts.append('<h2 class="sec">课程与录音</h2>')
    parts.append('<table class="recs"><tr><th>讲次</th><th>日期</th><th>阶段</th><th>操作</th></tr>')
    # Course-level actions preview the product GUI's 整理/小测/批改 (the TUI's
    # a/z/g/o). They stay disabled until the M2 cloud/REST path lands — the
    # operator keeps using the TUI for these today.
    ops = (
        '<button class="op" disabled title="M2 接入面板；现阶段用 TUI（a/z/g/o）">整理</button>'
        '<button class="op" disabled title="M2 接入面板；现阶段用 TUI（a/z/g/o）">生成小测</button>'
        '<button class="op" disabled title="M2 接入面板；现阶段用 TUI（a/z/g/o）">批改</button>'
    )
    for course in payload["recordings"]:
        parts.append(
            f'<tr class="course"><td colspan="3">{html.escape(course["name"])}</td>'
            f'<td class="ops">{ops}</td></tr>'
        )
        for rec in course["recordings"]:
            stage = rec["unavailable_reason"] or rec["stage"]
            parts.append(
                f"<tr><td>{html.escape(rec['title'])}</td>"
                f"<td>{html.escape(rec['date'])}</td>"
                f"<td>{html.escape(stage)}</td></tr>"
            )
    parts.append("</table>")
    parts.append('<h2 class="sec">Notion 报告</h2>')
    parts.append('<div class="dim">晨检/建页 runbook 的最近一份报告</div>')
    for rep in payload["notion"]:
        if rep["file"]:
            parts.append(
                f'<div class="rep">{html.escape(rep["kind"])}：{html.escape(rep["file"])}'
                f'<details><summary>开头预览</summary>'
                f"<pre>{html.escape(rep['head'])}</pre></details></div>"
            )
        else:
            parts.append(f'<div class="dim">{html.escape(rep["kind"])}：还没有报告</div>')
    tail = daily.get("tail") or ""
    if tail:
        parts.append(
            "<details><summary>最近日志尾部</summary>"
            f"<pre>{html.escape(tail)}</pre></details>"
        )
    return "\n".join(parts)


_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>PKU All in Notion 状态面板</title>
<style>
 body { font-family: system-ui, "Microsoft YaHei", sans-serif; margin: 2rem auto;
        max-width: 60rem; background: #111418; color: #e8e8e8; }
 h1 { font-size: 1.4rem; }
 button { padding: .55rem 1.1rem; margin-right: .8rem; font-size: .95rem;
          cursor: pointer; border-radius: .35rem; border: 1px solid #444;
          background: #1d232b; color: #e8e8e8; }
 button:hover { background: #2a3340; }
 .running { color: #ffd166; } .dim { color: #7a828c; }
 table { border-collapse: collapse; margin-top: 1rem; width: 100%; }
 td, th { border-bottom: 1px solid #2b3038; padding: .32rem .6rem;
          text-align: left; font-size: .92rem; }
 tr.course td { font-weight: 600; background: #181d23; }
 pre { white-space: pre-wrap; background: #181d23; padding: .8rem;
       border-radius: .4rem; font-size: .85rem; }
 details { margin-top: 1rem; }
 h2.sec { margin: 1.6rem 0 .4rem; font-size: 1.05rem; }
 .op { padding: .18rem .55rem; margin-right: .3rem; font-size: .8rem;
       border-radius: .3rem; border: 1px solid #3a4149; background: #161b21;
       color: #7a828c; cursor: not-allowed; }
 .quota { margin-top: .6rem; color: #7a828c; font-size: .85rem; }
 .rep { margin-top: .4rem; }
 button:disabled { opacity: .55; }
</style>
</head>
<body>
<h1>PKU All in Notion 状态面板</h1>
<div id="actions">
 <button onclick="act('daily')">跑 daily（同步 → 下载 → 转写 → 简报）</button>
 <button onclick="act('automate')">跑 automate（daily + Notion 晨检/建页）</button>
 <span id="msg"></span>
</div>
<div class="quota">云端配额：未连接平台 —— M2 接入后显示 转写分钟 / LLM 配额（账号制，用户不持有任何 key）</div>
<div id="status">__STATUS__</div>
<script>
async function act(kind) {
  const r = await fetch('/actions/' + kind, {method: 'POST'});
  document.getElementById('msg').textContent =
      r.ok ? '已开始' : (r.status === 409 ? '上一个任务还在跑' : '触发失败');
  poll();
}
async function poll() {
  const r = await fetch('/partials/status');
  document.getElementById('status').innerHTML = await r.text();
}
setInterval(poll, 3000);
</script>
</body>
</html>"""


def create_app(settings=None, runner: JobRunner | None = None) -> FastAPI:
    """Build the panel app; every piece is injectable for tests."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    if runner is None:
        runner = make_runner(settings)

    app = FastAPI(title="pku-sync panel", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.runner = runner

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE.replace("__STATUS__", render_status(_status_payload(settings, runner)))

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_fragment() -> str:
        return render_status(_status_payload(settings, runner))

    @app.get("/api/status")
    def status_json() -> dict:
        return _status_payload(settings, runner)

    @app.post("/actions/{kind}")
    def trigger(kind: str) -> dict:
        if kind not in ACTIONS:
            raise HTTPException(status_code=404, detail=f"unknown action: {kind}")
        job = runner.submit(kind)
        if job is None:
            raise HTTPException(status_code=409, detail="上一个任务还在跑")
        return {"started": job.kind}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
