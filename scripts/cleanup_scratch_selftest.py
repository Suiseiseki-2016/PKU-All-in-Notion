from __future__ import annotations
import argparse

import json

import os

import shutil

import stat

from datetime import datetime, timezone

from pathlib import Path

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def _check(checks: list, name: str, ok: bool, detail: str='') -> None:
    checks.append({'name': name, 'result': 'pass' if ok else 'fail', 'detail': detail})

SELFTEST_CAPTURE_REL = os.path.normpath('client-data/panel/organize_failures/quiz-parse-selftest-1.txt')

def _unlock_readonly(root: Path) -> list[str]:
    """Clear the Windows read-only attribute on every file under `root`.

    The organizer persists parse-failure captures
    (panel/organize_failures/quiz-parse-*.txt) with chmod 0444 (see
    persist_parse_failure), which on Windows sets FILE_ATTRIBUTE_READONLY and
    makes shutil.rmtree refuse to delete the file (PermissionError/WinError 5).
    The whole root is disposable scratch at teardown, so the attribute is
    cleared before rmtree. This is NOT an ignore_errors workaround: rmtree
    still runs with ignore_errors=False, so genuinely unrelated deletion
    errors (live handles, denied directories) remain fail-loud. Returns the
    relative names of every file whose read-only flag was cleared (honest
    proof for the evidence record). No-op on POSIX where file mode never
    blocks unlink or directory removal.
    """
    if os.name != 'nt':
        return []
    cleared: list[str] = []
    if not root.exists():
        return cleared
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if mode & stat.S_IWRITE:
            continue
        try:
            os.chmod(path, stat.S_IWRITE)
            cleared.append(str(path.relative_to(root)))
        except OSError:
            continue
    return cleared

def _remove_scratch_root(root: Path) -> dict:
    """Delete a disposable scratch root and return honest removal proof.

    The proof records the pre-removal file inventory, which read-only files
    had their attribute cleared first (the organizer's 0444 capture files on
    Windows), and whether the root actually exists afterward
    (`root_removed`). rmtree keeps ignore_errors=False, so any unrelated
    deletion error raises instead of being swept under the ignore_errors rug.
    """
    inventory = sorted(({'name': str(p.relative_to(root)), 'bytes': p.stat().st_size} for p in root.rglob('*') if p.is_file()), key=lambda entry: entry['name'])
    cleared_readonly = _unlock_readonly(root)
    shutil.rmtree(root, ignore_errors=False)
    return {'removed_entry_count': len(inventory), 'removed_entry_names': [entry['name'] for entry in inventory], 'cleared_readonly_files': cleared_readonly, 'root_removed': not root.exists()}

def _probe_unrelated_error_fails_loud() -> dict:
    """Windows-only probe: an unrelated deletion blocker stays fail-loud.

    Holds one file open with ZERO sharing (CreateFileW dwShareMode=0), which
    is not the read-only attribute and cannot be fixed by the unlock pass;
    rmtree(...) must still raise PermissionError. Proves the hardening is a
    targeted read-only unlock, not an ignore_errors-style sweep.
    """
    import ctypes
    import tempfile
    from ctypes import wintypes
    root = Path(tempfile.mkdtemp(prefix='e2e-window-c-fail-loud-'))
    paired = root / 'client-data' / 'panel' / 'organize_failures'
    paired.mkdir(parents=True, exist_ok=True)
    locked = paired / 'locked.txt'
    locked.write_text('locked-by-selftest', encoding='utf-8')
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    outcome = {'result': 'setup-failed', 'detail': 'CreateFileW could not open the lock file', 'raised_permission_error': False}
    handle = None
    try:
        handle = kernel32.CreateFileW(str(locked), 2147483648, 0, None, 3, 128, None)
        invalid = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid:
            return outcome
        _unlock_readonly(root)
        try:
            shutil.rmtree(root, ignore_errors=False)
        except PermissionError:
            outcome = {'result': 'fail-loud', 'detail': 'rmtree re-raised the unrelated deletion error after the read-only unlock', 'raised_permission_error': True}
        else:
            outcome = {'result': 'fail-loud-violated', 'detail': 'rmtree hid a live exclusive handle (ignore_errors-like sweep)', 'raised_permission_error': False}
    finally:
        if handle is not None and handle != ctypes.c_void_p(-1).value:
            kernel32.CloseHandle(handle)
        if root.exists():
            _unlock_readonly(root)
            shutil.rmtree(root, ignore_errors=False)
    return outcome

def cmd_cleanup_selftest(args) -> int:
    """Local deterministic check of the read-only scratch-cleanup hardening.

    Builds a scratch-shaped tree with a chmod-0444
    client-data/panel/organize_failures/quiz-parse-*.txt capture (the exact
    shape persist_parse_failure produces after a parse failure), runs the same
    unlock + rmtree(ignore_errors=False) teardown path the window uses, and
    proves an honest root_removed=true. On Windows it additionally proves an
    unrelated deletion blocker (exclusively-opened file) still fails loud.
    No services, credentials, Notion calls, or spend."""
    import tempfile
    host_root = Path(tempfile.mkdtemp(prefix='e2e-window-c-cleanup-selftest-'))
    checks: list[dict] = []
    removal: dict = {}
    fixture_readonly = False
    try:
        root = host_root / 'scratch'
        capture = root / 'client-data' / 'panel' / 'organize_failures' / 'quiz-parse-selftest-1.txt'
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text('{"text": "bounded quiz parse failure capture (local selftest fixture)"}\n', encoding='utf-8')
        os.chmod(capture, 292)
        fixture_readonly = not capture.stat().st_mode & stat.S_IWRITE
        removal = _remove_scratch_root(root)
        _check(checks, 'the capture fixture was read-only before the teardown', fixture_readonly)
        _check(checks, 'the read-only capture was unlocked (Windows) or unlinked directly (POSIX)', os.name == 'nt' and SELFTEST_CAPTURE_REL in removal['cleared_readonly_files'] or (os.name != 'nt' and removal['cleared_readonly_files'] == []), f"cleared={removal['cleared_readonly_files']}")
        _check(checks, 'the scratch-shaped root was removed with root_removed=true', removal['root_removed'], f"root_removed={removal['root_removed']}")
        if os.name == 'nt':
            fail_loud = _probe_unrelated_error_fails_loud()
            removal['unrelated_error_fail_loud'] = fail_loud
            _check(checks, 'an unrelated deletion error still fails loud (no blanket sweep)', fail_loud['raised_permission_error'] is True, f"detail={fail_loud['detail']} result={fail_loud['result']}")
    finally:
        if host_root.exists():
            try:
                _unlock_readonly(host_root)
                shutil.rmtree(host_root, ignore_errors=False)
            except OSError:
                pass
    evidence = {'step': 'cleanup-selftest', 'generated_at': _utcnow(), 'mode': 'local-deterministic', 'fixture': {'capture_path': SELFTEST_CAPTURE_REL, 'readonly_mode': fixture_readonly}, 'removal': removal, 'checks': checks, 'note': 'local deterministic functional check of the Windows read-only scratch-cleanup hardening; no services, credentials, Notion calls, or spend'}
    _write_json(args.out, evidence)
    passed = all((check['result'] == 'pass' for check in checks))
    print(f"cleanup-selftest: root_removed={removal.get('root_removed')} capture_readonly={fixture_readonly} unlocked={removal.get('cleared_readonly_files')} pass={sum((1 for c in checks if c['result'] == 'pass'))}/{len(checks)}")
    for check in checks:
        marker = 'PASS' if check['result'] == 'pass' else 'FAIL'
        print(f"  [{marker}] {check['name']}" + (f" -- {check['detail']}" if check['detail'] else ''))
    return 0 if passed else 1

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Local scratch-cleanup safety selftest')
    parser.add_argument('action', choices=['cleanup-selftest'])
    parser.add_argument('--out', type=Path, required=True)
    raise SystemExit(cmd_cleanup_selftest(parser.parse_args()))
