"""Installer safety checks; not native installation acceptance."""
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest
from product_e2e import windows_install
from product_e2e.core import ResourceBlocked


@pytest.mark.parametrize('image', ['pku-desktop.exe', 'PKU-DESKTOP.EXE'])
def test_isolated_install_refuses_live_native_window_before_registry_or_installer(monkeypatch, image):
    root = Path(tempfile.gettempdir()) / 'pku-installer-safety-fixture'
    monkeypatch.setattr(windows_install, 'process_inventory', lambda: [{'name': image, 'pid': 123}])
    calls = []
    monkeypatch.setattr(windows_install, 'winreg', SimpleNamespace(OpenKey=lambda *a: calls.append('registry')))
    monkeypatch.setattr(windows_install.subprocess, 'run', lambda *a, **k: calls.append('installer'))
    with pytest.raises(ResourceBlocked, match='close_existing_native_window'):
        windows_install.install_isolated(root / 'candidate.exe', root / 'installed/pku-desktop.exe', root)
    assert calls == []


def test_isolation_uses_actual_nsis_manufacturer_key():
    assert windows_install.ISOLATED_REGISTRY_KEYS == (
        r'Software\Microsoft\Windows\CurrentVersion\Uninstall\PKU All in Notion',
        r'Software\aeoluswu\PKU All in Notion')
