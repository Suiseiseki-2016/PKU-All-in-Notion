"""Owned real NTFS volume controller for the installed Windows candidate."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.probe_windows_disk_full_volume import wait_receipt, direct_win32_write

ROOT = Path(__file__).resolve().parents[1]


class WindowsDiskBoundary:
    def __init__(self, root, *, lifetime_seconds=600):
        if type(lifetime_seconds) is not int or not 30 <= lifetime_seconds <= 7200:
            raise ValueError('Disk fixture lifetime must be bounded between 30 and 7200 seconds')
        self.lifetime_seconds = lifetime_seconds
        self.root = Path(root).resolve()
        assert sys.platform == 'win32'
        assert self.root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert self.root.name.startswith('pku-native-disk-full-')
        assert not (self.root / 'disk-full.vhd').exists()
        self.mount = self.root / 'primary/data'
        assert not self.mount.exists() or not list(self.mount.iterdir())
        # Keep host profile/mount ancestors caller-owned before RunAs. The
        # elevated helper must not become their creator/ACL owner.
        self.mount.mkdir(parents=True, exist_ok=True)
        self.receipt = self.root / 'disk-full-receipt.json'
        self.control = self.root / 'disk-full-control.json'
        self.helper = self.root / 'windows_disk_full.ps1'
        self.helper.write_bytes((ROOT / 'product_e2e/windows_disk_full.ps1').read_bytes())
        self.helper_sha256 = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        for name in ('disk-full-receipt.json', 'setup.diskpart.txt', 'setup.diskpart.log',
                     'cleanup.diskpart.txt', 'cleanup.diskpart.log'):
            (self.root / name).write_text('{}' if name.endswith('.json') else '', 'utf-8')
        self.sequence = 0
        self.pid = None
        self.ready = None
        self.cleaned = None
        self.observations = []

    def alive(self):
        if self.pid is None:
            return False
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, self.pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)

    def start(self):
        assert self.pid is None and not (self.root / 'disk-full.vhd').exists()
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        occupied = kernel.GetLogicalDrives()
        assert occupied, 'Caller drive inventory must be available'
        letter = next((chr(i + 65) for i in range(25, 14, -1)
                       if not occupied & (1 << i)), None)
        assert letter, 'No free private test drive letter'
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        arguments = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.helper),
            '-TestRoot', str(self.root), '-LifetimeSeconds', str(self.lifetime_seconds), '-DriveLetter', letter]
        assert all('"' not in part for part in arguments)
        values = ','.join(quote('"' + part + '"') for part in arguments)
        command = ("$ErrorActionPreference='Stop'; $pkuVolumeProcess=Start-Process "
            "-FilePath powershell.exe -Verb RunAs -WindowStyle Hidden -PassThru -ArgumentList @(" +
            values + "); $pkuVolumeProcess.Id")
        launched = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
            check=True, timeout=180, capture_output=True, text=True)
        assert launched.stdout.strip().isdigit()
        self.pid = int(launched.stdout.strip())
        self.ready = wait_receipt(self.receipt, lambda v: v.get('attached') is True)
        assert self.alive() and self.ready['volume_size_bytes'] <= 64 * 1024 * 1024
        assert Path(self.ready['mount']) == self.mount and self.ready['active'] is False
        assert self.ready['native_data_dir'] == letter + ':\\'
        # Verify the ordinary drive alias resolves to the same exact volume.
        buffer = ctypes.create_unicode_buffer(1024)
        assert kernel.GetVolumeNameForVolumeMountPointW(self.ready['native_data_dir'], buffer, 1024)
        assert buffer.value.lower() == self.ready['volume_unique_id'].lower()
        return self.ready

    def signal(self, operation):
        assert operation in {'fill', 'release', 'stop'}
        self.sequence += 1
        pending = self.root / 'disk-full-control.tmp'
        pending.write_text(json.dumps({'operation': operation, 'sequence': self.sequence}), 'utf-8')
        os.replace(pending, self.control)
        return self.sequence

    def bind(self, run):
        assert self.ready is not None and self.alive()
        assert Path(run.config['test_root']).resolve() == self.root
        assert Path(run.config['instances']['primary']['app_dir']).resolve() == self.root / 'primary'
        assert run.context['target'] == 'windows' and run.mode == 'fault'
        assert hashlib.sha256(self.helper.read_bytes()).hexdigest() == self.helper_sha256

    @contextmanager
    def inject(self, run):
        self.bind(run)
        # These files belong exclusively to this real fault controller. They
        # never replace student/course files or contain private source data.
        probe = self.mount / 'independent-real-filesystem-write.bin'
        filler = self.mount / 'owned-disk-full-filler.bin'
        assert not probe.exists() and not filler.exists()
        probe.write_bytes(b'm')
        filler.write_bytes(b'')
        observation = {'variant': 'disk-full', 'boundary': 'actual-owned-ntfs-volume',
            'observed': False, 'helper_pid': self.pid, 'helper_sha256': self.helper_sha256}
        self.observations.append(observation)
        try:
            sequence = self.signal('fill')
            active = wait_receipt(self.receipt, lambda v: v.get('active') is True and v.get('sequence') == sequence)
            assert self.alive() and active['attached'] is True
            assert active['actual_fill_winerror'] in (39, 112)
            assert active['actual_small_write_winerror'] in (39, 112)
            assert active['remaining_free_bytes'] == 0
            failed = direct_win32_write(probe, active['volume_unique_id'])
            assert failed['success'] is False and failed['winerror'] in (39, 112)
            assert failed['written_bytes'] == 0 and failed['same_volume_verified'] is True
            observation.update(observed=True, active_receipt=active, independent_win32_failure=failed)
            run.actions.append({'instance': 'primary', 'operation': 'actual-ntfs-volume-full',
                'actual_disk_full': observation})
            yield observation
            current = wait_receipt(self.receipt, lambda v: v.get('sequence') == sequence)
            assert self.alive() and current['active'] is True, 'Fault must remain active throughout native action'
        finally:
            if self.pid is not None:
                sequence = self.signal('release')
                restored = wait_receipt(self.receipt,
                    lambda v: v.get('sequence') == sequence and v.get('active') is False)
                assert restored['attached'] is True and restored['released_free_bytes'] > 65536
                observation['released_receipt'] = restored
                assert probe.read_bytes() == b'm'
                successful = direct_win32_write(probe, restored['volume_unique_id'])
                assert successful['success'] is True and successful['written_bytes'] == 65536
                assert probe.read_bytes() == b'm' + b'p' * 65536
                observation['same_win32_write_after_release'] = successful

    def close(self):
        if self.pid is None:
            return
        self.signal('stop')
        self.cleaned = wait_receipt(self.receipt, lambda v: v.get('cleanup_verified') is True)
        assert self.cleaned['attached'] is False and self.cleaned['active'] is False
        return self.cleaned

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(exist_ok=False)
        rows = []
        for path in sorted(self.root.iterdir()):
            if path.is_file() and path.suffix in {'.json', '.txt', '.log', '.ps1'}:
                target = directory / path.name
                target.write_bytes(path.read_bytes())
                rows.append({'path': target.name,
                    'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        return rows
