"""One bounded real-lecture claim audit; saves diagnostics locally only."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.claim_audit import audit_chapter
from pku_sync.config import Settings
from pku_sync.media import _chapters, _merge_leading_fragment, _merge_source_pages, _select_source_pages
from pku_sync.note_sources import collect_note_sources


def main() -> None:
    installed = Path.home() / "PKU-All-in-Notion"
    settings = Settings(_env_file=installed / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed desktop account is not logged in")
    recording = (installed / "data" / "信息安全引论_26-27学年第1学期"
                 / "recordings" / "2026-09-17_2026-09-17第7-8节")
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    pages = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / name
                       for name in ("ch01-绪论.pdf", "ch02-古典密码.pdf")],
    )
    transcript = json.loads((recording / "transcript.json").read_text("utf-8"))
    windows = _merge_leading_fragment(
        _chapters(transcript["segments"], target_seconds=480),
        transcript["segments"], target_seconds=480,
    )
    first_full_chapter = len(sys.argv) == 2 and sys.argv[1] == "chapter1"
    if first_full_chapter:
        window = windows[0]
        note = Path(".probe/infosec-first-chapter-fullv4/notes.md").read_text("utf-8")
        body = note.split("## 课堂内容", 1)[1].split("## 作业与考试口头线索", 1)[0]
        chapter = re.split(r"(?=^### )", body, flags=re.M)[1]
        cited = {(name, f"第 {int(page)} 页") for name, page in re.findall(
            r"（课件：([^，（）]+)，第\s*(\d+)\s*页", chapter)}
        sources = _merge_source_pages(
            [row for row in pages if (row.get("title"), row.get("locator")) in cited],
            _select_source_pages(window["text"] + chapter, pages,
                                 max_pages=5, max_chars=7500),
        )
    else:
        # Genuine chapter 12 statements, including two known false claims.
        window = windows[11]
        chapter = (
            "### 对抗频率分析与多表代替密码\n\n"
            "多音代替采用“一对多”映射，将同一明文字母替换为多个不同密文字母以打乱频率分布。\n"
            "多对一映射用于确保解密可逆性（录像：88:02）。\n"
            "多表代替利用多个代替表加密，相当于分组加密。\n"
            "弗吉尼亚密码是多表代替的典型代表。"
        )
        sources = _merge_source_pages(
            [row for row in pages if row.get("title") == "ch02-古典密码.pdf"
             and row.get("locator") in {"第 38 页", "第 39 页", "第 40 页"}],
            _select_source_pages(window["text"], pages, max_pages=5, max_chars=7500),
        )
    calls = 0
    def ask(system: str, user: str) -> str:
        nonlocal calls
        if calls >= 2:
            raise RuntimeError("This probe allows at most two AI calls")
        calls += 1
        return str(platform.llm("notes", user, settings, system=system)["content"])

    output = Path(".probe/infosec-claim-audit-chapter1-v4.json" if first_full_chapter
                  else ".probe/infosec-claim-audit-v1.json")
    before = platform.quota(settings)
    started = time.perf_counter()
    result = audit_chapter(chapter, window["evidence_blocks"], sources, ask,
                           cache_path=output, model=settings.notes_model)
    after = platform.quota(settings)
    print(json.dumps({
        "status": result["status"], "reason": result["reason"],
        "verdicts": [{"id": row["id"], "status": row["status"],
                      "reason": row["reason"]} for row in result["verdicts"]],
        "calls": calls, "seconds": round(time.perf_counter() - started, 2),
        "balance_delta": round(before["llm_points_remaining"]
                               - after["llm_points_remaining"], 4),
        "report": str(output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
