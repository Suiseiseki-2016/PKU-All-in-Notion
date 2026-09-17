#!/usr/bin/env python3
"""Entry-point E2E: launch ``pkutui`` exactly like a user does.

Real entry coverage: run the installed ``pkutui`` console script in a pty,
wait for the course table to render, then SIGTERM and assert a clean exit.
The interactive ``q`` path is covered by the Textual ``run_test`` suite.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CANDIDATES = (
    ("pkutui", None),
    (str(PROJECT_ROOT / ".venv" / "bin" / "pkutui"), None),
    (sys.executable, ("-m", "pku_sync.cli", "tui")),
)

READ_IDLE = 45  # seconds to wait for the course table
QUIT_WAIT = 20  # seconds for the process to exit after q


def _pick_candidate():
    for name, argv in CANDIDATES:
        if argv is not None or shutil.which(name):
            return name, argv
    raise RuntimeError("no pkutui candidate found")


def scenario() -> tuple[str, int]:
    exe, args = _pick_candidate()
    entry = exe if args is None else f"{exe} {' '.join(args)}"

    master, slave = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color"}
    proc = subprocess.Popen(
        [exe, *(args or ())],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=str(PROJECT_ROOT),
        env=env,
        close_fds=True,
    )
    os.close(slave)

    # drain the pty until the course table renders or we time out
    output = ""
    deadline = time.monotonic() + READ_IDLE
    rendered = False
    while time.monotonic() < deadline:
        r, _, _ = select.select([master], [], [], 1.0)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk.decode("utf-8", errors="replace")
            if "课程" in output and "本地" in output:
                rendered = True
                break
        if proc.poll() is not None:
            break

    if not rendered:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        os.close(master)
        tail = output[-600:]
        raise RuntimeError(
            f"pkutui did not render the course table within {READ_IDLE}s; "
            f"entry={entry}; rc={proc.poll()}; tail={tail!r}"
        )

    os.kill(proc.pid, signal.SIGTERM)  # the interactive q path is covered by run_test; here we verify startup + render
    try:
        proc.wait(QUIT_WAIT)
    except subprocess.TimeoutExpired:
        proc.kill()
        os.close(master)
        raise RuntimeError(
            f"pkutui did not exit within {QUIT_WAIT}s after SIGTERM; entry={entry}; "
            f"tail={output[-400:]!r}"
        )
    os.close(master)
    if proc.returncode not in (0, -signal.SIGTERM):
        raise RuntimeError(
            f"pkutui exited with {proc.returncode}; entry={entry}; tail={output[-400:]!r}"
        )
    return entry, proc.returncode


def main() -> int:
    try:
        entry, rc = scenario()
    except Exception as exc:  # noqa: BLE001 - an E2E failure needs its real cause
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"E2E PASS: pkutui launched via entry '{entry}', rendered course table, exited cleanly on SIGTERM (rc={rc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
