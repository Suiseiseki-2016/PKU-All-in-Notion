"""VAL-PKG-006: OS-agnostic code guarantee for the ``pku_sync`` package.

Core code remains platform-neutral. Two explicit Windows integration
boundaries may detect Windows before accessing its native services: desktop
process lifetime and the user's enabled local proxy. Their early-return
behavior is tested on non-Windows platforms. Native imports at module scope,
other OS branches and GUI toolkit imports remain forbidden.

The scan is deliberately AST-based rather than raw text, so prose in
docstrings and comments is ignored: product *code* must be free of these
patterns while documentation about them is fine. It is self-checking: the
scanner must flag a planted offender, so a vacuous scan can never pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKU_SYNC_DIR = Path(__file__).resolve().parent.parent / "pku_sync"

# Windows-only or native-GUI module roots that the product package must never
# import (the root of the module name decides: ``import wx.something`` counts).
FORBIDDEN_IMPORT_ROOTS = {
    "winreg",
    "msvcrt",
    "tkinter",
    "PyQt4",
    "PyQt5",
    "PyQt6",
    "PySide",
    "PySide2",
    "PySide6",
    "wx",
}

# ``module.attribute`` accesses that make code platform-conditional. The
# stdlib modules themselves are cross-platform and may be imported; the
# flagged attributes are the OS-detection / Windows-only surface.
FORBIDDEN_ATTRIBUTES = {
    ("sys", "platform"),
    ("sys", "getwindowsversion"),
    ("os", "name"),
    ("os", "startfile"),
    ("platform", "platform"),
    ("platform", "system"),
    ("platform", "machine"),
    ("platform", "uname"),
    ("platform", "release"),
    ("platform", "version"),
    ("platform", "node"),
    ("ctypes", "windll"),
    ("ctypes", "oledll"),
}

# ``from module import name`` equivalents of the same forbidden surface.
FORBIDDEN_FROM_IMPORTS = FORBIDDEN_ATTRIBUTES


class _PlatformScan(ast.NodeVisitor):
    """Collects (lineno, message) pairs for platform-conditional code."""

    def __init__(self, source_path, boundaries=None):
        self.source_path = source_path
        self.violations: list[tuple[int, str]] = []
        self.boundaries = boundaries or {}
        self.function = None

    def visit_FunctionDef(self, node):
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    def allowed(self, surface):
        return surface in self.boundaries.get(self.function, set())

    def visit_Import(self, node):
        for alias in node.names:
            root = (alias.name or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS and not self.allowed((root, "import")):
                self.violations.append(
                    (node.lineno, f"platform/GUI import: import {alias.name}")
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            root = node.module.split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS and not self.allowed((root, "import")):
                self.violations.append(
                    (node.lineno, f"platform/GUI import: from {node.module} import ...")
                )
            for alias in node.names:
                if (node.module, alias.name) in FORBIDDEN_FROM_IMPORTS:
                    self.violations.append(
                        (
                            node.lineno,
                            f"platform-conditional import: "
                            f"from {node.module} import {alias.name}",
                        )
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and (
            node.value.id,
            node.attr,
        ) in FORBIDDEN_ATTRIBUTES and not self.allowed((node.value.id, node.attr)):
            self.violations.append(
                (node.lineno, f"platform-conditional access: {node.value.id}.{node.attr}")
            )
        self.generic_visit(node)


WINDOWS_BOUNDARIES = {
    "network.py": {"campus_identity_fallback_proxy": {("os", "name"), ("winreg", "import")}},
    "desktop_lifecycle.py": {"guard_windows_desktop_parent": {("sys", "platform")}},
}


def scan_package(root: Path = PKU_SYNC_DIR, *, boundaries=None) -> list[tuple[str, int, str]]:
    """Scan every ``*.py`` under ``root``; return (rel_path, lineno, message)."""
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        visitor = _PlatformScan(py_file, (boundaries or {}).get(py_file.relative_to(root).as_posix()))
        visitor.visit(tree)
        for lineno, message in visitor.violations:
            violations.append((str(py_file.relative_to(root)), lineno, message))
    return violations


def test_core_has_no_platform_branches_outside_explicit_windows_boundaries():
    violations = scan_package(boundaries=WINDOWS_BOUNDARIES)
    assert not violations, (
        "OS-conditional or native-GUI code found in pku_sync:\n"
        + "\n".join(f"  {path}:{lineno}: {message}" for path, lineno, message in violations)
    )


def test_scan_itself_detects_platform_conditional_and_gui_code(tmp_path):
    """The scanner is not vacuous: every forbidden pattern is caught."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "bad.py").write_text(
        "import sys\n"
        "import os\n"
        "import ctypes\n"
        "import winreg\n"
        "import tkinter\n"
        "from os import name\n"
        "def blame():\n"
        "    if sys.platform == 'win32':\n"
        "        return 'sys.platform'\n"
        "    if os.name == 'nt':\n"
        "        return 'os.name'\n"
        "    ctypes.windll.user32\n"
        "    if sys.getwindowsversion():\n"
        "        pass\n"
        "    os.startfile('x')\n",
        encoding="utf-8",
    )
    messages = [message for _path, _lineno, message in scan_package(planted)]
    text = "\n".join(messages)
    for needle in (
        "sys.platform",
        "sys.getwindowsversion",
        "os.name",
        "os.startfile",
        "ctypes.windll",
        "winreg",
        "tkinter",
        "from os import name",
    ):
        assert needle in text, f"scanner missed planted pattern: {needle}"


def test_named_boundary_does_not_allow_module_scope_or_unrelated_function_imports(tmp_path):
    (tmp_path / "network.py").write_text(
        "import winreg\ndef unrelated():\n    import winreg\n", "utf-8")
    violations = scan_package(tmp_path, boundaries=WINDOWS_BOUNDARIES)
    assert len(violations) == 2


def test_non_windows_boundaries_return_before_any_native_access(monkeypatch):
    import sys
    from types import SimpleNamespace
    from pku_sync import desktop_lifecycle, network

    monkeypatch.setattr(desktop_lifecycle, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(desktop_lifecycle, "os", SimpleNamespace(environ={
        "PKU_DESKTOP_APP": "1", "PKU_DESKTOP_PARENT_PID": "123"}))
    monkeypatch.setattr(desktop_lifecycle, "_kernel32", lambda: (_ for _ in ()).throw(
        AssertionError("Windows API on non-Windows")))
    monkeypatch.setattr(network, "os", SimpleNamespace(name="posix", environ={}))
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert desktop_lifecycle.guard_windows_desktop_parent() is False
    assert network.campus_identity_fallback_proxy() is None
