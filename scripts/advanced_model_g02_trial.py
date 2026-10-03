"""One private, bounded G02 evidence extraction trial; no product state mutation."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from pku_sync.media import _validate_recovery_ledger, _ledger_transcript_readable


ROOT = Path(r"C:\Users\A\AppData\Local\Temp\pku-quality-20260922-full-v4")
PACKET = ROOT / "g02-editorial-review-v1.json"
OUTPUT = ROOT / "g02-advanced-model-one-call.json"
MODEL = "qwen3.7-plus"
MAX_OUTPUT_TOKENS = 2500


REMOTE_CODE = r'''
import json, os, sys, time
import httpx
request = json.load(sys.stdin)
key = os.environ.get("DASHSCOPE_API_KEY")
base = (os.environ.get("BAILIAN_OPENAI_BASE_URL") or "").rstrip("/")
if request.get("_preflight"):
    print(json.dumps({"ready":bool(key and base)}))
    sys.exit(0)
if not key or not base:
    print(json.dumps({"error":"supplier_not_configured"}))
    sys.exit(3)
start = time.monotonic()
try:
    response = httpx.post(
        base + "/chat/completions",
        headers={"Authorization":"Bearer " + key},
        json=request,
        timeout=300,
    )
except Exception as exc:
    print(json.dumps({"error":"transport", "type":type(exc).__name__, "seconds":round(time.monotonic()-start,2)}))
    sys.exit(4)
if response.status_code != 200:
    print(json.dumps({"error":"supplier_http", "status":response.status_code, "seconds":round(time.monotonic()-start,2)}))
    sys.exit(5)
body = response.json()
choice = (body.get("choices") or [{}])[0]
print(json.dumps({
    "content":(choice.get("message") or {}).get("content"),
    "finish_reason":choice.get("finish_reason"),
    "usage":body.get("usage") or {},
    "seconds":round(time.monotonic()-start,2),
},ensure_ascii=False))
'''


def main() -> int:
    if "--preflight" in sys.argv:
        code = base64.b64encode(REMOTE_CODE.encode()).decode("ascii")
        remote = f'docker exec -i pku-relay python -c "import base64;exec(base64.b64decode(\'{code}\'))"'
        cmd = ["ssh", "-F", r"C:\Users\A\.ssh\config",
               "-o", r"UserKnownHostsFile=C:\Users\A\.ssh\known_hosts",
               "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "server-a", remote]
        result = subprocess.run(cmd, input=b'{"_preflight":true}', capture_output=True, timeout=30)
        print(json.dumps({"exit":result.returncode,
                          "stdout":result.stdout.decode("utf-8", "replace").strip(),
                          "stderr":result.stderr.decode("utf-8", "replace").strip()}))
        return 0 if result.returncode == 0 else 1
    if "--estimate" not in sys.argv and "--send-approved-trial" not in sys.argv:
        print("No paid call: use --estimate, or run the explicitly approved trial.")
        return 2
    if OUTPUT.exists():
        print("Output exists; refusing a second paid request.")
        return 2
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    group = packet["uncovered_groups"][0]
    blocks = group["blocks"]
    slides = [
        row for row in packet["source_pages"]
        if row.get("page") in {"第 11 页", "第 13 页", "第 16 页", "第 20 页"}
    ]
    assert len(blocks) == 9 and len(slides) <= 6
    system = (
        "你是课堂证据审校员，只返回严格 JSON：顶层 schema_version=10 和 claims 数组。"
        "每条 claim 仅用一个证据块中的连续逐字原文，text 和 display_text 完全相同；"
        "translation_status=not_needed，origin=transcript，evidence_block_id、evidence_start、"
        "evidence_end 原样复制，transcript_excerpt 也是该块的连续原文；"
        "source_filename/source_page/source_quote 必须为 null。"
        "只摘取完整、易读、低风险且对课程笔记有用的课堂观察。"
        "医学因果、药物安全、饮酒安全、孕期情绪与胎儿健康的推断、具体数值与建议，"
        "除非转写逐字清楚且能由可靠来源交叉核实，否则不摘取。"
        "如果证据含糊或 ASR 错误，省略该条。课件仅供辨认术语，不能冒充教师原话。"
        "不要跨块拼接或为凑数量输出不完整片段。"
        "每条 claim 键恰为 text, display_text, translation_status, origin, evidence_block_id, "
        "transcript_excerpt, evidence_start, evidence_end, source_filename, source_page, source_quote。"
    )
    user = (
        "课程：发展心理学；仅对这节 485 秒录像的 G02 提取可逐字追溯事实。\n"
        "EVIDENCE_BLOCKS=" + json.dumps(blocks, ensure_ascii=False) + "\n"
        "MATCHED_SLIDE_CONTEXT=" + json.dumps(slides, ensure_ascii=False) + "\n"
        "仅返回 JSON，保守省略不确定内容。"
    )
    request = {
        "model": MODEL,
        "temperature": 0.2,
        "enable_thinking": False,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request_bytes = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if "--estimate" in sys.argv:
        print(json.dumps({
            "model": MODEL,
            "blocks": len(blocks),
            "matched_slide_pages": len(slides),
            "request_utf8_bytes": len(request_bytes),
            "input_token_ceiling_from_bytes": len(request_bytes),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "worst_case_beijing_yuan": round((len(request_bytes) * 2 + MAX_OUTPUT_TOKENS * 8) / 1_000_000, 6),
        }, ensure_ascii=False))
        return 0
    # Conservative < 40k input-token ceiling: even this plus 2500 output
    # costs (40k*2 + 2500*8)/1e6 = ¥0.10 at the verified Beijing price.
    if len(request_bytes) > 40_000:
        print("Input is larger than the cost preflight ceiling; no call made.")
        return 3
    code = base64.b64encode(REMOTE_CODE.encode()).decode("ascii")
    remote = f'docker exec -i pku-relay python -c "import base64;exec(base64.b64decode(\'{code}\'))"'
    cmd = [
        "ssh", "-F", r"C:\Users\A\.ssh\config",
        "-o", r"UserKnownHostsFile=C:\Users\A\.ssh\known_hosts",
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "server-a", remote,
    ]
    started = time.perf_counter()
    result = subprocess.run(cmd, input=request_bytes, capture_output=True, timeout=360)
    if result.returncode:
        print(json.dumps({"status":"failed", "exit_code":result.returncode,
                          "elapsed_seconds":round(time.perf_counter()-started,2)}))
        return 4
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        print("Remote response was not valid JSON.")
        return 5
    if "error" in response:
        print(json.dumps({"status":"supplier_error", **response}))
        return 6
    content = str(response.get("content") or "")
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    transcript = " ".join(str(row["text"]) for row in blocks)
    accepted, rejected = _validate_recovery_ledger(
        payload, transcript, [], evidence_blocks=blocks,
        allow_provisional_fragments=True,
    )
    usage = response.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    estimated_yuan = (input_tokens * 2 + output_tokens * 8) / 1_000_000
    report = {
        "model": MODEL,
        "source_bundle_sha256": packet["source_bundle_sha256"],
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "request_bytes": len(request_bytes),
        "supplier_seconds": response.get("seconds"),
        "end_to_end_seconds": round(time.perf_counter()-started, 2),
        "finish_reason": response.get("finish_reason"),
        "usage": usage,
        "beijing_list_estimate_yuan": round(estimated_yuan, 6),
        "raw": content,
        "accepted": accepted,
        "rejected": rejected,
        "readable_count": sum(_ledger_transcript_readable(row["display_text"]) for row in accepted),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status":"done", "output":str(OUTPUT),
        "supplier_seconds":response.get("seconds"),
        "end_to_end_seconds":report["end_to_end_seconds"],
        "input_tokens":input_tokens, "output_tokens":output_tokens,
        "beijing_list_estimate_yuan":report["beijing_list_estimate_yuan"],
        "raw_claims":len(payload.get("claims", [])) if isinstance(payload, dict) else None,
        "accepted":len(accepted), "rejected":len(rejected),
        "readable":report["readable_count"],
        "rejection_reasons":[row.get("reason") for row in rejected],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
