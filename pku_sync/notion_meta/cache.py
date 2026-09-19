"""Local identity cache (VAL-META-009): discovered ids stored under DATA_DIR.

Launch paths resolve from this file, never from workspace search. The cache
holds only page identity (ids/urls/titles) — no body content, no tokens.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CACHE_VERSION = 2


def default_cache_path(data_dir: Path | str) -> Path:
    """Cache location inside the client's DATA_DIR (gitignored, local-only)."""
    return Path(data_dir) / "notion" / "identity_cache.json"


class IdentityCache:
    def __init__(self, path: Path | str):
        self._path = Path(path)

    def save(self, data) -> None:
        """Persist discovered identity after a successful directory read."""
        payload = {
            "version": CACHE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "semester": data.semester,
            "hub": data.hub.model_dump(),
            "semester_page": data.semester_page.model_dump() if data.semester_page else None,
            "notes_hub": data.notes_hub.model_dump() if data.notes_hub else None,
            "material_database": data.material_database.model_dump(),
            "wrong_answer_database": (
                data.wrong_answer_database.model_dump()
                if data.wrong_answer_database is not None else None
            ),
            "courses": [course.model_dump() for course in data.courses],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def load(self) -> dict | None:
        """Read the stored identity; a missing/corrupt/foreign-version cache
        is simply a cache miss (a fresh directory read repopulates it)."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return None
        if "wrong_answer_database" not in raw:
            return None
        target = raw["wrong_answer_database"]
        if target is not None and (
            not isinstance(target, dict)
            or not all(str(target.get(key) or "").strip() for key in ("id", "url", "title"))
        ):
            return None
        return raw
