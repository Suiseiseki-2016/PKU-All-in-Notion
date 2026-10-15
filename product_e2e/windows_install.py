"""Install a candidate in TEMP while preserving the user's product registry."""
from pathlib import Path
import subprocess
import tempfile
try:
    import winreg
except ImportError:
    winreg = None

from .core import ResourceBlocked, installed_version, process_inventory, sha256

ISOLATED_REGISTRY_KEYS = (
    r'Software\Microsoft\Windows\CurrentVersion\Uninstall\PKU All in Notion',
    r'Software\aeoluswu\PKU All in Notion')


def install_isolated(artifact, installed, test_root):
    if winreg is None:
        raise ResourceBlocked('actual_windows_registry_required_for_installation')
    artifact, installed, test_root = Path(artifact).resolve(), Path(installed).resolve(), Path(test_root).resolve()
    if (not test_root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or not installed.is_relative_to(test_root) or installed.name != 'pku-desktop.exe'):
        raise ResourceBlocked('candidate_installation_not_in_isolated_temp')
    # NSIS update can close an existing application by image name. Never let
    # an isolated test install terminate a student window or another test.
    if any(str(row.get('name', '')).lower() == 'pku-desktop.exe' for row in process_inventory()):
        raise ResourceBlocked('close_existing_native_window_before_isolated_install')
    keys = ISOLATED_REGISTRY_KEYS
    saved = {}
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                saved[key] = [winreg.EnumValue(handle, i) for i in range(winreg.QueryInfoKey(handle)[1])]
        except FileNotFoundError:
            saved[key] = None
    try:
        # NSIS reads MANUPRODUCTKEY even when /D is supplied, and appends its
        # saved directory to the previous uninstaller's _?= argument. Merely
        # restoring UNINSTKEY afterwards cannot isolate that earlier mutation.
        # Suppress both pointers only for this installer, restoring every value.
        for key, values in saved.items():
            if values is not None:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                    for name, _, _ in values:
                        winreg.DeleteValue(handle, name)
        result = subprocess.run([str(artifact), '/S', '/NS', '/UPDATE', '/D=' + str(installed.parent)], timeout=180)
        if result.returncode != 0 or not installed.is_file():
            raise ResourceBlocked('isolated_candidate_installation_failed')
        runtime = installed.parent / 'resources/runtime'
        if not all((runtime / name).is_file() for name in ('python.exe', 'python311.dll', '.bundle-ok')):
            raise ResourceBlocked('installed_candidate_bundled_runtime_missing')
    finally:
        for key, values in saved.items():
            if values is None:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                except FileNotFoundError:
                    pass
            else:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
                    names = [winreg.EnumValue(handle, i)[0] for i in range(winreg.QueryInfoKey(handle)[1])]
                    for name in names:
                        winreg.DeleteValue(handle, name)
                    for name, value, kind in values:
                        winreg.SetValueEx(handle, name, 0, kind, value)
    return {'artifact_sha256': sha256(artifact), 'installed_sha256': sha256(installed),
            'version': installed_version(installed), 'generated_by': 'os-installer',
            'student_product_registry_values_restored': True,
            'previous_install_pointers_suppressed_during_isolated_install': True,
            'actual_nsis_manufacturer_key_preserved': True,
            'installed_bundled_python_sha256': sha256(runtime / 'python.exe')}
