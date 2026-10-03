"""One bounded text-only trial for fifth-chapter spoken fragments."""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

import pku_sync.media as media
from pku_sync import platform
from pku_sync.config import Settings
from pku_sync.recovery_claim_selection import select_recovery_claims
from pku_sync.recovery_fact_readiness import assess_recovery_facts
from pku_sync.recovery_fragment_repair import (
    FragmentRepairError, build_request, validate_fragment_repairs,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suffix", nargs="?", default="v1")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=12)
    args = parser.parse_args()
    suffix = args.suffix
    if not suffix.isalnum():
        raise ValueError("Trial suffix must be alphanumeric")
    if args.start < 0 or not 1 <= args.count <= 12 or args.start + args.count > 12:
        raise ValueError("Trial needs a bounded slice of the 12 review rows")
    output = Path(f".probe/infosec-fragment-repair-{suffix}.json")
    if output.exists():
        raise RuntimeError("Refusing to overwrite the previous paid trial")
    path = Path(glob.glob(
        ".probe/infosec-first-chapter-fullv10quality/.notes-parts/"
        "0004-*.recovery-ledger-v13.json")[0])
    record = json.loads(path.read_text("utf-8"))
    claims, _ = select_recovery_claims(record["validated_claims"])
    # No service call is permitted while reconstructing already cached facts.
    media._recovery_llm = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("Cached ledger reconstruction attempted a cloud call"))
    verified = media._verified_ledger_sentences(
        None, "", claims, settings=None, record=record, cache_path=path)
    readiness = assess_recovery_facts(
        verified, expected_group_ids=list(record["transcript_groups"]))
    review = readiness.review_required
    if len(review) != 12:
        raise RuntimeError(f"Expected 12 review rows, got {len(review)}")
    review = review[args.start:args.start + args.count]
    system, user = build_request(review, selected_facts=readiness.selected)
    settings = Settings(_env_file=Path.home() / "PKU-All-in-Notion" / ".env")
    if not settings.platform_token:
        raise RuntimeError("The installed platform is not logged in")
    before = platform.quota(settings)
    started = time.perf_counter()
    reply = platform.llm("notes", user, settings, system=system)
    after = platform.quota(settings)
    result = {
        "source_ledger": str(path), "review_count": len(review),
        "review_ids": [row.get("id") or f"R{row['index']}" for row in review],
        "request_characters": len(user), "response": reply["content"],
        "seconds": round(time.perf_counter() - started, 2),
        "points_charged": reply["points_charged"],
        "quota_before": before.get("llm_points_remaining"),
        "quota_after": after.get("llm_points_remaining"),
    }
    try:
        checked = validate_fragment_repairs(
            review, reply["content"], selected_facts=readiness.selected)
    except FragmentRepairError as exc:
        result["validation_error"] = str(exc)
    else:
        result["validated"] = {
            "kept": checked.kept, "omitted": checked.omitted,
            "needs_audio": checked.needs_audio, "ready": checked.ready,
        }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output), "seconds": result["seconds"],
        "points_charged": result["points_charged"],
        "validation_error": result.get("validation_error"),
        "kept": len(result.get("validated", {}).get("kept", [])),
        "omitted": len(result.get("validated", {}).get("omitted", [])),
        "needs_audio": len(result.get("validated", {}).get("needs_audio", [])),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
