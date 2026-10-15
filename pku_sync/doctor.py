"""一键自检：把整条链路的健康检查收进 `pku-sync doctor`。

每项检查返回 ok / warn / fail 三态：
  - fail 让命令以退出码 1 结束（链路肯定跑不动）；
  - warn 不挡路，但值得看一眼（报告偏旧、依赖缺失等）。

全部检查都是纯 Python（读文件 + PATH 探测 + 磁盘用量），不依赖任何操作系统的
专属接口；CLI / 磁盘探测走模块级入口，方便测试隔离。
"""


from __future__ import annotations


import shutil


from dataclasses import dataclass


from pathlib import Path




from .config import Settings, settings as default_settings


project_root = lambda: Path(__file__).resolve().parent.parent  # noqa: E731


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


def ok(name: str, detail: str = "") -> Check:
    return Check(name, "ok", detail)


def warn(name: str, detail: str = "") -> Check:
    return Check(name, "warn", detail)


def fail(name: str, detail: str = "") -> Check:
    return Check(name, "fail", detail)


def check_env_file(s: Settings) -> Check:
    root_env = project_root() / ".env"
    if root_env.is_file() or Path(".env").is_file():
        return ok(".env 配置", str(root_env if root_env.is_file() else Path(".env").resolve()))
    return fail(".env 配置", "未找到 .env（账号/密钥都在里面）")


def check_credentials(s: Settings) -> Check:
    missing = []
    if not s.pku_username or not s.pku_password:
        missing.append("PKU_USERNAME/PKU_PASSWORD")
    if not s.openai_api_key and s.llm_provider == "openai":
        missing.append("OPENAI_API_KEY")
    if missing:
        return fail("凭据", "缺 " + ", ".join(missing))
    return ok("凭据")


def check_agent_host(s: Settings) -> Check:
    binary = {"factory": "droid"}.get(s.agent_host, s.agent_host)
    path = shutil.which(binary)
    if path:
        return ok("Agent 宿主", f"{s.agent_host}（{Path(path).name}）")
    return fail("Agent 宿主", f"找不到 {binary} CLI（{s.agent_host} 宿主靠它；确认已安装且在 PATH 上）")


def check_disk(s: Settings) -> Check:
    data = s.data_dir.expanduser()
    target = data if data.is_dir() else data.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return warn("磁盘", f"无法读取 {target} 的磁盘用量")
    free_gb = usage.free / (1 << 30)
    if free_gb < 1:
        return fail("磁盘", f"仅剩 {free_gb:.1f} GB")
    if free_gb < 5:
        return warn("磁盘", f"仅剩 {free_gb:.1f} GB")
    return ok("磁盘", f"余 {free_gb:.0f} GB")


ALL_CHECKS = [
    check_env_file,
    check_credentials,
    check_agent_host,
    check_disk,
]


_ICONS = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}


def run_checks(console, settings: Settings | None = None) -> int:
    """跑全部检查，打印结果表，返回 fail 项数。"""
    s = settings or default_settings
    checks: list[Check] = []
    for check in ALL_CHECKS:
        try:
            checks.append(check(s))
        except Exception as exc:  # 单项检查本身出错不该让 doctor 崩掉
            checks.append(Check(check.__name__, "warn", f"检查异常：{exc}"))
    for c in checks:
        line = f"{_ICONS[c.status]} {c.name}"
        if c.detail:
            line += f"  [dim]{c.detail}[/dim]"
        console.print(line)
    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    if failures:
        console.print(f"\n[red]{failures} 项失败[/red]，{warnings} 项警告")
    elif warnings:
        console.print(f"\n[green]全部通过[/green]，{warnings} 项警告")
    else:
        console.print("\n[green]全部通过[/green]")
    return failures
