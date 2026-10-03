"""Bounded real-note acceptance probe; never writes the published note."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.media import _chapters, _merge_leading_fragment, write_notes
from pku_sync.note_sources import collect_note_sources


class FirstChapterDone(Exception):
    pass


def main() -> None:
    installed = Path.home() / "PKU-All-in-Notion"
    settings = Settings(_env_file=installed / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed app is not logged in")
    settings.notes_source_matching = False
    recording = (installed / "data" / "信息安全引论_26-27学年第1学期"
                 / "recordings" / "2026-09-17_2026-09-17第7-8节")
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    sources = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / name
                       for name in ("ch01-绪论.pdf", "ch02-古典密码.pdf")],
    )
    transcript = json.loads((recording / "transcript.json").read_text("utf-8"))
    label = sys.argv[1] if len(sys.argv) > 1 else "v5"
    if not label.isalnum():
        raise ValueError("Probe label must be alphanumeric")
    whole_lecture = label.startswith("full")
    selected_chapter = re.fullmatch(r"chapter(\d+)[a-z0-9]*", label)
    if selected_chapter:
        chapter_number = int(selected_chapter.group(1))
        windows = _merge_leading_fragment(
            _chapters(transcript["segments"], target_seconds=480),
            transcript["segments"], target_seconds=480,
        )
        if not 1 <= chapter_number <= len(windows):
            raise ValueError("Chapter number is out of range")
        selected = windows[chapter_number - 1]
        transcript = {**transcript, "segments": [row for row in transcript["segments"]
                                                if selected["start"] <= row["start"] <= selected["end"]]}
    output_label = ("v5" if label in {"full", "fullrender"}
                    else "chapter13latepages" if label == "chapter13repair"
                    else label)
    output = Path("E:/remote_project/pku-course-sync/.probe") / f"infosec-first-chapter-{output_label}"
    output.mkdir(parents=True, exist_ok=True)
    target = output / ("notes-layout-v2.md" if label == "fullrender"
                       else "notes-repaired.md" if label == "chapter13repair"
                       else "notes.md")
    if target.exists():
        raise RuntimeError("Refusing to reuse an earlier complete probe")

    before = platform.quota(settings)
    original_llm = platform.llm
    charges: list[float] = []

    def capped_llm(*args, **kwargs):
        cap = 48 if whole_lecture else 12 if selected_chapter else 2
        if len(charges) >= cap:
            raise RuntimeError(f"{cap} AI calls are the maximum for this probe")
        result = original_llm(*args, **kwargs)
        charge = result.get("points_charged")
        charges.append(float(charge) if isinstance(charge, (int, float)) else 0.0)
        return result

    def stop_after_one(done: int, total: int, stage: str, _: int) -> None:
        if whole_lecture or selected_chapter:
            if done:
                print(json.dumps({"progress": f"{done}/{total}",
                                  "ai_calls": len(charges),
                                  "points": round(sum(charges), 4)}), flush=True)
        elif done >= 1:
            raise FirstChapterDone(f"{done}/{total} {stage}")

    platform.llm = capped_llm
    started = time.perf_counter()
    status = "unknown"
    try:
        write_notes(transcript, [], target, settings, "信息安全引论", source_context=sources,
                    progress=stop_after_one)
        status = "completed" if whole_lecture or selected_chapter else "completed unexpectedly"
    except FirstChapterDone as exc:
        status = str(exc)
    except RuntimeError as exc:
        status = (str(exc.__cause__) if isinstance(exc.__cause__, FirstChapterDone)
                  else f"stopped: {exc}")
    finally:
        platform.llm = original_llm
    after = platform.quota(settings)
    print(json.dumps({
        "status": status,
        "seconds": round(time.perf_counter() - started, 2),
        "ai_calls": len(charges),
        "job_points_charged_including_reused": charges,
        "job_points_total_including_reused": round(sum(charges), 4),
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
        "net_balance_decrease": (
            round(before["llm_points_remaining"] - after["llm_points_remaining"], 4)
            if isinstance(before.get("llm_points_remaining"), (int, float))
            and isinstance(after.get("llm_points_remaining"), (int, float)) else None),
        "output": str(output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
