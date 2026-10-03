"""One bounded, local-only natural-note trial for the validated fifth chapter."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.note_sources import collect_note_sources


def main() -> None:
    installed = Path.home() / "PKU-All-in-Notion"
    settings = Settings(_env_file=installed / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed app is not logged in")
    cache = Path(".probe/infosec-first-chapter-fullv10quality/.notes-parts")
    ledger = json.loads((cache / "0004-12d6bb55f5cd54e6d3cf.recovery-ledger-v13.json")
                        .read_text(encoding="utf-8"))
    output = Path(".probe/infosec-natural-compose-fifth.json")
    if output.exists():
        raise RuntimeError("Refusing to overwrite an earlier trial")
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    pages = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / "ch02-古典密码.pdf"],
    )
    selected_pages = [
        {"page": row["locator"], "text": str(row["text"])[:2500]}
        for row in pages if row.get("title") == "ch02-古典密码.pdf"
        and row.get("locator") in {"第 29 页", "第 30 页", "第 31 页", "第 33 页", "第 36 页"}
        and row.get("status") == "readable"
    ]
    selected_claims = [
        {"id": f"L{index:02d}", "origin": row["origin"],
         "text": row["display_text"], "page": row.get("source_page")}
        for index, row in enumerate(ledger["validated_claims"], 1)
        if index in {6, 10, 13, 21, 23, 24, 26, 28, 31, 32, 33, 34, 35}
    ]
    system = (
        "你是为缺课大学生写学习笔记的审慎编辑。仅用提供的已核实课堂事实与明确匹配的课件页，"
        "自然地分主题写完整中文段落，不要逐条照抄证据或写分钟时间轴。"
        "第29至31页的历史年份、人物、算法必须逐项核准；一次一密只能按第33页原文说明，"
        "不可写成现实中不可能使用。课堂事实和课件补充要区别归属，所有课件补充的具体事实"
        "紧随标注（课件：ch02-古典密码.pdf，第 N 页）。"
        "删除无学习价值的口头赘词、市场价格闲聊与重复句；不得自行补充老师要求、作业、"
        "截止时间、算法安全保证或未给出的历史评价。"
        "输出严格 JSON：{\"note\":\"Markdown 中文笔记\",\"used_ids\":[\"Lxx\"],"
        "\"unresolved\":[\"无法确认的事项\"]}。"
    )
    prompt = json.dumps({"verified_claims": selected_claims, "matched_slide_pages": selected_pages},
                        ensure_ascii=False)
    before = platform.quota(settings)
    start = time.perf_counter()
    response = platform.llm("notes", prompt, settings, system=system)
    after = platform.quota(settings)
    output.write_text(json.dumps({
        "prompt_claim_ids": [row["id"] for row in selected_claims],
        "pages": [row["page"] for row in selected_pages],
        "response": response["content"],
        "points_charged": response["points_charged"],
        "seconds": round(time.perf_counter() - start, 2),
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "seconds": round(time.perf_counter() - start, 2),
                      "points_charged": response["points_charged"],
                      "quota_before": before.get("llm_points_remaining"),
                      "quota_after": after.get("llm_points_remaining")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
