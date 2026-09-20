"""Assemble the complete fake-mode panel service set for one startup variant.

Shared by ``pku-sync panel --fake`` (``cli.py``) and the variant-wiring tests
so the browser-verifiable fixtures stay exactly reproducible: the same fake
directory, connection, platform bridge, organizer, and grader that a
validator starts with ``--fake-variant <variant>``.

The relay failure injection for the blocked/error UI variants lives ONLY on
the fake platform bridge (see ``FakePlatformBridge``): the flags default to
None and are inert for every variant unless the wiring below opts a variant
into them. Production runs use ``RealPlatformBridge`` and never see these.
"""

from __future__ import annotations

from ..notion_meta import canonical_url
from .connection import build_fake_connection_service
from .exercise_grader import make_fake_grading_service
from .exercise_organizer import ExerciseOrganizer, MemoryOrganizeRecordStore
from .fake_directory import build_fake_directory_service
from .platform_bridge import FakePlatformBridge

_FIXTURE_PAGE_ID = "2c000001-0000-4000-8000-000000000901"
_FIXTURE_PAGE_UPDATED = "2026-09-19T08:00:00.000Z"


class _FakeNotes:
    def for_scope(self, *, course_title, lecture_titles):
        return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "演示笔记"}]


class _FakePages:
    """In-memory organizer page adapter: one deterministic created page."""

    def __init__(self):
        self.created: dict | None = None

    def create_page(self, parent_page_id, title, *, children):
        if self.created is None:
            self.created = {
                "id": _FIXTURE_PAGE_ID,
                "url": canonical_url(_FIXTURE_PAGE_ID),
                "last_edited_time": _FIXTURE_PAGE_UPDATED,
            }
        return dict(self.created)


def build_fake_panel_services(variant: str = "normal"):
    """Build ``(directory, connection, platform, organizer, grading)`` for one
    ``--fake-variant`` value, exactly as the CLI wires a panel."""
    directory_service = build_fake_directory_service(variant=variant)
    connection_service = build_fake_connection_service(
        directory_service=directory_service
    )
    platform_service = FakePlatformBridge(
        activated=True,
        # low-balance keeps the 4-point balance AND forces every grade to hit
        # the relay 402 boundary: the grade charge exceeds the balance, so the
        # fake pre-check rejects before any charge or Notion mutation.
        llm_points=4.0 if variant == "low-balance" else 100.0,
        points_charged=6.0 if variant == "low-balance" else 3.0,
        grade_delay=1.5 if variant == "grade-slow" else 0.0,
        grade_with_wrong_answer=variant == "grade-recovery",
        quiz_fail_status=502 if variant == "organize-blocked" else None,
        grade_fail_once=502 if variant == "grade-502" else None,
    )
    organizer_service = ExerciseOrganizer(
        directory_service=directory_service, relay=platform_service,
        page_adapter=_FakePages(), notes_provider=_FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
    )
    grading_service = make_fake_grading_service(
        directory_service, platform_service,
        fail_wrong_answer_preflight_once=variant == "grade-recovery",
    )
    return (directory_service, connection_service, platform_service,
            organizer_service, grading_service)


__all__ = ["build_fake_panel_services"]
