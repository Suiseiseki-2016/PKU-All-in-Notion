"""The localhost panel: data-tree status plus daily/automate triggers.

Server-rendered with zero external assets (an offline campus laptop is a
first-class environment), and the page polls an HTML fragment so a
running job surfaces without a reload. Binds loopback only — the panel
serves the machine's own user and carries no auth of its own.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..pipeline import collect_jobs, recording_stage
from .jobs import ExerciseJobGate, JobRunner
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
        "platform_active": bool(getattr(settings, "platform_token", "")),
    }


def render_status(payload: dict) -> str:
    """The status area, as one HTML fragment (also embedded on first load)."""
    parts: list[str] = []
    if payload["platform_active"]:
        parts.append('<div class="cloud-ok">云端转写已启用</div>')
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
        max-width: 64rem; background: #f6f8fb; color: #1d2939; }
 h1 { font-size: 1.7rem; margin-bottom: .2rem; } .sub { color: #667085; }
 button { padding: .65rem 1rem; margin-right: .5rem; font-size: .95rem;
          cursor: pointer; border-radius: .5rem; border: 0; background: #175cd3; color: white; }
 button:hover { background: #1849a9; }
 .card { background: white; padding: 1.2rem; border-radius: .8rem; margin: 1rem 0;
         box-shadow: 0 1px 3px #10182818; }
 .running { color: #b54708; } .dim { color: #667085; } .cloud-ok { color: #067647; font-weight: 600; }
 table { border-collapse: collapse; margin-top: 1rem; width: 100%; }
 td, th { border-bottom: 1px solid #eaecf0; padding: .48rem .6rem;
          text-align: left; font-size: .92rem; }
 tr.course td { font-weight: 600; background: #f9fafb; }
 pre { white-space: pre-wrap; background: #f9fafb; padding: .8rem;
       border-radius: .4rem; font-size: .85rem; }
 details { margin-top: 1rem; }
 h2.sec { margin: 1.6rem 0 .4rem; font-size: 1.05rem; }
 .op { padding: .18rem .55rem; margin-right: .3rem; font-size: .8rem;
       border-radius: .3rem; border: 1px solid #3a4149; background: #161b21;
       color: #7a828c; cursor: not-allowed; }
 .quota { margin-top: .6rem; color: #475467; font-size: .95rem; }
 .rep { margin-top: .4rem; }
 button:disabled { opacity: .55; }
</style>
</head>
<body>
<h1>PKU All in Notion</h1><div class="sub">课程资料、课堂录像与学习笔记，都在这里。</div>
<div class="sub"><a href="/app">打开学生面板</a>（学生端界面；本页是工程状态面板）</div>
<div class="card" id="activation">
 <b>云端转写</b><div class="quota" id="quota">正在检查账户状态…</div>
 <div id="activate-form"><input id="code" placeholder="输入兑换码" autocomplete="off">
 <button onclick="activate()">激活云端转写</button></div>
</div>
<div class="card" id="actions">
 <button onclick="act('daily')">跑 daily（同步 → 下载 → 转写 → 简报）</button>
 <button onclick="act('automate')">跑 automate（daily + Notion 晨检/建页）</button>
 <span id="msg"></span>
</div>
<div class="card" id="status">__STATUS__</div>
<script>
async function act(kind) {
  const r = await fetch('/actions/' + kind, {method: 'POST'});
  document.getElementById('msg').textContent =
      r.ok ? '已开始' : (r.status === 409 ? '上一个任务还在跑' : '触发失败');
  poll();
}
async function quota() {
  const r = await fetch('/api/platform/quota'); const p = await r.json();
  const el = document.getElementById('quota');
  if (!p.active) { el.textContent = '还没有激活。输入兑换码后可使用云端转写。'; return; }
  document.getElementById('activate-form').style.display = 'none';
  el.textContent = p.available ? ('剩余 ' + Math.floor((p.transcribe_seconds_remaining || 0) / 60) + ' 分钟转写额度') : '账户已激活，暂时无法读取额度。';
}
async function activate() {
  const r = await fetch('/api/platform/activate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:document.getElementById('code').value})});
  const p = await r.json(); document.getElementById('quota').textContent = r.ok ? ('激活成功，剩余 ' + Math.floor(p.transcribe_seconds_remaining / 60) + ' 分钟。') : (p.detail || '激活失败');
  if (r.ok) { document.getElementById('activate-form').style.display='none'; poll(); }
}
async function poll() {
  const r = await fetch('/partials/status');
  document.getElementById('status').innerHTML = await r.text();
}
quota();
setInterval(poll, 3000);
</script>
</body>
</html>"""


def create_app(
    settings=None,
    runner: JobRunner | None = None,
    directory_service=None,
    connection_service=None,
    platform_service=None,
    organizer_service=None,
    grading_service=None,
    exercise_job_gate: ExerciseJobGate | None = None,
    update_service=None,
) -> FastAPI:
    """Build the panel app; every piece is injectable for tests.

    ``directory_service`` is the student directory service (real adapter by
    default, seeded-fake for ``pku-sync panel --fake``). It is attached here
    so the directory/sync/materials/launch routes share one service and its
    honest sync state. ``connection_service`` owns the Notion connection
    state (revoke clears the local token and drops that cached directory),
    and ``platform_service`` is the relay bridge behind activation/quota.
    """
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    if runner is None:
        runner = make_runner(settings)
    if directory_service is None:
        from .directory import make_directory_service

        directory_service = make_directory_service(settings)
    if connection_service is None:
        from .connection import make_connection_service

        connection_service = make_connection_service(
            settings, directory_service=directory_service
        )
    if platform_service is None:
        from .platform_bridge import RealPlatformBridge

        platform_service = RealPlatformBridge(settings)

    app = FastAPI(title="pku-sync panel", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.runner = runner
    app.state.directory_service = directory_service
    app.state.connection_service = connection_service
    app.state.platform_service = platform_service
    if organizer_service is None:
        from .exercise_organizer import make_organizer_service
        organizer_service = make_organizer_service(settings, directory_service, platform_service)
    app.state.organizer_service = organizer_service
    if grading_service is None:
        from .exercise_grader import make_grading_service
        grading_service = make_grading_service(settings, directory_service, platform_service)
    app.state.grading_service = grading_service
    if exercise_job_gate is None:
        exercise_job_gate = ExerciseJobGate()
    app.state.exercise_job_gate = exercise_job_gate
    if update_service is None:
        from ..autoupdate import UpdateService

        update_service = UpdateService.from_settings(settings)
    app.state.update_service = update_service

    from .connection_api import add_connection_routes
    from .directory_api import add_directory_routes
    from .student_ui import add_student_ui_routes
    from .exercise_api import add_organize_routes
    from .grading_api import add_grading_routes

    add_directory_routes(app, directory_service)
    add_connection_routes(app, connection_service)
    add_student_ui_routes(app)
    add_organize_routes(app, organizer_service, gate=exercise_job_gate,
                         ui_e2e_mode=bool(getattr(settings, "exercise_ui_e2e_mode", False)))
    add_grading_routes(app, grading_service, gate=exercise_job_gate)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE.replace("__STATUS__", render_status(_status_payload(settings, runner)))

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_fragment() -> str:
        return render_status(_status_payload(settings, runner))

    @app.get("/api/status")
    def status_json() -> dict:
        return _status_payload(settings, runner)

    @app.get("/api/platform/quota")
    def platform_quota() -> dict:
        return platform_service.quota()

    @app.post("/api/platform/activate")
    def platform_activate(code: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.activate(code)
        except PlatformError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/update")
    def update_status() -> dict:
        return update_service.panel_state()

    @app.post("/api/update/request")
    def request_update(version: str = Body(..., embed=True)) -> dict:
        try:
            update_service.request_apply(version)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid update request") from exc
        return update_service.panel_state()

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
