"""Run the local panel from this source tree using the installed app's config.

Why this exists
---------------
``Settings`` loads ``.env`` from the working directory *and* from the package
root, and pydantic-settings lets the package-root file win. In this repo the
package root sits next to a developer ``.env`` that points ``DATA_DIR`` at
``E:/pku-course-data`` and carries its own ``NOTION_TOKEN``, so a plain
``python -m pku_sync.cli panel`` from the repo silently opens the wrong data
directory instead of the student's real one.

This launcher reads the installed app's ``.env`` into the process environment
first, pins ``DATA_DIR`` to that app's ``data`` folder, and starts the panel
from there. The result is a source run that behaves exactly like the installed
app while using the current code.

Usage
-----
    python scripts/run_panel_app_data.py [--app-dir DIR] [--port N] [--no-browser]

``--app-dir`` defaults to ``%USERPROFILE%\\PKU-All-in-Notion`` (or
``~/PKU-All-in-Notion``). Arguments after ``--app-dir`` are passed through to
``pku-sync panel``; the panel itself only accepts ports 8791, 8792 and 8793.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader; the app's .env has no interpolation needs."""
    values: dict[str, str] = {}
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def default_app_dir() -> Path:
    return Path.home() / "PKU-All-in-Notion"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-dir", type=Path, default=default_app_dir(),
                        help="installed app directory holding .env and data/")
    args, passthrough = parser.parse_known_args()

    app_dir: Path = args.app_dir.expanduser().resolve()
    env_path = app_dir / ".env"
    if not env_path.is_file():
        print(f"找不到应用配置：{env_path}", file=sys.stderr)
        return 2

    env = read_env(env_path)
    os.environ.update(env)
    data_dir = app_dir / "data"
    os.environ["DATA_DIR"] = str(data_dir)
    os.chdir(app_dir)
    sys.path.insert(0, str(REPO_ROOT))

    print(f"应用目录：{app_dir}")
    print(f"数据目录：{data_dir}")
    print(f"Notion 授权：{'已配置' if env.get('NOTION_TOKEN') else '缺失'}")
    print(f"教学网账号：{'已配置' if env.get('PKU_USERNAME') else '缺失'}")
    print("提示：安装版应用请先关闭，面板只使用 8791、8792、8793。")

    from pku_sync.cli import app  # imported after the env is in place

    sys.argv = ["pku-sync", "panel", *passthrough]
    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
