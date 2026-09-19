"""Additional launch recovery contract coverage (additive M3 tests)."""

from __future__ import annotations

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js"


def test_launch_feedback_is_cleared_when_navigating_to_another_target():
    script = APP_JS.read_text(encoding="utf-8")

    select_start = script.index("  function selectLecture(")
    select_end = script.index("\n  function setMaterialView", select_start)
    select_body = script[select_start:select_end]
    assert "state.launch = null;" in select_body

    course_start = script.index("  function openCourse(")
    course_end = script.index("\n  function selectLecture", course_start)
    course_body = script[course_start:course_end]
    assert "state.launch = null;" in course_body


def test_missing_course_fallback_requires_a_stored_page_url():
    from pku_sync.panel.directory import DirectoryService
    from pku_sync.panel.fake_directory import PanelDirectoryProvider
    from pku_sync.panel.fake_workspace import COURSE_NET, NET_L2, build_panel_workspace

    class MissingCourseUrlProvider(PanelDirectoryProvider):
        def load(self):
            data = super().load()
            for course in data.courses:
                if course.id == COURSE_NET:
                    course.url = ""
            for lecture in data.lectures:
                if lecture.id == NET_L2:
                    lecture.url = ""
            return data

    service = DirectoryService(MissingCourseUrlProvider(build_panel_workspace()))
    service.load()
    result = service.resolve_launch(NET_L2, course_id=COURSE_NET)

    assert result.url is None
    assert result.fallback is None
