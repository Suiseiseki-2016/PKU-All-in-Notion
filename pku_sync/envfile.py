"""Minimal .env writer: upsert keys, never disturb the rest of the file.

Used by the OAuth login (NOTION_TOKEN) and the first-run setup wizard
(PKU account, LLM key). UTF-8 throughout: course names and comments in
this file are Chinese.
"""

from __future__ import annotations

import re
from pathlib import Path


def write_env_values(path: Path, values: dict[str, str]) -> Path:
    """Upsert ``KEY=value`` lines in a .env file, preserving every other line.

    Existing keys are replaced in place; new keys are appended. The file is
    created when absent. Commented examples (``# KEY=``) are left alone.
    """
    if not values:
        return path
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        if pattern.search(text):
            text = pattern.sub(lambda _m: line, text)
        else:
            text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path
