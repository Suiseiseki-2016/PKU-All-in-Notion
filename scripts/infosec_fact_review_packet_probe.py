"""Rebuild the fifth-chapter fact review packet from cached evidence only."""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pku_sync.media as media
from pku_sync.recovery_claim_selection import select_recovery_claims
from pku_sync.recovery_fact_readiness import assess_recovery_facts


def main() -> None:
    matches = glob.glob(
        ".probe/infosec-first-chapter-fullv10quality/.notes-parts/"
        "0004-*.recovery-ledger-v13.json")
    if len(matches) != 1:
        raise RuntimeError("Expected one cached fifth-chapter ledger")
    path = Path(matches[0])
    record = json.loads(path.read_text("utf-8"))
    claims, _ = select_recovery_claims(record["validated_claims"])
    media._recovery_llm = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("This probe must not call any cloud model"))
    verified = media._verified_ledger_sentences(
        None, "", claims, settings=None, record=record, cache_path=path)
    readiness = assess_recovery_facts(
        verified, expected_group_ids=list(record["transcript_groups"]))
    output = Path(".probe/infosec-fifth-fact-review-packet.json")
    packet = media._write_fact_readiness_review_packet(
        output, claims=verified, readiness=readiness)
    print(json.dumps({"output": str(output),
                      "selected": packet["selected_count"],
                      "review": packet["review_count"],
                      "uncovered_groups": packet["uncovered_groups"],
                      "automatic_approval": packet["automatic_approval"]},
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
