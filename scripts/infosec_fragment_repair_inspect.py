"""Explain each local fragment-repair trial decision without service calls."""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import pku_sync.media as media
from pku_sync.claim_audit import _repair_reason_quotes
from pku_sync.recovery_claim_selection import select_recovery_claims
from pku_sync.recovery_fact_readiness import assess_recovery_facts
from pku_sync.recovery_fragment_repair import FragmentRepairError, validate_fragment_repairs


def main() -> None:
    suffix = sys.argv[1] if len(sys.argv) == 2 else "v2"
    trial = json.loads(Path(f".probe/infosec-fragment-repair-{suffix}.json").read_text("utf-8"))
    response = str(trial["response"]).strip()
    if response.startswith("```json\n") and response.endswith("\n```"):
        response = response[8:-4]
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        parsed = _repair_reason_quotes(response)
        if parsed is None:
            raise
    path = Path(glob.glob(
        ".probe/infosec-first-chapter-fullv10quality/.notes-parts/"
        "0004-*.recovery-ledger-v13.json")[0])
    record = json.loads(path.read_text("utf-8"))
    claims, _ = select_recovery_claims(record["validated_claims"])
    media._recovery_llm = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("Cloud call is prohibited in inspection"))
    verified = media._verified_ledger_sentences(
        None, "", claims, settings=None, record=record, cache_path=path)
    readiness = assess_recovery_facts(verified, expected_group_ids=list(record["transcript_groups"]))
    by_id = {f"R{row['index']}": row for row in readiness.review_required}
    outcomes = []
    for decision in parsed["repairs"]:
        identity = decision["id"]
        row = by_id.get(identity)
        if row is None:
            outcomes.append({"id": identity, "result": "unknown_id"})
            continue
        try:
            validated = validate_fragment_repairs(
                [row], {"repairs": [decision]}, selected_facts=readiness.selected)
        except FragmentRepairError as exc:
            outcomes.append({"id": identity, "status": decision.get("status"),
                             "result": "rejected", "reason": str(exc)})
        else:
            accepted = validated.kept or validated.omitted or validated.needs_audio
            outcomes.append({"id": identity, "status": decision.get("status"),
                             "result": "accepted", "reason": accepted[0]["reason"]})
    print(json.dumps(outcomes, ensure_ascii=True))


if __name__ == "__main__":
    main()
