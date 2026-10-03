"""One bounded, locally saved real-lecture trial of sentence-bound composition."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.note_sources import collect_note_sources
from pku_sync.structured_note_composer import CompositionError, build_request, validate_and_render


def main() -> None:
    suffix = sys.argv[1] if len(sys.argv) == 2 else "fifth"
    if not suffix.isalnum():
        raise ValueError("Trial suffix must be alphanumeric")
    destination = Path(f".probe/infosec-structured-compose-{suffix}.json")
    if destination.exists():
        raise RuntimeError("Refusing to overwrite the previous paid trial")
    course = Path("E:/pku-course-data/信息安全引论_26-27学年第1学期")
    filename = "ch02-古典密码.pdf"
    pages = collect_note_sources(
        course, "2026-09-17", "信息安全引论",
        allowed_paths=[course / "materials" / "课程内容" / filename],
    )
    source = {row["locator"]: str(row["text"]) for row in pages
              if row.get("status") == "readable"}
    rows = [
        ("F01", 29, "1949年Shannon", "1949 年 Shannon 的 The Communication Theory of Secret Systems。"),
        ("F02", 29, "1967年David Kahn", "1967 年 David Kahn 的《The Codebreakers》。"),
        ("F03", 29, "1971-73年IBM Watson", "1971—1973 年，IBM Watson 实验室的 Horst Feistel 等发表了几篇技术报告。"),
        ("F04", 29, "数据的安全基于密钥而不是算法的保密", "数据的安全基于密钥的保密，而不是算法的保密。"),
        ("F05", 30, "1976年Diffie & Hellman", "1976 年 Diffie 与 Hellman 的 New Directions in Cryptography 提出了不对称密钥密码。"),
        ("F06", 30, "1977年Rivest，Shamir & Adleman", "1977 年 Rivest、Shamir 和 Adleman 提出了 RSA 公钥算法。"),
        ("F07", 31, "1977年DES正式成为标准", "1977 年 DES 正式成为标准。"),
        ("F08", 31, "2001年Rijndael成为DES的替代者", "2001 年 Rijndael 成为 DES 的替代者。"),
        ("F09", 33, "One-time pad", "无条件安全指破译者即使有任意多密文，也无法确定对应明文；课件列出 One-time pad。"),
        ("F10", 33, "破译的代价超出信息本身的价值", "计算上安全包括破译代价超过信息价值，或破译时间超过信息有效期。"),
        ("F11", 36, "代替密码", "代替密码把明文中的字符替换为密文中的另一字符，接收者反向替换以恢复明文。"),
        ("F12", 36, "置换密码", "置换密码保留明文的字母，但打乱其顺序。"),
    ]
    facts = []
    for identity, page, anchor, display in rows:
        locator = f"第 {page} 页"
        if anchor.replace(" ", "") not in source.get(locator, "").replace("\n", "").replace(" ", ""):
            raise RuntimeError(f"Missing source anchor: {identity}")
        facts.append({"id": identity, "origin": "slide", "display_text": display,
                      "source_filename": filename, "source_page": page})

    settings = Settings(_env_file=Path.home() / "PKU-All-in-Notion" / ".env")
    if not settings.platform_token:
        raise RuntimeError("The installed app is not logged in")
    before = platform.quota(settings)
    started = time.perf_counter()
    batches = ([facts[:3], facts[3:6], facts[6:10], facts[10:]]
               if suffix.startswith("batch") else [facts])
    results = []
    for batch in batches:
        system, user = build_request(batch, title="信息安全引论：古典密码与密码学发展")
        response = platform.llm("notes", user, settings, system=system)
        item: dict = {"fact_ids": [row["id"] for row in batch],
                      "response": response["content"],
                      "points_charged": response["points_charged"]}
        try:
            composed = validate_and_render(batch, response["content"])
        except CompositionError as exc:
            item["validation_error"] = str(exc)
        else:
            item["note"] = composed.markdown
            item["sentences"] = [vars(sentence) for sentence in composed.sentences]
        results.append(item)
        destination.write_text(json.dumps({"facts": facts, "batches": results},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    elapsed = round(time.perf_counter() - started, 2)
    after = platform.quota(settings)
    result: dict = {"facts": facts, "batches": results, "seconds": elapsed,
                    "points_charged": round(sum(float(item["points_charged"])
                                                for item in results), 3),
                    "quota_before": before.get("llm_points_remaining"),
                    "quota_after": after.get("llm_points_remaining")}
    if all("note" in item for item in results):
        result["note"] = "\n\n".join(item["note"] for item in results)
        result["sentences"] = [sentence for item in results
                               for sentence in item["sentences"]]
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(destination), "seconds": elapsed,
                      "points_charged": result["points_charged"],
                      "batch_errors": [item.get("validation_error") for item in results],
                      "characters": len(result.get("note", ""))}, ensure_ascii=True))


if __name__ == "__main__":
    main()
