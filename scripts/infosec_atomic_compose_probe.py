"""One bounded natural-note trial from exact atomic facts, saved locally."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.note_sources import collect_note_sources


def main() -> None:
    output = Path(".probe/infosec-atomic-compose-fifth.json")
    if output.exists():
        raise RuntimeError("Refusing to overwrite a previous trial")
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    pages = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / "ch02-古典密码.pdf"],
    )
    by_page = {row["locator"]: str(row["text"]) for row in pages
               if row.get("status") == "readable"
               and row.get("locator") in {"第 29 页", "第 30 页", "第 31 页",
                                              "第 33 页", "第 36 页"}}
    requested = [
        ("F01", "第 29 页", "1949年Shannon"),
        ("F02", "第 29 页", "1967年David Kahn"),
        ("F03", "第 29 页", "1971-73年IBM Watson"),
        ("F04", "第 29 页", "数据的安全基于密钥而不是算法的保密"),
        ("F05", "第 30 页", "1976年Diffie & Hellman"),
        ("F06", "第 30 页", "1977年Rivest，Shamir & Adleman"),
        ("F07", "第 31 页", "1977年DES正式成为标准"),
        ("F08", "第 31 页", "2001年Rijndael成为DES的替代者"),
        ("F09", "第 33 页", "无条件安全"),
        ("F10", "第 33 页", "计算上安全"),
        ("F11", "第 36 页", "代替密码"),
        ("F12", "第 36 页", "置换密码"),
    ]
    facts = []
    for identity, page, anchor in requested:
        text = by_page.get(page, "")
        if anchor.replace(" ", "") not in text.replace("\n", "").replace(" ", ""):
            raise RuntimeError(f"Source anchor missing: {identity}")
        facts.append({"id": identity, "page": page, "source_anchor": anchor})
    settings = Settings(_env_file=Path.home() / "PKU-All-in-Notion" / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed app is not logged in")
    prompt = json.dumps({"facts": facts,
                         "source_pages": [{"page": page, "text": text[:2500]}
                                          for page, text in by_page.items()]},
                        ensure_ascii=False)
    system = (
        "你是为缺课大学生写中文课堂学习笔记的编辑。依据所给事实与对应课件页写三个自然段："
        "古典密码代替/置换、密码学发展年份、无条件与计算安全。"
        "每个事实只能使用其 source_anchor 所在页明确表达的含义，不能写额外的因果、"
        "历史评价、实现条件或课件没有的机构归属。"
        "F01=1949，F02=1967，F03=1971—1973，F05=1976，F06/F07=1977，F08=2001；"
        "课件第29页的1949～1975只是章节时期标题，不能替代人物与成果的具体年份。"
        "所有十二个事实必须在笔记中可识别，课件事实所在句末标注页码。"
        "写自然、紧凑的段落，不写时间轴或逐条列表。"
        "输出严格 JSON：{\"note\":\"Markdown 笔记正文\",\"used_ids\":[\"F01\",...]}。"
    )
    before = platform.quota(settings)
    started = time.perf_counter()
    response = platform.llm("notes", prompt, settings, system=system)
    after = platform.quota(settings)
    output.write_text(json.dumps({
        "response": response["content"], "points_charged": response["points_charged"],
        "seconds": round(time.perf_counter() - started, 2),
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "seconds": round(time.perf_counter() - started, 2),
                      "points_charged": response["points_charged"],
                      "quota_before": before.get("llm_points_remaining"),
                      "quota_after": after.get("llm_points_remaining")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
