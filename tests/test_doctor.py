"""doctor 自检逻辑的单测：磁盘/CLI/文件探测全走注入点，不碰真实系统。"""


from __future__ import annotations


import io




from types import SimpleNamespace


from rich.console import Console


from pku_sync import doctor


from pku_sync.config import Settings


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        pku_username="u",
        pku_password="p",
        data_dir=tmp_path / "data",
        agent_host="claude",
    )
    base.update(overrides)
    return Settings(**base)


def test_env_file_missing_and_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 离开仓库，cwd 没有 .env
    monkeypatch.setattr(doctor, "project_root", lambda: tmp_path)
    s = _settings(tmp_path)
    assert doctor.check_env_file(s).status == "fail"
    (tmp_path / ".env").write_text("X=1", "utf-8")
    assert doctor.check_env_file(s).status == "ok"


def test_credentials_missing(tmp_path):
    s = _settings(tmp_path, pku_username="", pku_password="")
    check = doctor.check_credentials(s)
    assert check.status == "fail"
    assert "PKU_USERNAME" in check.detail


def test_agent_host_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"claude", "droid"} else None,
    )
    assert doctor.check_agent_host(_settings(tmp_path)).status == "ok"
    factory = doctor.check_agent_host(_settings(tmp_path, agent_host="factory"))
    assert factory.status == "ok" and "droid" in factory.detail
    assert doctor.check_agent_host(_settings(tmp_path, agent_host="codex")).status == "fail"


def test_disk_thresholds(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    gb = 1 << 30
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * gb))
    assert doctor.check_disk(s).status == "ok"
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: SimpleNamespace(free=3 * gb))
    assert doctor.check_disk(s).status == "warn"
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: SimpleNamespace(free=gb // 2))
    assert doctor.check_disk(s).status == "fail"


def test_run_checks_aggregates_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "project_root", lambda: tmp_path)  # .env 缺失 → fail
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # agent 宿主缺失 → fail
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: SimpleNamespace(free=100 << 30))
    s = _settings(tmp_path)
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    failures = doctor.run_checks(console, s)
    text = out.getvalue()
    assert failures == 2
    assert ".env 配置" in text and "Agent 宿主" in text
    assert "项失败" in text


def test_run_checks_survives_broken_check(tmp_path, monkeypatch):
    def broken(settings):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(doctor, "ALL_CHECKS", [broken])
    out = io.StringIO()
    failures = doctor.run_checks(Console(file=out, force_terminal=False), _settings(tmp_path))
    assert failures == 0  # 检查自身异常降级为 warn，不让 doctor 崩
    assert "检查异常" in out.getvalue()
