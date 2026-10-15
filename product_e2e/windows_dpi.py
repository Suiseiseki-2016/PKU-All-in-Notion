"""Read the owned native window and monitor DPI without changing OS settings."""
import sys


def read_window_dpi(pid):
    if sys.platform != 'win32' or type(pid) is not int or pid <= 0:
        raise ValueError('actual-windows-native-pid-required')
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    shcore = ctypes.WinDLL('shcore', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
    user32.GetWindowDpiAwarenessContext.restype = wintypes.HANDLE
    user32.GetAwarenessFromDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    shcore.GetScaleFactorForMonitor.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
    shcore.GetScaleFactorForMonitor.restype = ctypes.c_long
    handles = []

    def visit(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, title, len(title))
            if title.value == 'PKU All in Notion':
                handles.append(handle)
        return True

    if not user32.EnumWindows(callback_type(visit), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if len(handles) != 1:
        raise ValueError('owned-native-window-not-unique')
    handle = handles[0]
    monitor = user32.MonitorFromWindow(handle, 2)
    scale = ctypes.c_int()
    if not monitor or shcore.GetScaleFactorForMonitor(monitor, ctypes.byref(scale)) != 0:
        raise ValueError('actual-monitor-scale-read-failed')
    dpi = user32.GetDpiForWindow(handle)
    awareness = user32.GetAwarenessFromDpiAwarenessContext(user32.GetWindowDpiAwarenessContext(handle))
    if not dpi or scale.value <= 0 or awareness < 0:
        raise ValueError('actual-window-dpi-read-failed')
    return {'pid':pid, 'window_dpi':dpi, 'monitor_scale_percent':scale.value,
            'window_dpi_awareness':awareness,
            'source':'GetDpiForWindow/GetScaleFactorForMonitor/GetWindowDpiAwarenessContext'}


def matches_native_dpi(webview_dpi, actual, pid):
    if type(webview_dpi) not in {int, float} or not isinstance(actual, dict):
        return False
    if any(type(actual.get(key)) is not int for key in
           ('pid','window_dpi','monitor_scale_percent','window_dpi_awareness')):
        return False
    return (type(pid) is int and pid > 0 and actual['pid'] == pid
            and actual['window_dpi'] > 0 and actual['monitor_scale_percent'] > 0
            and actual['window_dpi_awareness'] == 2
            and actual.get('source') == 'GetDpiForWindow/GetScaleFactorForMonitor/GetWindowDpiAwarenessContext'
            and abs(actual['window_dpi'] - 96 * actual['monitor_scale_percent'] / 100) <= 0.000001
            and abs(webview_dpi - actual['window_dpi']) <= 0.000001)
