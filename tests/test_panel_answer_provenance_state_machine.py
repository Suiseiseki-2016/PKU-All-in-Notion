"""Additive coverage for answer provenance and marker-authoritative reruns."""
from __future__ import annotations

import json
from pathlib import Path

from pku_sync.panel.exercise_grader import (
    ANSWER_PROVENANCE_KNOWN,
    ANSWER_PROVENANCE_MARKER_ONLY,
    ANSWER_PROVENANCE_UNKNOWN,
    ExerciseGrader,
    FakeGradingPageAdapter,
    GradingInput,
    GradeTarget,
    _answer_provenance,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED


def _piece(text):
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


def _block(kind, text):
    return {"type": kind, kind: {"rich_text": _piece(text)}}


def _target():
    return GradeTarget(
        operation="grade",
        page_id=EXERCISE_GENERATED,
        page_url=f"https://www.notion.so/{EXERCISE_GENERATED}",
        title="考前练习",
        course_id="course-page",
        course_title="计算机网络",
        scope="考前练习",
    )


class _Relay:
    def __init__(self):
        self.calls = 0

    def quota(self):
        return {"available": True, "llm_points_remaining": 20.0}

    def grade(self, _prompt):
        self.calls += 1
        return {"content": json.dumps({"score": 73}), "points_charged": 2}


class _ProvenanceAdapter(FakeGradingPageAdapter):
    def __init__(self, provenance):
        super().__init__()
        self.provenance = provenance

    def read_for_grading(self, target):
        return GradingInput(
            prompt="grade",
            unanswered=[],
            marker_present=True,
            existing_result=self.result,
            answer_provenance=self.provenance,
        )


def test_answer_provenance_ignores_result_section_and_recognizes_complete_fixture():
    blocks = [
        _block("heading_3", "第 1 题"),
        _block("heading_3", "第 2 题"),
        _block("heading_2", "E2E_ANSWER_FIXTURE"),
        _block("numbered_list_item", "1. answer-a"),
        _block("numbered_list_item", "2. answer-b"),
        _block("heading_2", "批改结果"),
        _block("numbered_list_item", "总分：80"),
    ]
    provenance, fingerprint = _answer_provenance(blocks)
    assert provenance == ANSWER_PROVENANCE_KNOWN and fingerprint

    blocks[-1] = _block("numbered_list_item", "总分：10")
    assert _answer_provenance(blocks) == (ANSWER_PROVENANCE_KNOWN, fingerprint)
    assert _answer_provenance([_block("heading_2", "批改结果")]) == (
        ANSWER_PROVENANCE_MARKER_ONLY,
        "",
    )


def test_answer_provenance_recognizes_production_answer_blocks():
    blocks = [
        _block("heading_3", "第 1 题"),
        _block("paragraph", "答案：answer-a"),
        _block("heading_3", "第 2 题"),
        _block("paragraph", "答案：answer-b"),
        _block("heading_2", "教师区（答案）"),
        _block("paragraph", "参考答案：teacher-only"),
        _block("heading_2", "批改结果"),
    ]

    provenance, fingerprint = _answer_provenance(blocks)

    assert provenance == ANSWER_PROVENANCE_KNOWN
    assert fingerprint


def test_deleted_or_malformed_answers_are_unknown_not_marker_only():
    deleted = [
        _block("heading_3", "第 1 题"),
        _block("heading_3", "第 2 题"),
        _block("heading_2", "批改结果"),
    ]
    malformed = [
        _block("heading_3", "第 1 题"),
        _block("heading_3", "第 2 题"),
        _block("paragraph", "我的答案是 answer-a"),
        _block("heading_2", "批改结果"),
    ]
    unrecognized_without_headings = [
        _block("paragraph", "作答内容 answer-a"),
        _block("heading_2", "批改结果"),
    ]
    assert _answer_provenance(deleted)[0] == ANSWER_PROVENANCE_UNKNOWN
    assert _answer_provenance(malformed)[0] == ANSWER_PROVENANCE_UNKNOWN
    assert _answer_provenance(unrecognized_without_headings)[0] == ANSWER_PROVENANCE_UNKNOWN


def test_marker_only_reconstructs_free_result_with_unknown_settlement_even_when_forced():
    directory = build_fake_directory()
    directory.load()
    relay = _Relay()
    adapter = _ProvenanceAdapter(ANSWER_PROVENANCE_MARKER_ONLY)
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    prepared = (
        _target(),
        GradingInput(
            prompt="grade",
            unanswered=[],
            marker_present=True,
            existing_result={"score": 91, "graded_at": "2026-01-01T00:00:00+08:00"},
            answer_provenance=ANSWER_PROVENANCE_MARKER_ONLY,
        ),
    )

    result = grader.grade(prepared, force=True)

    assert result["score"] == 91 and result["points_charged"] is None
    assert result["points_remaining"] is None and relay.calls == 0


def test_marker_only_does_not_trust_malformed_local_settlement_record():
    directory = build_fake_directory()
    directory.load()
    directory.local_grading_records[EXERCISE_GENERATED] = {}
    relay = _Relay()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=FakeGradingPageAdapter(),
    )
    prepared = (
        _target(),
        GradingInput(
            prompt="grade",
            unanswered=[],
            marker_present=True,
            existing_result={"score": 91},
            answer_provenance=ANSWER_PROVENANCE_MARKER_ONLY,
        ),
    )

    result = grader.grade(prepared)

    assert result["points_charged"] is None
    assert relay.calls == 0


def test_unknown_answer_provenance_never_reuses_marker_and_forced_rerun_is_metered():
    directory = build_fake_directory()
    directory.load()
    relay = _Relay()
    adapter = _ProvenanceAdapter(ANSWER_PROVENANCE_UNKNOWN)
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    prepared = (
        _target(),
        GradingInput(
            prompt="grade",
            unanswered=[],
            marker_present=True,
            existing_result={"score": 91},
            answer_provenance=ANSWER_PROVENANCE_UNKNOWN,
        ),
    )

    result = grader.grade(prepared, force=True)

    assert result["points_charged"] == 2 and relay.calls == 1
    assert adapter.write_calls[-1].get("updated") is True


def test_marker_reconstruction_ui_renders_unknown_settlement_copy():
    script = (Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'typeof r.points_charged === "number"' in script
    assert "COPY.directory.exercises.settlement_unknown" in script
