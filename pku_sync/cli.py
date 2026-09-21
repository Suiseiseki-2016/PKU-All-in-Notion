"""Command line entry point for the PKU course sync pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table


def _force_utf8_output() -> None:
    """Keep course names printable on the Windows console.

    A scheduled run inherits a GBK console there, and writing a course name or
    even a check mark to it raises UnicodeEncodeError after the real work is
    already done. Replacing unencodable characters keeps the log readable
    without letting output formatting fail a sync.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_output()

app = typer.Typer(help="Sync PKU Teaching Network courses, materials and recordings", no_args_is_help=True)
notion_app = typer.Typer(help="Notion REST fallback（高级/无 agent 环境）", no_args_is_help=True)
mcp_app = typer.Typer(help="本地课程 MCP server 与宿主配置", no_args_is_help=True)
submit_app = typer.Typer(help="教学网作业提交（文本作业；文件上传见 runbook 边界）", no_args_is_help=True)
app.add_typer(notion_app, name="notion")
app.add_typer(mcp_app, name="mcp")
app.add_typer(submit_app, name="submit")
console = Console()


class RichConsoleRecorder:
    """The CLI's `service.Recorder`: rich capture with markup-capable lines."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def begin(self) -> None:
        self._console.begin_capture()

    def print(self, text: str) -> None:
        self._console.print(text)

    def end(self) -> str:
        return self._console.end_capture()


def _daily_steps():
    """Bind today's steps exactly as `daily` runs them.

    The commands are looked up at call time, so tests (and any future
    caller) can still monkeypatch them before invoking `daily_cmd` /
    `automate_cmd`. The orchestration itself lives in `pku_sync.service`.
    """
    from functools import partial

    from .service import DailySteps

    return DailySteps(
        sync=partial(sync_cmd, course="", skip_recordings=False),
        download=partial(download_cmd, course="", limit=0),
        process=partial(process_cmd, course="", limit=0),
        summarize=partial(summarize_cmd, log=None),
    )


def _automate_events():
    """Automate's human-facing announcements, shared by CLI and panel."""
    from .service import AutomateEvents

    return AutomateEvents(
        daily_failed=lambda: console.print(
            "[red]daily 本步失败；继续晨检与批量建页，把管道异常登记进 Notion。[/red]"
        ),
        review_failed=lambda: console.print("[yellow]MCP review 失败；下次会重试。[/yellow]"),
        lecture_failed=lambda: console.print(
            "[yellow]讲义批量建页失败；建页是幂等的，下次 automate 会重试。[/yellow]"
        ),
    )


def _targets(client, course: str):
    """Resolve the `--course` option to a course list, defaulting to this term."""
    from .config import settings
    from .discover import discover_courses, resolve_course_name, select_courses
    from .models import Course

    courses = discover_courses(client)
    if course:
        picked = [c for c in courses if c.course_id == course]
        if picked:
            return picked
        return [Course(course_id=course, name=resolve_course_name(client, course), source="explicit")]
    return select_courses(courses, settings)


@app.command()
def discover(
    json_out: Annotated[bool, typer.Option("--json", help="Print raw JSON instead of a table")] = False,
) -> None:
    """List every course visible to the authenticated account."""
    from .auth import get_session
    from .config import settings
    from .discover import discover_courses, resolve_course_name, select_courses

    client = get_session()
    courses = discover_courses(client)
    if not courses:
        console.print("[red]No courses found.[/red] The session may have failed to establish.")
        raise typer.Exit(1)

    for course in courses:
        if course.name == course.course_id:
            course.name = resolve_course_name(client, course.course_id)

    selected = select_courses(courses, settings)

    if json_out:
        console.print_json(json.dumps([c.model_dump() for c in selected], ensure_ascii=False))
        return

    table = Table(title=f"{len(selected)} course(s)")
    table.add_column("course_id")
    table.add_column("name")
    table.add_column("code")
    table.add_column("source")
    for course in selected:
        table.add_row(course.course_id, course.name, course.code, course.source)
    console.print(table)


@app.command(name="recordings")
def recordings_cmd(
    course: Annotated[str, typer.Option(help="Only inspect this Blackboard course id")] = "",
    resolve: Annotated[bool, typer.Option("--resolve", help="Also resolve each media URL")] = False,
) -> None:
    """List lecture recordings available per course."""
    from .auth import get_session
    from .recordings import list_recordings, resolve_media

    client = get_session()
    for entry in _targets(client, course):
        try:
            items = list_recordings(client, entry.course_id)
        except Exception as exc:
            console.print(f"[yellow]{entry.name} ({entry.course_id}): {exc}[/yellow]")
            continue

        console.print(f"\n[bold]{entry.name}[/bold] ({entry.course_id}): {len(items)} recording(s)")
        for item in items:
            line = f"  {item.recorded_at or '?':<20} {item.title}"
            if resolve:
                try:
                    resolve_media(client, item)
                except Exception as exc:  # one bad session must not stop the list
                    line += f"\n      [yellow]resolve failed: {exc}[/yellow]"
                else:
                    line += "\n      " + (
                        item.media_url or f"[red]{item.unavailable_reason}[/red]"
                    )
            console.print(line)


@app.command(name="sync")
def sync_cmd(
    course: Annotated[str, typer.Option(help="Only sync this Blackboard course id")] = "",
    skip_recordings: Annotated[
        bool, typer.Option("--skip-recordings", help="List recordings without resolving media URLs")
    ] = False,
) -> None:
    """Download announcements, materials, assignments and recording metadata."""
    from .auth import get_session
    from .config import settings
    from .sync import sync_all

    client = get_session()
    targets = _targets(client, course)
    if not targets:
        console.print("[red]No courses selected.[/red]")
        raise typer.Exit(1)

    root = settings.data_dir
    console.print(f"Syncing {len(targets)} course(s) into {root.resolve()}\n")
    reports = sync_all(client, targets, root, resolve_recordings=not skip_recordings)

    table = Table(title="sync results")
    for column in ("course", "通知", "条目", "下载", "跳过", "作业", "录屏"):
        table.add_column(column)
    for report in reports:
        table.add_row(
            report.course.name[:28],
            str(report.announcements),
            str(report.items),
            str(report.downloaded),
            str(report.skipped),
            str(report.assignments),
            str(report.recordings),
            style=None if report.ok else "yellow",
        )
    console.print(table)

    for report in reports:
        for note in report.notes:
            console.print(f"[dim]{report.course.name[:24]}: {note}[/dim]")
        for error in report.errors:
            console.print(f"[yellow]{report.course.name[:24]}: {error}[/yellow]")


@app.command(name="download")
def download_cmd(
    course: Annotated[str, typer.Option(help="Only download this Blackboard course id")] = "",
    limit: Annotated[int, typer.Option(help="Stop after this many recordings (0 = all)")] = 0,
) -> None:
    """Download lecture recordings listed by a previous `sync`."""
    from .auth import get_session
    from .config import settings
    from .pipeline import collect_jobs, download_job

    jobs = [
        j
        for j in collect_jobs(settings.data_dir, course)
        if not (j.directory / "transcript.json").exists()
    ]
    if not jobs:
        console.print("[yellow]No recordings indexed. Run `sync` first.[/yellow]")
        raise typer.Exit(1)

    client = get_session()
    done = 0
    for job in jobs:
        if limit and done >= limit:
            break
        result = download_job(client, job)
        if result.skipped:
            console.print(f"[dim]已完整 {job.label}[/dim]")
        elif result.path is not None:
            size = result.path.stat().st_size / 1e6
            verb = "续传" if result.resumed else "下载"
            console.print(f"[green]{verb}完成[/green] {job.label} → {size:.0f} MB")
            done += 1
        else:
            console.print(f"[yellow]失败[/yellow] {job.label}: {result.error}")


@app.command(name="process")
def process_cmd(
    course: Annotated[str, typer.Option(help="Only process this Blackboard course id")] = "",
    limit: Annotated[int, typer.Option(help="Stop after this many recordings (0 = all)")] = 0,
) -> None:
    """Transcribe downloaded recordings, extract keyframes and write notes."""
    from .config import settings
    from .pipeline import collect_jobs, process_job

    jobs = [j for j in collect_jobs(settings.data_dir, course) if j.video.exists()]
    if not jobs:
        console.print("[yellow]No downloaded videos found. Run `download` first.[/yellow]")
        raise typer.Exit(1)

    for index, job in enumerate(jobs):
        if limit and index >= limit:
            break
        console.print(f"\n[bold]{job.label}[/bold]")
        result = process_job(job, settings)
        console.print(
            f"  转录 {'完成' if result.transcribed else '失败'}"
            f"  关键帧 {result.keyframes}"
            f"  笔记 {'完成' if result.notes else '跳过'}"
            f"{'  已删除视频' if result.removed_video else ''}"
        )
        for error in result.errors:
            console.print(f"  [yellow]{error}[/yellow]")


@app.command(name="setup")
def setup_cmd(
    host: Annotated[
        str, typer.Option("--host", help="MCP 宿主：factory | claude | codex")
    ] = "",
    no_mcp: Annotated[
        bool, typer.Option("--no-mcp", "--no-notion", help="跳过 MCP 配置与 Notion OAuth")
    ] = False,
) -> None:
    """首次配置：教学网账号 → MCP 宿主 → Notion OAuth。"""
    import getpass

    from . import mcp_setup
    from . import setup as setup_mod
    from .config import Settings, settings

    console.print("[bold]pku-sync 首次配置[/bold] 目标体验：选定一个 agent，Notion 只授权一次。\n")

    values: dict[str, str] = {}

    username = typer.prompt(
        "教学网（IAAA）用户名",
        default=settings.pku_username or "",
        show_default=bool(settings.pku_username),
    ).strip()
    if not username:
        console.print("[red]用户名不能为空。[/red]")
        raise typer.Exit(1)
    values["PKU_USERNAME"] = username

    password = getpass.getpass("教学网密码（输入不回显；留空 = 保留现有）: ")
    if password:
        values["PKU_PASSWORD"] = password
    elif not settings.pku_password:
        console.print("[red]还没有可用的教学网密码，请重新运行 pku-sync setup 并输入。[/red]")
        raise typer.Exit(1)

    key = typer.prompt(
        "OPENAI_API_KEY（回车 = 保留现有/跳过；跳过后简报自动改用本机已登录的 claude/codex/droid）",
        default="",
        show_default=False,
    ).strip()
    if key:
        values["OPENAI_API_KEY"] = key

    selected_host = (host or settings.agent_host or "claude").lower().strip()
    if selected_host not in mcp_setup.HOSTS:
        console.print(
            f"[red]未知 MCP 宿主 {selected_host!r}，可选：{', '.join(mcp_setup.HOSTS)}[/red]"
        )
        raise typer.Exit(2)
    values["AGENT_HOST"] = selected_host

    env_path = setup_mod.apply_setup(values, settings)
    console.print(f"[green]配置已写入[/green] {env_path}\n")

    if not no_mcp:
        try:
            messages = mcp_setup.configure(selected_host)
        except (FileNotFoundError, RuntimeError) as exc:
            console.print(f"[yellow]MCP 配置未完成：{exc}[/yellow]")
            console.print(
                f"安装/登录 {selected_host} 后重跑："
                f"`pku-sync mcp configure {selected_host} --login`\n"
            )
        else:
            for message in messages:
                console.print(f"[green]MCP[/green] {message}")
            if typer.confirm("现在完成一次 Notion OAuth？", default=True):
                try:
                    console.print(mcp_setup.authenticate(selected_host))
                except RuntimeError as exc:
                    console.print(f"[yellow]Notion OAuth 未完成：{exc}[/yellow]")

    console.print("\n[bold]当前配置[/bold]")
    for line in setup_mod.summarize(Settings()):
        console.print(f"  {line}")
    console.print(
        "\n下一步：`pku-sync mcp serve` 可验证本地 server；"
        "`pku-sync discover` 查看课程。"
    )


@mcp_app.command("serve")
def mcp_serve() -> None:
    """启动只读 stdio MCP server（通常由 Claude/Codex/Factory 自动拉起）。"""
    from .mcp_server import main

    main()


@mcp_app.command("configure")
def mcp_configure(
    host: Annotated[str, typer.Argument(help="factory | claude | codex")],
    login: Annotated[
        bool, typer.Option("--login", help="配置后启动官方 Notion MCP OAuth")
    ] = False,
) -> None:
    """为一个 agent 宿主添加 pku-sync 与官方 Notion MCP server。"""
    from . import mcp_setup

    try:
        for message in mcp_setup.configure(host.lower()):
            console.print(f"[green]{message}[/green]")
        if login:
            console.print(mcp_setup.authenticate(host.lower()))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _markdown_source(content_file: str | None, text: str | None) -> str:
    """Read the --file / --text content pair; exactly one source is allowed."""
    if bool(content_file) == bool(text):
        raise typer.BadParameter("请给且只给 --file <markdown 文件> 或 --text <内容> 之一")
    if content_file:
        try:
            return Path(content_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"读不了 {content_file}: {exc}") from exc
    return text or ""


@notion_app.command("login")
def notion_login(
    port: Annotated[
        int, typer.Option("--port", help="首选回调端口：8765 或 8766（被占用时自动用另一个）")
    ] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="不自动开浏览器，打印链接手动打开（SSH/无头场景）")
    ] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="等待授权完成的秒数")] = 300.0,
) -> None:
    """Notion 浏览器授权：授权码经平台 relay 交换，token 只写入本地 .env（需先激活）。"""
    from . import notion_login as notion_login_mod
    from .config import settings
    from .notion import NotionError

    if settings.notion_token:
        console.print("[yellow]已存在 NOTION_TOKEN，本次授权会覆盖它。[/yellow]")

    def show(url: str) -> None:
        console.print(f"在浏览器完成授权（没有自动打开就手动访问）：\n  {url}")
        console.print("勾选要分享的课程 hub 后点「允许」，等待回调…")

    try:
        info = notion_login_mod.login_via_relay(
            settings,
            port=port,
            open_browser=not no_browser,
            timeout=timeout,
            on_url=show,
        )
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]授权成功[/green]：workspace「{info.get('workspace_name') or '?'}」")
    console.print(f"本地回调端口：{info.get('callback_port') or '?'}")
    console.print(f"NOTION_TOKEN 已写入 {info['env_path']}（该文件在 .gitignore 里，不要外传）")
    try:
        console.print(f"[green]验证通过[/green]：{notion_login_mod.verify_token()}")
    except NotionError as exc:
        console.print(f"[yellow]token 已保存，但验证调用失败：{exc}[/yellow]")


@notion_app.command("check")
def notion_check() -> None:
    """验证 NOTION_TOKEN 并显示集成身份（GET /users/me）。"""
    from . import notion_login as notion_login_mod
    from .notion import NotionError

    try:
        identity = notion_login_mod.verify_token()
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]token 有效[/green]：{identity}")


@notion_app.command("fetch")
def notion_fetch(
    page_id: Annotated[str, typer.Argument(help="页面 id 或 URL")],
    json_out: Annotated[bool, typer.Option("--json", help="输出页面与子块的原始 JSON")] = False,
) -> None:
    """读取页面与其第一层子块（对账/幂等判定用）。"""
    from .notion import NotionError, get_client, page_title

    try:
        with get_client() as client:
            page = client.get_page(page_id)
            children = client.list_children(page["id"])
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        console.print_json(json.dumps({"page": page, "children": children}, ensure_ascii=False))
        return
    console.print(f"[bold]{page_title(page)}[/bold]  {page.get('url', '')}")
    if page.get("archived"):
        console.print("[yellow]该页面已归档[/yellow]")
    subpages = [b for b in children if b.get("type") == "child_page"]
    console.print(f"子块 {len(children)} 个，其中子页面 {len(subpages)} 个")
    for block in subpages:
        title = (block.get("child_page") or {}).get("title", "")
        url = f"https://www.notion.so/{block['id'].replace('-', '')}"
        console.print(f"  - {title} → {url}")


@notion_app.command("create-page")
def notion_create_page(
    parent: Annotated[str, typer.Option("--parent", help="父页面 id/URL")],
    title: Annotated[str, typer.Option("--title", help="新页面标题")],
    content_file: Annotated[str | None, typer.Option("--file", help="markdown 正文文件")] = None,
    text: Annotated[str | None, typer.Option("--text", help="markdown 正文（内联）")] = None,
) -> None:
    """在父页面下建子页（正文可选，markdown 自动转 blocks）。"""
    from .notion import NotionError, get_client, markdown_to_blocks, page_url

    children = markdown_to_blocks(_markdown_source(content_file, text)) if (content_file or text) else None
    try:
        with get_client() as client:
            page = client.create_page(parent, title, children=children)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    size = len(children) if children else 0
    console.print(f"[green]已创建[/green] {title} → {page_url(page)}（正文 {size} 块）")


@notion_app.command("append")
def notion_append(
    page: Annotated[str, typer.Option("--page", help="页面 id/URL")],
    content_file: Annotated[str | None, typer.Option("--file", help="markdown 正文文件")] = None,
    text: Annotated[str | None, typer.Option("--text", help="markdown 正文（内联）")] = None,
) -> None:
    """在页面末尾追加 markdown 内容（不动既有块）。"""
    from .notion import NotionError, get_client, markdown_to_blocks

    blocks = markdown_to_blocks(_markdown_source(content_file, text))
    try:
        with get_client() as client:
            client.append_blocks(page, blocks)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]已追加[/green] {len(blocks)} 块 → {page}")


@notion_app.command("replace")
def notion_replace(
    page: Annotated[str, typer.Option("--page", help="页面 id/URL")],
    content_file: Annotated[str | None, typer.Option("--file", help="markdown 正文文件")] = None,
    text: Annotated[str | None, typer.Option("--text", help="markdown 正文（内联）")] = None,
) -> None:
    """overwrite 模式：原地替换页面正文（URL/标题不动；仍有子页的页面会被拒绝）。"""
    from .notion import NotionError, get_client, markdown_to_blocks, page_url

    blocks = markdown_to_blocks(_markdown_source(content_file, text))
    try:
        with get_client() as client:
            client.replace_page_content(page, blocks)
            url = page_url(client.get_page(page))
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]正文已替换[/green] {len(blocks)} 块 → {url}")


@notion_app.command("upload-file")
def notion_upload_file(
    path: Annotated[str, typer.Argument(help="本地文件路径（如关键帧 jpg，≤ 20 MB）")],
) -> None:
    """上传单个文件，输出 file-upload://<id>（用于 markdown 内嵌图片）。"""
    from .notion import NotionError, get_client

    try:
        with get_client() as client:
            ref = client.upload_file(path)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(ref)


@notion_app.command("database")
def notion_database(
    database_id: Annotated[str, typer.Argument(help="数据库 id 或 collection:// URL")],
    json_out: Annotated[bool, typer.Option("--json", help="输出原始 schema JSON")] = False,
) -> None:
    """读取数据库 schema（建行前先看属性名与选项）。"""
    from .notion import NotionError, get_client

    try:
        with get_client() as client:
            db = client.get_database(database_id)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        console.print_json(json.dumps(db, ensure_ascii=False))
        return
    title = "".join(t.get("plain_text", "") for t in db.get("title") or [])
    console.print(f"[bold]{title}[/bold]  {db.get('url', '')}")
    for name, prop in (db.get("properties") or {}).items():
        ptype = prop.get("type", "")
        options = ""
        for key in ("select", "status", "multi_select"):
            if ptype == key:
                options = ", ".join(
                    o.get("name", "") for o in (prop.get(key) or {}).get("options") or []
                )
        suffix = f": {options}" if options else ""
        console.print(f"  {name} ({ptype}){suffix}")


@notion_app.command("query")
def notion_query(
    database: Annotated[str, typer.Option("--database", help="数据库 id 或 collection:// URL")],
    filter_json: Annotated[str | None, typer.Option("--filter", help="Notion filter JSON")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="输出原始行 JSON")] = False,
) -> None:
    """查询数据库行（对账判定：是否已有该作业/课件/通知）。"""
    from .notion import NotionError, get_client, page_title

    try:
        flt = json.loads(filter_json) if filter_json else None
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--filter 不是合法 JSON: {exc}") from exc
    try:
        with get_client() as client:
            rows = client.query_database(database, filter=flt)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        console.print_json(json.dumps(rows, ensure_ascii=False))
        return
    console.print(f"{len(rows)} 行")
    for row in rows:
        console.print(f"  - {page_title(row) or row['id']} → {row.get('url', '')}")


@notion_app.command("create-row")
def notion_create_row(
    database: Annotated[str, typer.Option("--database", help="数据库 id 或 collection:// URL")],
    properties_json: Annotated[str, typer.Option("--properties", help="属性 JSON，可用 pku_sync.notion 的 prop_* 帮助格式")],
) -> None:
    """在数据库里建一行（登记新作业/课件；对账后只补缺，不覆盖）。"""
    from .notion import NotionError, get_client, page_title, page_url

    try:
        props = json.loads(properties_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--properties 不是合法 JSON: {exc}") from exc
    try:
        with get_client() as client:
            row = client.create_database_row(database, props)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]已建行[/green] {page_title(row)} → {page_url(row)}")


@notion_app.command("search")
def notion_search(
    query_text: Annotated[str, typer.Argument(help="搜索词")],
    json_out: Annotated[bool, typer.Option("--json", help="输出原始结果 JSON")] = False,
) -> None:
    """在工作区搜索页面/数据库。"""
    from .notion import NotionError, get_client, page_title

    try:
        with get_client() as client:
            results = client.search(query_text)
    except NotionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        console.print_json(json.dumps(results, ensure_ascii=False))
        return
    if not results:
        console.print("[yellow]无结果（token 对应集成未获分享的页面搜不到）[/yellow]")
    for row in results:
        if row.get("object") == "database":
            title = "".join(t.get("plain_text", "") for t in row.get("title") or [])
            kind = "数据库"
        else:
            title = page_title(row)
            kind = "页面"
        console.print(f"  [{kind}] {title} → {row.get('url', '')}")


@app.command(name="summarize")
def summarize_cmd(
    log: Annotated[
        str | None,
        typer.Option("--log", help="指定 daily 日志路径（默认 logs/latest.daily.log）"),
    ] = None,
) -> None:
    """把最近一次 daily 日志整理成当日中文简报（LLM 后端，无需 agent 会话）。"""
    from .summarize import run

    path = run(log)
    if path is None:
        console.print(
            "[yellow]没有可用的 daily 日志（logs/latest.daily.log 缺失或为空），跳过简报。[/yellow]"
        )
        return
    console.print(f"[green]简报已写入[/green] {path}")


@app.command(name="review")
def review_cmd() -> None:
    """通过已选 agent 宿主和两组 MCP 完成 Notion 晨检登记。"""
    from .agent_runner import run_review
    from .config import settings

    try:
        result = run_review(settings)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        console.print(f"[red]MCP review 启动失败：{exc}[/red]")
        raise typer.Exit(1) from exc
    if result.returncode:
        console.print(
            f"[red]{result.host} review 失败（退出码 {result.returncode}）："
            f"{result.stderr[-400:]}[/red]"
        )
        raise typer.Exit(result.returncode)
    console.print(f"[green]MCP review 完成[/green] {result.output_path}")


@app.command(name="daily")
def daily_cmd(
    skip_process: Annotated[
        bool, typer.Option("--skip-process", help="Download only; leave transcription for the GPU host")
    ] = False,
    skip_summary: Annotated[
        bool, typer.Option("--skip-summary", help="Skip the LLM daily summary step")
    ] = False,
) -> None:
    """One unattended pass: sync, download, process, then summarize."""
    from .config import settings
    from .service import run_daily

    result = run_daily(
        settings,
        skip_process=skip_process,
        skip_summary=skip_summary,
        recorder=RichConsoleRecorder(console),
        steps=_daily_steps(),
    )
    if result.failure is not None:
        # Re-raise so the failure reaches the caller's exit code: the log
        # files (and their EXIT_CODE marker) are already written.
        raise result.failure


@app.command(name="lecture-batch")
def lecture_batch_cmd() -> None:
    """通过已选 agent 宿主和两组 MCP 幂等补建缺失的讲次页。"""
    from .agent_runner import run_lecture_batch
    from .config import settings

    try:
        result = run_lecture_batch(settings)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        console.print(f"[red]MCP 讲义批量建页启动失败：{exc}[/red]")
        raise typer.Exit(1) from exc
    if result.returncode:
        console.print(
            f"[red]{result.host} 讲义批量建页失败（退出码 {result.returncode}）："
            f"{result.stderr[-400:]}[/red]"
        )
        raise typer.Exit(result.returncode)
    console.print(f"[green]MCP 讲义批量建页完成[/green] {result.output_path}")


@app.command(name="automate")
def automate_cmd(
    skip_process: Annotated[
        bool, typer.Option("--skip-process", help="Download only; leave transcription for the GPU host")
    ] = False,
    skip_summary: Annotated[
        bool, typer.Option("--skip-summary", help="Skip the local LLM daily summary")
    ] = False,
    skip_review: Annotated[
        bool, typer.Option("--skip-review", help="Skip the MCP-backed Notion review")
    ] = False,
    skip_lecture: Annotated[
        bool, typer.Option("--skip-lecture", help="Skip the MCP-backed lecture-page backfill")
    ] = False,
) -> None:
    """One unattended pass: the daily pipeline, then the Notion review and lecture backfill."""
    from .config import settings
    from .service import run_automate

    result = run_automate(
        settings,
        skip_process=skip_process,
        skip_summary=skip_summary,
        skip_review=skip_review,
        skip_lecture=skip_lecture,
        recorder=RichConsoleRecorder(console),
        steps=_daily_steps(),
        review=review_cmd,
        lecture=lecture_batch_cmd,
        events=_automate_events(),
    )
    if result.daily.failure is not None:
        # Re-raise so any external scheduler's last-run status stays non-zero:
        # the failure should be visible outside the log file too, and the
        # EXIT_CODE marker is already written.
        raise result.daily.failure


@app.command(name="panel")
def panel_cmd(
    port: Annotated[
        int,
        typer.Option("--port", help="首选面板端口：8791、8792 或 8793（被占用时自动选下一个）"),
    ] = 8791,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="不自动打开浏览器")
    ] = False,
    fake: Annotated[
        bool, typer.Option("--fake", help="用内置演示数据启动（不需要连接 Notion）")
    ] = False,
    fake_variant: Annotated[
        str,
        typer.Option(
            "--fake-variant",
            help="演示状态：normal / slow / grade-slow / grade-recovery / low-balance / grade-502 / organize-blocked / organize-agent-missing / organize-agent-blocked / organize-agent-mcp-down / organize-agent-timeout / organize-agent-multiple / organize-agent-recovery / fault / fault-launch / transport-launch / missing-lecture / empty / usage-cap / exercise-fault-launch / exercise-missing（需配合 --fake）",
        ),
    ] = "normal",
) -> None:
    """启动 localhost 状态面板（server extra，只绑 127.0.0.1，8791→8792→8793 首个空闲）。"""
    import threading
    import webbrowser

    import uvicorn

    from .panel.ports import PanelPortError, bind_panel
    from .panel.webapi import create_app

    host = "127.0.0.1"  # docs/SERVICE_PLAN.md §5.3：本地面板只绑回环地址
    # The student directory service (real adapter by default; the seeded-fake
    # mode for browser verification) is shared by the directory API routes.
    # Built before binding so an invalid fake variant fails cleanly.
    # In --fake mode the connection and platform bridges are faked too, so a
    # verification run can drive activation and connect/disconnect without any
    # network call and without touching the student's real credentials. The
    # fake service assembly (variant wiring, relay failure injection) is shared
    # with the variant tests via ``fake_panel.build_fake_panel_services``.
    if fake:
        from .panel.fake_panel import build_fake_panel_services

        try:
            (directory_service, connection_service, platform_service,
             organizer_service, grading_service) = build_fake_panel_services(fake_variant)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    else:
        from .panel.directory import make_directory_service

        directory_service = make_directory_service()
        # None → create_app builds the production connection/platform bridges
        # against the loaded settings
        connection_service = None
        platform_service = None
        organizer_service = None
        grading_service = None
    try:
        sock, bound_port = bind_panel(port)
    except PanelPortError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    # The chosen port is surfaced in startup output; uvicorn's own banner is
    # suppressed at warning level, so this line is the user-facing proof.
    console.print(f"[green]面板已启动[/green]：http://{host}:{bound_port}/")
    if not no_browser:
        url = f"http://{host}:{bound_port}/"
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    # Serve on the pre-bound socket (the port we probed is the port uvicorn
    # binds — never a check-then-rebind race, never an arbitrary port).
    uvicorn.Server(
        uvicorn.Config(
            create_app(
                directory_service=directory_service,
                connection_service=connection_service,
                platform_service=platform_service,
                organizer_service=organizer_service,
                grading_service=grading_service,
            ),
            host=host,
            port=bound_port,
            log_level="warning",
        )
    ).run(sockets=[sock])


@app.command(name="tui")
def tui_cmd() -> None:
    """交互式工作台：课程资料、录音、Notion 状态、DDL 与 AI 操作。"""
    from .config import settings

    try:
        from .tui import PkuSyncApp
    except ImportError as exc:
        console.print(f"[red]TUI 需要 textual：pip install textual（或 uv pip install -e .）[/red]")
        console.print(f"[dim]原因：{exc}[/dim]")
        raise typer.Exit(1) from exc
    PkuSyncApp(settings.data_dir).run()


def _submit_assignment_table(rows: list) -> Table:
    table = Table(title="作业条目（content_id 用于 submit text / submit file）")
    for column in ("content_id", "截止时间", "作业", "来源"):
        table.add_column(column)
    for assignment in rows:
        table.add_row(
            assignment.content_id or "—",
            assignment.due_at or "未公布",
            assignment.title,
            assignment.source,
        )
    return table


@submit_app.command("list")
def submit_list(
    course: Annotated[
        str,
        typer.Option("--course", help="Blackboard course_id 或课程名片段（按本地 course.json 解析）"),
    ],
) -> None:
    """列出该课程内容树里的作业条目（含 content_id 与合并后的截止时间）。"""
    from .auth import get_session
    from .config import settings
    from .materials import fetch_deadlines, merge_deadlines, walk_materials
    from .submit import SubmitError, resolve_course_id

    try:
        course_id = resolve_course_id(settings.data_dir, course)
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    client = get_session()
    _items, assignments = walk_materials(client, course_id)
    deadlines = fetch_deadlines(client)
    merged = merge_deadlines(assignments, deadlines.get(course_id, []))
    if not merged:
        console.print("[yellow]该课程内容树里没有作业条目（教师可能未在教学网布置）。[/yellow]")
        return
    console.print(_submit_assignment_table(merged))


@submit_app.command("text")
def submit_text_cmd(
    course: Annotated[
        str,
        typer.Option("--course", help="Blackboard course_id 或课程名片段（按本地 course.json 解析）"),
    ],
    content: Annotated[str, typer.Option("--content", help="作业的 content_id（submit list 里查）")],
    file: Annotated[
        str | None, typer.Option("--file", help="答案的 markdown/文本文件路径（UTF-8）")
    ] = None,
    text: Annotated[str | None, typer.Option("--text", help="答案文本（与 --file 二选一）")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="确认执行提交。默认干跑：取表单、核状态、预览答案，但不提交"),
    ] = False,
) -> None:
    """提交文本作业到教学网。默认干跑，--yes 才真正提交。"""
    from .auth import get_session
    from .config import settings
    from .materials import fetch_deadlines, merge_deadlines, walk_materials
    from .submit import (
        AlreadySubmitted,
        SubmitError,
        deadline_warning,
        fetch_form,
        resolve_course_id,
        submit_text,
    )

    if bool(file) == bool(text):
        console.print("[red]请给且只给 --file <路径> 或 --text <内容> 之一。[/red]")
        raise typer.Exit(2)
    if file:
        try:
            answer = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]读不了 {file}: {exc}[/red]")
            raise typer.Exit(1) from exc
    else:
        answer = text or ""
    if not answer.strip():
        console.print("[red]答案为空，拒绝提交。[/red]")
        raise typer.Exit(2)

    try:
        course_id = resolve_course_id(settings.data_dir, course)
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    client = get_session()

    # Title + deadline for the preview and the late-submission warning.
    title = ""
    due_at = ""
    try:
        _items, assignments = walk_materials(client, course_id)
        deadlines = fetch_deadlines(client)
        for assignment in merge_deadlines(assignments, deadlines.get(course_id, [])):
            if assignment.content_id == content:
                title, due_at = assignment.title, assignment.due_at
                break
    except Exception as exc:  # listing is a convenience; the form is authoritative
        console.print(f"[yellow]作业信息查询失败（不影响提交）：{exc}[/yellow]")

    if warning := deadline_warning(due_at):
        console.print(f"[red]{warning}[/red]")

    try:
        form = fetch_form(client, course_id, content)
    except AlreadySubmitted as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"目标：{title or content}（{course_id} / {content}）截止：{due_at or '未公布'}")
    console.print(f"答案 {len(answer)} 字符；开头：{answer[:60].splitlines()[0] if answer else ''}")
    if not yes:
        console.print(
            "[yellow]干跑：表单已取得、可提交；加 --yes 才会真正提交。[/yellow]"
        )
        return

    try:
        receipt = submit_text(client, form, answer)
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]提交成功[/green] 回执 destinationUrl：{receipt.get('destinationUrl')}")
    console.print("请把回执登记回 Notion 任务行（scripts/assignment_submit.md 第 6 步）。")


@submit_app.command("file")
def submit_file_cmd(
    course: Annotated[
        str,
        typer.Option("--course", help="Blackboard course_id 或课程名片段（按本地 course.json 解析）"),
    ],
    content: Annotated[str, typer.Option("--content", help="作业的 content_id（submit list 里查）")],
    file: Annotated[str, typer.Option("--file", help="要提交的文件路径（docx/pdf/zip/md/txt…）")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="确认执行提交。默认干跑：取表单、核状态、预览文件，但不提交"),
    ] = False,
) -> None:
    """提交文件作业（附件形式）到教学网。默认干跑，--yes 才真正提交。"""
    from .auth import get_session
    from .config import settings
    from .materials import fetch_deadlines, merge_deadlines, walk_materials
    from .submit import (
        AlreadySubmitted,
        SubmitError,
        deadline_warning,
        fetch_form,
        resolve_course_id,
        submit_file,
    )

    path = Path(file)
    if not path.is_file():
        console.print(f"[red]文件不存在或不是普通文件：{file}[/red]")
        raise typer.Exit(1)

    try:
        course_id = resolve_course_id(settings.data_dir, course)
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    client = get_session()

    title = ""
    due_at = ""
    try:
        _items, assignments = walk_materials(client, course_id)
        deadlines = fetch_deadlines(client)
        for assignment in merge_deadlines(assignments, deadlines.get(course_id, [])):
            if assignment.content_id == content:
                title, due_at = assignment.title, assignment.due_at
                break
    except Exception as exc:  # listing is a convenience; the form is authoritative
        console.print(f"[yellow]作业信息查询失败（不影响提交）：{exc}[/yellow]")

    if warning := deadline_warning(due_at):
        console.print(f"[red]{warning}[/red]")

    try:
        form = fetch_form(client, course_id, content, action="newAttempt")
    except AlreadySubmitted as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    size = path.stat().st_size
    console.print(
        f"目标：{title or content}（{course_id} / {content}）截止：{due_at or '未公布'}"
    )
    console.print(f"文件：{path.name}（{size} 字节，{path.suffix.lstrip('.') or '未知'} 类型）")
    if not yes:
        console.print(
            "[yellow]干跑：表单已取得、可提交；加 --yes 才会真正提交。[/yellow]"
        )
        return

    try:
        receipt = submit_file(client, form, path)
    except SubmitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if "destinationUrl" in receipt:
        console.print(f"[green]提交成功[/green] 回执 destinationUrl：{receipt['destinationUrl']}")
    else:
        console.print(f"[green]提交成功[/green] 已上传 {path.name}（Blackboard 未返回回执 JSON）")
    console.print("请把回执登记回 Notion 任务行（scripts/assignment_submit.md 第 6 步）。")


@app.command(name="coursetable")
def coursetable_cmd(
    term: Annotated[
        str | None,
        typer.Option("--term", help="学期代码，如 25-26-2。默认取门户的当前学期"),
    ] = None,
    list_terms: Annotated[
        bool, typer.Option("--list-terms", help="只列出可选学期代码，不查课表")
    ] = False,
    raw: Annotated[bool, typer.Option("--raw", help="打印门户返回的原始 JSON")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="输出按天整理的 JSON")] = False,
) -> None:
    """只读获取个人课表（门户 myCourseTable，不写任何内容）。"""
    from .portal import (
        PortalError,
        fetch_course_table,
        fetch_terms,
        parse_course_table,
        portal_session,
        render_course_table,
        table_unavailable,
    )

    try:
        client = portal_session()
        if list_terms:
            current, terms = fetch_terms(client)
            table = Table(title="可选学期")
            table.add_column("代码")
            table.add_column("学期")
            table.add_column("当前")
            for entry in terms:
                table.add_row(entry.code, entry.label, "←" if entry.code == current else "")
            console.print(table)
            return
        body, used = fetch_course_table(client, term)
    except (PortalError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if raw:
        console.print_json(json.dumps(body, ensure_ascii=False))
        return
    if reason := table_unavailable(body):
        console.print(f"[yellow]{used} 学期没有可用课表（门户返回：{reason}）[/yellow]")
        console.print("新学期课表未发布时会这样。用 --list-terms 看可选学期，再用 --term 查历史学期。")
        return
    if json_out:
        rows = [
            {"day": s.day, "start_slot": s.start_slot, "end_slot": s.end_slot, "info": s.info}
            for s in parse_course_table(body)
        ]
        console.print_json(json.dumps(rows, ensure_ascii=False))
        return
    console.print(f"[dim]学期 {used}[/dim]")
    for line in render_course_table(body):
        console.print(line)


@app.command(name="grades")
def grades_cmd(
    json_out: Annotated[
        bool, typer.Option("--json", help="输出结构化 JSON（课程/评分项/得分/满分）")
    ] = False,
) -> None:
    """只读查询当前账号可访问的成绩（Blackboard Learn REST，不写任何内容）。"""
    from .auth import get_session
    from .grades import GradesError, fetch_all_grades

    try:
        client = get_session()
        records = fetch_all_grades(client)
    except (GradesError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        console.print_json(json.dumps([r.__dict__ for r in records], ensure_ascii=False))
        return
    if not records:
        console.print("[yellow]暂无成绩数据[/yellow]")
        return
    table = Table(title="成绩查询（只读）")
    for column in ("课程", "评分项", "得分", "满分"):
        table.add_column(column)
    for record in records:
        table.add_row(
            record.course_name,
            record.column_name,
            f"{record.score:.2f}" if record.score is not None else "—",
            f"{record.possible:.2f}" if record.possible is not None else "—",
        )
    console.print(table)


@app.command(name="doctor")
def doctor_cmd() -> None:
    """一键自检：配置、凭据、依赖、宿主、日志、报告新鲜度、磁盘。"""
    from .doctor import run_checks

    failures = run_checks(console)
    raise typer.Exit(1 if failures else 0)


if __name__ == "__main__":
    app()
