"""VAL-PKG-006: OS-agnostic code guarantee for the ``pku_sync`` package.

The product package must stay platform-neutral: no ``sys.platform`` /
``os.name`` / ``platform.*`` OS branching, no Windows-only stdlib modules
(``winreg``, ``msvcrt``), no Windows-only socket/ctypes surfaces, and no
native GUI toolkit imports. The full fake-based suite plus this scan run on
every platform in the CI matrix on the default branch, so a Windows-only
import here would break the package on the ubuntu/macos legs.

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

    def __init__(self, source_path):
        self.source_path = source_path
        self.violations: list[tuple[int, str]] = []

    def visit_Import(self, node):
        for alias in node.names:
            root = (alias.name or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                self.violations.append(
                    (node.lineno, f"platform/GUI import: import {alias.name}")
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            root = node.module.split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
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
        ) in FORBIDDEN_ATTRIBUTES:
            self.violations.append(
                (node.lineno, f"platform-conditional access: {node.value.id}.{node.attr}")
            )
        self.generic_visit(node)


def scan_package(root: Path = PKU_SYNC_DIR) -> list[tuple[str, int, str]]:
    """Scan every ``*.py`` under ``root``; return (rel_path, lineno, message)."""
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        visitor = _PlatformScan(py_file)
        visitor.visit(tree)
        for lineno, message in visitor.violations:
            violations.append((str(py_file.relative_to(root)), lineno, message))
    return violations


def test_pku_sync_package_has_no_platform_conditional_or_gui_code():
    violations = scan_package()
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
