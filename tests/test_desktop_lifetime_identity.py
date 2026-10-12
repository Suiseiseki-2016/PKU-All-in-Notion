"""Desktop binding rejects malformed or unrelated parents before any job starts."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pku_sync.desktop_lifecycle import _identity


@pytest.mark.parametrize('parent,instance', [
    ('0', '0-123'), ('-2', '-2-123'), ('4294967296', '4294967296-123'),
    ('12', '13-123'), ('12', '12-0'), ('12', None),
    (str(os.getpid()), str(os.getpid()) + '-123'),
])
def test_invalid_desktop_launch_identity_cannot_bind_current_or_other_process(parent, instance):
    with pytest.raises(RuntimeError):
        _identity(parent, instance)


def test_actual_python_parent_is_rejected_before_backend_service_start():
    env = dict(os.environ, PYTHONUTF8='1', PKU_DESKTOP_APP='1', PKU_DESKTOP_PARENT_PID=str(os.getpid()),
               PKU_PANEL_INSTANCE_ID=str(os.getpid()) + '-1791280000000000000')
    result = subprocess.run([sys.executable, '-c',
        'from pku_sync.desktop_lifecycle import guard_windows_desktop_parent; guard_windows_desktop_parent()'],
        env=env, cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
    if sys.platform == 'win32':
        assert result.returncode != 0
        assert '父进程不是桌面窗口' in result.stderr.decode('utf-8')
    else:
        assert result.returncode == 0 and not result.stderr
