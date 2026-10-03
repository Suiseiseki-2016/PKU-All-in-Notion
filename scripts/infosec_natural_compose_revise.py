"""One bounded correction of audited unsupported natural-note assertions."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.note_sources import collect_note_sources


def main() -> None:
    root = Path(".probe")
    trial = json.loads((root / "infosec-natural-compose-fifth.json").read_text("utf-8"))
    audit = json.loads((root / "infosec-natural-compose-fifth-claim-audit.json")
                       .read_text("utf-8"))
    output = root / "infosec-natural-compose-fifth-revision.json"
    if output.exists():
        raise RuntimeError("Refusing to overwrite a previous revision")
    note = json.loads(trial["response"])["note"]
    assertions = {row["id"]: row["text"] for row in audit["assertions"]}
    failed = [{"id": row["id"], "original": assertions[row["id"]],
               "audit_reason": row["reason"]}
              for row in audit["verdicts"] if row["status"] != "supported"]
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    sources = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / "ch02-古典密码.pdf"],
    )
    pages = [{"page": row["locator"], "text": str(row["text"])[:2500]}
             for row in sources if row.get("status") == "readable"
             and row.get("locator") in {"第 29 页", "第 30 页", "第 31 页"}]
    system = (
        "你是严格依据课程课件修正大学课堂笔记的编辑。只修复 failed_assertions 中四条，"
        "每条可以删除或改成一至两句自然中文，但不得修改其他已通过的句子。"
        "来源页的年份必须逐项准确：1967 不能写成 1975；1971-73 表示 1971 至 1973。"
        "不得用‘奠定基础’‘被确立’‘演进为’等课件未写的因果或评价。"
        "输出严格 JSON：{\"replacements\":[{\"id\":\"C004\",\"text\":\"替换文字或空字符串\"}],"
        "\"unresolved\":[]}。每个 failed id 恰好一次，不要输出全文。"
    )
    settings = Settings(_env_file=Path.home() / "PKU-All-in-Notion" / ".env")
    if not settings.platform_token:
        raise RuntimeError("Installed app is not logged in")
    before = platform.quota(settings)
    started = time.perf_counter()
    response = platform.llm("notes", json.dumps({"failed_assertions": failed,
                                                   "matched_pages": pages}, ensure_ascii=False),
                            settings, system=system)
    after = platform.quota(settings)
    output.write_text(json.dumps({
        "failed_ids": [row["id"] for row in failed], "response": response["content"],
        "points_charged": response["points_charged"],
        "seconds": round(time.perf_counter() - started, 2),
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
        "original_note": note,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "seconds": round(time.perf_counter() - started, 2),
                      "points_charged": response["points_charged"],
                      "quota_before": before.get("llm_points_remaining"),
                      "quota_after": after.get("llm_points_remaining")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
