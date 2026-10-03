"""Audit the saved single-call natural-note trial; no publication side effects."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.claim_audit import audit_chapter
from pku_sync.config import Settings
from pku_sync.media import _chapters, _merge_leading_fragment
from pku_sync.note_sources import collect_note_sources


def main() -> None:
    trial = json.loads(Path(".probe/infosec-natural-compose-fifth.json").read_text("utf-8"))
    note = json.loads(trial["response"])["note"]
    installed = Path.home() / "PKU-All-in-Notion"
    settings = Settings(_env_file=installed / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed app is not logged in")
    recording = (installed / "data" / "信息安全引论_26-27学年第1学期" / "recordings"
                 / "2026-09-17_2026-09-17第7-8节" / "transcript.json")
    segments = json.loads(recording.read_text("utf-8"))["segments"]
    fifth = _merge_leading_fragment(
        _chapters(segments, target_seconds=480), segments, target_seconds=480)[4]
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    pages = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / "ch02-古典密码.pdf"],
    )
    relevant = [row for row in pages if row.get("status") == "readable"
                and row.get("locator") in {"第 17 页", "第 29 页", "第 30 页",
                                           "第 31 页", "第 33 页", "第 36 页"}]
    calls: list[float] = []

    def ask(system: str, user: str) -> str:
        if len(calls) >= 20:
            raise RuntimeError("20 AI calls is the trial audit limit")
        response = platform.llm("notes", user, settings, system=system)
        calls.append(float(response["points_charged"]))
        return str(response["content"])

    before = platform.quota(settings)
    started = time.perf_counter()
    result = audit_chapter(
        note, fifth["evidence_blocks"], relevant, ask,
        cache_path=Path(".probe/infosec-natural-compose-fifth-claim-audit.json"),
        model=settings.notes_model,
    )
    after = platform.quota(settings)
    print(json.dumps({
        "status": result["status"],
        "unsupported": [row["id"] for row in result.get("verdicts", [])
                        if row["status"] != "supported"],
        "reason": result.get("reason"),
        "calls": len(calls), "points_charged": sum(calls),
        "seconds": round(time.perf_counter() - started, 2),
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
