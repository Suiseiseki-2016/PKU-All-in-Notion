"""Actual bounded Windows volume fault smoke; never a native E2E verdict."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def wait_receipt(path, predicate, timeout=45):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            value = json.loads(path.read_text('utf-8-sig'))
        except (OSError, ValueError):
            value = {}
        if predicate(value):
            return value
        time.sleep(.2)
    raise AssertionError('Real isolated disk receipt not observed within bound')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def direct_win32_write(path, expected_volume):
    """Use the real handle/API: CRT errno alone cannot supply Win32 evidence."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_path = kernel.GetFinalPathNameByHandleW
    get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_path.restype = wintypes.DWORD
    write_file = kernel.WriteFile
    write_file.argtypes = (wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    write_file.restype = wintypes.BOOL
    with path.open('ab', buffering=0) as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        name = ctypes.create_unicode_buffer(8192)
        length = get_path(handle, name, len(name), 1)
        assert 0 < length < len(name) and name.value.casefold().startswith(expected_volume.casefold())
        payload = ctypes.create_string_buffer(b'p' * 65536)
        written = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        success = bool(write_file(handle, payload, 65536, ctypes.byref(written), None))
        winerror = ctypes.get_last_error() if not success else 0
        if success:
            os.fsync(stream.fileno())
        return {'same_volume_verified': True, 'success': success,
            'winerror': winerror, 'written_bytes': written.value, 'requested_bytes': 65536}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='pku-native-disk-full-'))
    source = ROOT / 'product_e2e/windows_disk_full.ps1'
    helper = private / 'windows_disk_full.ps1'
    helper.write_bytes(source.read_bytes())
    assert digest(helper) == digest(source)
    receipt = private / 'disk-full-receipt.json'
    control = private / 'disk-full-control.json'
    volume = private / 'primary/data'
    # Preserve the caller's ACL on every elevated output. A new file created
    # under TEMP by RunAs can be unreadable to the unelevated E2E observer.
    for name in ('disk-full-receipt.json', 'setup.diskpart.txt', 'setup.diskpart.log',
                 'cleanup.diskpart.txt', 'cleanup.diskpart.log'):
        (private / name).write_text('{}' if name.endswith('.json') else '', 'utf-8')
    result = {'kind': 'actual-windows-isolated-volume-fault-not-native-e2e',
        'passed': False, 'native_e2e_passed': False, 'full_release_gate_passed': False,
        'private_profile': str(private), 'maximum_image_mib': 64,
        'helper_sha256': digest(helper), 'fault_active_receipt': None,
        'cleanup_receipt': None, 'uac_requested': True}
    sequence = 0

    def signal(operation):
        nonlocal sequence
        sequence += 1
        pending = private / 'disk-full-control.tmp'
        pending.write_text(json.dumps({'operation': operation, 'sequence': sequence}), 'utf-8')
        os.replace(pending, control)
        return sequence

    # Windows itself presents UAC. No UI automation of security dialogs.
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    child = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper),
        '-TestRoot', str(private), '-LifetimeSeconds', '180']
    assert all('"' not in part for part in child)
    values = ','.join(quote('"' + part + '"') for part in child)
    launch = ("$ErrorActionPreference='Stop'; Start-Process -FilePath powershell.exe "
        "-Verb RunAs -WindowStyle Hidden -ArgumentList @(" + values + ") | Out-Null")
    attempted_launch = False
    try:
        attempted_launch = True
        subprocess.run(['powershell.exe', '-NoProfile', '-Command', launch],
            check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready = wait_receipt(receipt, lambda v: v.get('attached') is True)
        assert ready['volume_size_bytes'] <= 64 * 1024 * 1024
        original = volume / 'preserved-isolated-smoke-fixture.md'
        original.write_bytes(b'Isolated volume smoke fixture.\n' * 4096)
        original_hash = digest(original)
        probe = volume / 'independent-real-filesystem-write.bin'
        probe.write_bytes(b'm')
        # No writer is active before the first fill signal. Keep the file
        # caller-owned and reject any unexpectedly preexisting filler.
        filler = volume / 'owned-disk-full-filler.bin'
        result['volume_file_names_before_fill'] = sorted(p.name for p in volume.iterdir())
        assert not filler.exists(), 'Unexpected filler already exists before activation'
        with filler.open('wb'):
            pass
        assert filler.is_file() and filler.stat().st_size == 0
        seq = signal('fill')
        active = wait_receipt(receipt, lambda v: v.get('active') is True and v.get('sequence') == seq)
        assert active['actual_fill_winerror'] in (39, 112)
        assert active['actual_small_write_winerror'] in (39, 112)
        result['fault_active_receipt'] = active
        error = None
        try:
            with probe.open('ab', buffering=0) as stream:
                import ctypes
                from ctypes import wintypes
                import msvcrt
                get_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
                get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
                get_path.restype = wintypes.DWORD
                buffer = ctypes.create_unicode_buffer(8192)
                length = get_path(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 1)
                assert 0 < length < len(buffer)
                assert buffer.value.casefold().startswith(active['volume_unique_id'].casefold())
                result['independent_handle_same_volume_verified'] = True
                stream.write(b'p' * 65536)
                os.fsync(stream.fileno())
        except OSError as exc:
            error = {'type': type(exc).__name__, 'winerror': exc.winerror, 'errno': exc.errno}
        result['independent_write_failure'] = error
        assert error and error['errno'] == errno.ENOSPC
        native_error = direct_win32_write(probe, active['volume_unique_id'])
        result['independent_win32_write_failure'] = native_error
        assert native_error['success'] is False and native_error['winerror'] in (39, 112)
        assert native_error['written_bytes'] == 0
        assert digest(original) == original_hash
        seq = signal('release')
        restored = wait_receipt(receipt, lambda v: v.get('sequence') == seq and v.get('active') is False)
        assert restored['released_free_bytes'] > 65536
        assert probe.read_bytes() == b'm'
        native_restored = direct_win32_write(probe, active['volume_unique_id'])
        result['same_win32_write_after_release'] = native_restored
        assert native_restored['success'] is True and native_restored['written_bytes'] == 65536
        assert probe.read_bytes() == b'm' + b'p' * 65536 and digest(original) == original_hash
        result.update(preserved_fixture_sha256=original_hash,
            isolated_fixture_preserved=True, same_write_succeeds_after_release=True)
        signal('stop')
        final = wait_receipt(receipt, lambda v: v.get('cleanup_verified') is True)
        assert final['attached'] is False and final['active'] is False
        result['cleanup_receipt'] = final
        result['passed'] = True
    except Exception as exc:
        import traceback
        result['failure_type'] = type(exc).__name__
        result['exception_locations'] = [f'{Path(r.filename).name}:{r.lineno}'
            for r in traceback.extract_tb(exc.__traceback__)]
    finally:
        if attempted_launch and result['cleanup_receipt'] is None:
            signal('stop')
            try:
                result['cleanup_receipt'] = wait_receipt(receipt,
                    lambda v: v.get('cleanup_verified') is True, timeout=30)
            except Exception as exc:
                result['cleanup_unverified'] = type(exc).__name__
        result['evidence'] = []
        # No secrets or media enter this smoke. Preserve exact receipts and
        # DiskPart logs independently of a successful outcome.
        for path in sorted(private.iterdir()):
            if path.is_file() and path.suffix in {'.json', '.txt', '.log', '.ps1'}:
                copied = args.output_dir / path.name
                captured = False
                for attempt in range(10):
                    try:
                        copied.write_bytes(path.read_bytes())
                        result['evidence'].append({'path': copied.name, 'sha256': digest(copied)})
                        captured = True
                        break
                    except (PermissionError, OSError):
                        time.sleep(.2)
                if not captured:
                    result.setdefault('evidence_capture_failures', []).append(path.name)
                    result['passed'] = False
        (args.output_dir / 'volume-fault-proof.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps({'actual_volume_smoke_passed': result['passed'],
        'native_e2e_passed': False, 'cleanup_verified': bool(result['cleanup_receipt']),
        'failure_type': result.get('failure_type')}, ensure_ascii=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
