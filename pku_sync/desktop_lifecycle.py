"""Keep the Windows desktop's backend tree tied to its actual shell lifetime.

The backend owns a non-inheritable kill-on-close Job Object before it can
start any media workers. A handle to the original native parent, rather than
PID polling, detects both normal exit and a crash without PID reuse races.
CLI processes without the desktop launch identity keep their normal lifetime.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import sys
import threading

_HANDLES = []  # OS closes these on process exit; no early atexit self-kill.


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('process_count', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('read_count', 'write_count', 'other_count', 'read_bytes', 'write_bytes', 'other_bytes')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', _IOCounters),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


def _identity(parent, instance):
    if not isinstance(parent, str) or not re.fullmatch('[1-9][0-9]{0,9}', parent):
        raise RuntimeError('原生后台父进程身份无效，服务尚未启动。')
    pid = int(parent)
    if pid > 0xffffffff or pid == os.getpid() or not isinstance(instance, str):
        raise RuntimeError('原生后台父进程身份无效，服务尚未启动。')
    match = re.fullmatch(re.escape(parent) + '-([1-9][0-9]{0,19})', instance)
    if not match:
        raise RuntimeError('原生后台启动身份不匹配，服务尚未启动。')
    return pid, int(match.group(1))


def _kernel32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'GetCurrentProcess': ([], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        'ProcessIdToSessionId': ([wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def guard_windows_desktop_parent():
    if sys.platform != 'win32' or os.environ.get('PKU_DESKTOP_APP') != '1' or not os.environ.get('PKU_DESKTOP_PARENT_PID'):
        return False
    if _HANDLES:
        return True
    pid, launch_ns = _identity(os.environ['PKU_DESKTOP_PARENT_PID'], os.environ.get('PKU_PANEL_INSTANCE_ID'))
    api = _kernel32()
    # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, no inherited handle.
    parent = api.OpenProcess(0x00101000, False, pid)
    if not parent:
        raise ctypes.WinError(ctypes.get_last_error())
    job = None
    try:
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        if not api.QueryFullProcessImageNameW(parent, 0, image, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        if Path(image.value).name.lower() != 'pku-desktop.exe':
            raise RuntimeError('原生后台父进程不是桌面窗口，服务尚未启动。')
        sessions = [wintypes.DWORD(), wintypes.DWORD()]
        if not api.ProcessIdToSessionId(pid, ctypes.byref(sessions[0])) or not api.ProcessIdToSessionId(os.getpid(), ctypes.byref(sessions[1])):
            raise ctypes.WinError(ctypes.get_last_error())
        if sessions[0].value != sessions[1].value:
            raise RuntimeError('原生后台与窗口会话不同，服务尚未启动。')
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(parent, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        created_ns = (created - 116444736000000000) * 100
        if created_ns > launch_ns:
            raise RuntimeError('原生窗口身份已被替换，服务尚未启动。')
        job = api.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        # No job handle is inherited by a media worker. On parent exit the
        # backend exits, closing the last handle and killing its worker tree.
        _HANDLES.extend([parent, job])
        def watch_parent():
            result = api.WaitForSingleObject(parent, 0xffffffff)
            os._exit(0 if result == 0 else 1)
        threading.Thread(target=watch_parent, daemon=True, name='desktop-parent-lifetime').start()
        return True
    except BaseException:
        if job:
            api.CloseHandle(job)
        api.CloseHandle(parent)
        raise
