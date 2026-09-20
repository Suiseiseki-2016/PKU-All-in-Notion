#!/usr/bin/env python
"""Run the reusable fake-only Window D panel-path probe.

The probe uses the real in-process panel organize and grade HTTP controllers.
Its relay and Notion boundaries are deterministic fakes, so this command never
calls an LLM, reads credentials, or mutates a real Notion workspace.  The JSON
output documents the fixture/page identifiers, read-only checks, fake relay
ledger, rerun outcomes, and cleanup proof.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pku_sync.e2e_window_d import run_dry_run, write_dry_run_pack


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fake-only Window D panel organize/grade lifecycle."
    )
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--output",
        type=Path,
        help="JSON evidence file to write (use a mission validation or temp path).",
    )
    destination.add_argument(
        "--pack-dir",
        type=Path,
        help="Write a Window D fixture pack accepted by the M4 evidence harness.",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        help="Disposable scratch root; defaults to a unique system-temp directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scratch_root = args.scratch_root or Path(
        tempfile.mkdtemp(prefix="pku-window-d-dry-run-")
    )
    if args.pack_dir is not None:
        result = write_dry_run_pack(pack_dir=args.pack_dir, scratch_root=scratch_root)
        evidence = result["raw"]
        destination = str(args.pack_dir)
    else:
        evidence = run_dry_run(output=args.output, scratch_root=scratch_root)
        destination = str(args.output)
    summary = {
        "outcome": evidence["outcome"],
        "mode": evidence["mode"],
        "destination": destination,
        "checks_passed": sum(
            check["result"] == "pass" for check in evidence["checks"]
        ),
        "ledger_rows": len(evidence["ledger_rows"]),
        "cleanup_complete": evidence["cleanup"]["scratch_root_removed"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not summary["cleanup_complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())