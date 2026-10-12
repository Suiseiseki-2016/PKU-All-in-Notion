"""Allow `python -m pku_sync` (desktop sidecar / portable runtime)."""

from __future__ import annotations

from pku_sync.desktop_lifecycle import guard_windows_desktop_parent

guard_windows_desktop_parent()

from pku_sync.cli import app

if __name__ == "__main__":
    app()
