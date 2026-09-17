#!/usr/bin/env python3
"""Launch the pku-sync MCP stdio server independent of cwd and .pth health.

Python 3.11+ silently skips ``.pth`` files flagged ``UF_HIDDEN`` (macOS) or
``FILE_ATTRIBUTE_HIDDEN`` (Windows). On iCloud-synced locations (Desktop &
Documents) the sync service re-applies that flag to the editable install's
bootstrap ``.pth``, so ``python -m pku_sync.mcp_server`` fails whenever an
MCP host spawns the server from outside the project root. This launcher puts
the project root on ``sys.path`` explicitly; hosts register it instead of the
``-m`` form so the connection survives any working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pku_sync.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
