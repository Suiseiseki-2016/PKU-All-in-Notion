"""Changed-answer rerun and production local provenance coverage."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pku_sync.panel.exercise_grader import ExerciseGrader, FakeGradingPageAdapter
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED

@dataclass
class Relay:
    balance: float = 20.0
    def __post_init__(self): self.grade_calls = 0
    def quota(self): return {"available": True, "llm_points_remaining": self.balance}
    def grade(self, _prompt):
        self.grade_calls += 1; self.balance -= 2
        return {"content": json.dumps({"score": 80}), "points_charged": 2}

class Adapter(FakeGradingPageAdapter):
    def __init__(self):
        super().__init__(); self.answer_fingerprint = "answers-v1"
    def read_for_grading(self, target):
        value = super().read_for_grading(target)
        from pku_sync.panel.exercise_grader import GradingInput
        return GradingInput(prompt=value.prompt, unanswered=value.unanswered,
                            marker_present=value.marker_present, existing_result=value.existing_result,
                            answer_fingerprint=self.answer_fingerprint)

def test_unchanged_marker_rerun_is_free_even_when_explicitly_confirmed():
    directory = build_fake_directory(); directory.load(); relay = Relay(); adapter = Adapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    first = grader.grade(grader.prepare(EXERCISE_GENERATED))
    second = grader.grade(grader.prepare(EXERCISE_GENERATED), force=True)
    assert first["points_charged"] == 2 and second["points_charged"] == 0
    assert relay.grade_calls == 1 and len(adapter.write_calls) == 1

def test_changed_answers_charge_once_and_update_in_place():
    directory = build_fake_directory(); directory.load(); relay = Relay(); adapter = Adapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    grader.grade(grader.prepare(EXERCISE_GENERATED)); adapter.answer_fingerprint = "answers-v2"
    result = grader.grade(grader.prepare(EXERCISE_GENERATED), force=True)
    assert result["points_charged"] == 2 and relay.grade_calls == 2
    assert len(adapter.write_calls) == 2 and adapter.write_calls[-1].get("updated") is True
    assert directory.local_grading_records[EXERCISE_GENERATED]["answer_fingerprint"] == "answers-v2"

def test_local_record_is_available_to_directory_mismatch_boundary():
    directory = build_fake_directory(); directory.load(); relay = Relay(); adapter = Adapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    grader.grade(grader.prepare(EXERCISE_GENERATED))
    row = next(item for item in directory.load()["exercises"] if item["id"] == EXERCISE_GENERATED)
    assert row["mismatch_notice"] is True

