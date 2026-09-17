"""Parser tests built from the exact markup and payloads course.pku.edu.cn returns.

The fixtures are trimmed copies of real responses. They exist because every
parser here reads undocumented HTML that changes without notice, and a silent
parse failure looks identical to a course with nothing published.
"""

from __future__ import annotations

import json

from pku_sync.materials import _embedded_attachments, _html_to_text, _parse_content_list
from pku_sync.models import Course, Recording
from pku_sync.recordings import _apply_subject_info, _parse_video_list
from pku_sync.store import safe_name

VIDEO_LIST_HTML = """
<table>
 <tr><th>名称</th><th>时间</th><th>教师</th><th>操作</th></tr>
 <tr>
   <td>2026-09-09第1-2节</td><td>2026-09-09 08:00:00</td><td>张老师</td>
   <td><a href="playVideo.action?token=abc&amp;course_id=_104128_1">播放</a></td>
 </tr>
 <tr>
   <td>2026-09-07第5-6节</td><td>2026-09-07 13:00:00</td><td>张老师</td>
   <td><a href="playVideo.action?token=def">播放</a></td>
 </tr>
</table>
"""

CONTENT_LIST_HTML = """
<ul id="content_listContainer" class="contentList">
  <li class="clearfix liItem read" id="contentListItem:_1680613_1">
    <img src="/i.gif" alt="项目">
    <h3><a href="#">The Linux Command Line</a></h3>
    <div class="details">
      <div class="contextItemDetailsHeaders clearfix">
        <div class="detailsLabel">已附加文件:</div>
        <div class="detailsValue">
          <ul class="attachments clearfix"><li>
            <a href="/bbcswebdav/pid-1680613-dt-content-rid-12901407_1/xid-12901407_1">
              <img src="/f.gif" alt="文件">TLCL-19.01.pdf
            </a>
            <span>( 2.022 MB )</span>
          </li></ul>
        </div>
      </div>
      <div class="vtbegenerated">William Shotts, The Linux Command Line</div>
    </div>
  </li>
  <li class="clearfix liItem" id="contentListItem:_1710226_1">
    <img src="/f.gif" alt="内容文件夹">
    <h3><a href="/webapps/blackboard/content/listContent.jsp?course_id=_102156_1&amp;content_id=_1710227_1">课件</a></h3>
  </li>
  <li class="clearfix liItem" id="contentListItem:_1708913_1">
    <img src="/a.gif" alt="作业">
    <h3><a href="/webapps/assignment/uploadAssignment?content_id=_1708913_1&amp;course_id=_101578_1">作业1 强化/惩罚 四象限</a></h3>
  </li>
</ul>
"""


class TestVideoList:
    def test_reads_title_time_and_teacher_from_labelled_columns(self):
        recordings = _parse_video_list(VIDEO_LIST_HTML, "_104128_1")
        assert [r.title for r in recordings] == ["2026-09-09第1-2节", "2026-09-07第5-6节"]
        assert recordings[0].recorded_at == "2026-09-09 08:00:00"
        assert recordings[0].teacher == "张老师"
        assert recordings[0].date == "2026-09-09"

    def test_unescapes_the_play_link_so_the_token_survives(self):
        play_url = _parse_video_list(VIDEO_LIST_HTML, "_104128_1")[0].play_url
        assert "&amp;" not in play_url
        assert play_url.startswith("playVideo.action?token=abc&course_id=")

    def test_header_row_is_not_mistaken_for_a_recording(self):
        assert len(_parse_video_list(VIDEO_LIST_HTML, "_104128_1")) == 2

    def test_narrow_layout_repeats_the_column_label_in_each_cell(self):
        html = """
        <table><tr>
          <td>名称: 第一讲</td><td>时间: 2026-09-09 08:00:00</td><td>教师: 李老师</td>
          <td><a href="playVideo.action?token=z">播放</a></td>
        </tr></table>
        """
        recording = _parse_video_list(html, "_1_1")[0]
        assert recording.title == "第一讲"
        assert recording.recorded_at == "2026-09-09 08:00:00"
        assert recording.teacher == "李老师"

    def test_a_row_without_a_play_link_is_skipped(self):
        assert _parse_video_list("<table><tr><td>暂无</td></tr></table>", "_1_1") == []


class TestContentList:
    def test_reads_attachment_url_filename_and_printed_size(self):
        items = [item for item, _ in _parse_content_list(CONTENT_LIST_HTML, "_103987_1", "课程资料")]
        attachment = items[0].attachments[0]
        assert attachment.filename == "TLCL-19.01.pdf"
        assert attachment.url.endswith("/xid-12901407_1")
        assert attachment.size == int(2.022 * 1024**2)

    def test_description_excludes_the_attached_file_preamble(self):
        items = [item for item, _ in _parse_content_list(CONTENT_LIST_HTML, "_103987_1", "课程资料")]
        assert items[0].body_text == "William Shotts, The Linux Command Line"

    def test_folder_yields_the_child_id_to_recurse_into(self):
        entries = _parse_content_list(CONTENT_LIST_HTML, "_102156_1", "教学内容")
        folder, child_id = entries[1]
        assert folder.kind == "内容文件夹"
        assert child_id == "_1710227_1"

    def test_leaf_items_report_no_child_to_recurse_into(self):
        entries = _parse_content_list(CONTENT_LIST_HTML, "_103987_1", "课程资料")
        assert entries[0][1] == ""
        assert entries[2][1] == ""

    def test_assignment_keeps_blackboards_own_kind_label(self):
        items = [item for item, _ in _parse_content_list(CONTENT_LIST_HTML, "_101578_1", "课程作业")]
        assert items[2].kind == "作业"

    def test_path_joins_the_parent_folder(self):
        items = [item for item, _ in _parse_content_list(CONTENT_LIST_HTML, "_102156_1", "教学内容")]
        assert items[1].path == "教学内容/课件"

    def test_missing_container_is_an_empty_folder_not_an_error(self):
        assert _parse_content_list("<html><body>无内容</body></html>", "_1_1", "x") == []


class TestSubjectInfo:
    def test_prefers_the_campus_hosted_playback_url(self):
        recording = Recording(course_id="_1_1", title="t")
        _apply_subject_info(
            recording,
            {
                "room_name": "二教411",
                "duration": "7182",
                "content": {
                    "playback": {"url": "https://resourcese.pku.edu.cn/a.mp4"},
                    "firm_source": {"contents": "https://vendor.example.com/b.mp4?token=x"},
                },
            },
        )
        assert recording.mp4_url == "https://resourcese.pku.edu.cn/a.mp4"
        assert recording.room == "二教411"
        assert recording.duration_seconds == 7182

    def test_falls_back_to_the_highest_quality_playlist(self):
        recording = Recording(course_id="_1_1", title="t")
        _apply_subject_info(
            recording,
            {
                "content": {
                    "resource": json.dumps(
                        {
                            "is_m3u8": 1,
                            "multi_path": {
                                "sd": "https://x/sd.m3u8",
                                "fhd": "https://x/fhd.m3u8",
                            },
                        }
                    )
                }
            },
        )
        assert recording.mp4_url == ""
        assert recording.m3u8_url == "https://x/fhd.m3u8"

    def test_unpublished_session_leaves_both_urls_empty(self):
        recording = Recording(course_id="_1_1", title="t")
        _apply_subject_info(recording, {"content": {}})
        assert recording.media_url == ""

    def test_a_non_numeric_duration_does_not_raise(self):
        recording = Recording(course_id="_1_1", title="t")
        _apply_subject_info(recording, {"duration": None, "content": {}})
        assert recording.duration_seconds == 0


class TestAnnouncementBodies:
    def test_block_tags_become_line_breaks(self):
        text = _html_to_text("<p>第一行</p><p>第二行<br>第三行</p>")
        assert text.splitlines() == ["第一行", "第二行", "第三行"]

    def test_embedded_image_uses_the_freshly_signed_url(self):
        html = '<p><img src="/bbcswebdav/xid-12963957_1?token=stale"></p>'
        fresh = {"12963957_1": "/bbcswebdav/courses/x/pic.png?token=valid"}
        attachment = _embedded_attachments(html, fresh)[0]
        assert attachment.url == "/bbcswebdav/courses/x/pic.png?token=valid"
        assert attachment.filename == "embedded-12963957_1"

    def test_an_unknown_xid_keeps_the_original_url(self):
        html = '<a href="/bbcswebdav/xid-999_1">讲义.pdf</a>'
        attachment = _embedded_attachments(html, {})[0]
        assert attachment.url == "/bbcswebdav/xid-999_1"
        assert attachment.filename == "讲义.pdf"


class TestTermSelection:
    def _settings(self, **overrides):
        from pku_sync.config import Settings

        return Settings(_env_file=None, **overrides)

    def _courses(self):
        return [
            Course(course_id="_1_1", name="本学期", code="26271-00048-04834210-1-00-1"),
            Course(course_id="_2_1", name="上学期", code="26261-00048-04834210-1-00-1"),
            Course(course_id="_3_1", name="无课号", code=""),
        ]

    def test_newest_term_is_detected_from_the_course_code(self):
        from pku_sync.discover import current_term

        assert current_term(self._courses()) == "26271"

    def test_older_terms_are_dropped_by_default(self):
        from pku_sync.discover import select_courses

        picked = select_courses(self._courses(), self._settings())
        assert [c.course_id for c in picked] == ["_1_1", "_3_1"]

    def test_include_past_terms_keeps_everything(self):
        from pku_sync.discover import select_courses

        picked = select_courses(self._courses(), self._settings(include_past_terms=True))
        assert len(picked) == 3

    def test_an_allowlist_overrides_term_filtering(self):
        from pku_sync.discover import select_courses

        picked = select_courses(self._courses(), self._settings(course_allowlist="_2_1"))
        assert [c.course_id for c in picked] == ["_2_1"]

    def test_denylist_removes_a_current_term_course(self):
        from pku_sync.discover import select_courses

        picked = select_courses(self._courses(), self._settings(course_denylist="_1_1"))
        assert "_1_1" not in [c.course_id for c in picked]

    def test_shell_term_variable_does_not_leak_into_course_selection(self, monkeypatch):
        """`TERM=xterm` used to match every course out of the current term."""
        from pku_sync.config import Settings

        monkeypatch.setenv("TERM", "xterm-256color")
        assert Settings(_env_file=None).course_term == ""


class TestDirectoryAssignment:
    def test_a_unique_name_becomes_the_directory_unchanged(self):
        from pku_sync.discover import assign_directories

        courses = [Course(course_id="_104128_1", name="计算机网络(26-27学年第1学期)")]
        assign_directories(courses)
        assert courses[0].directory == "计算机网络_26-27学年第1学期"

    def test_two_sections_sharing_a_name_get_separate_directories(self):
        from pku_sync.discover import assign_directories

        courses = [
            Course(course_id="_101578_1", name="认知心理学(26-27学年第1学期)"),
            Course(course_id="_101577_1", name="认知心理学(26-27学年第1学期)"),
        ]
        assign_directories(courses)
        assert courses[0].directory != courses[1].directory
        assert courses[0].directory.endswith("_101578")
        assert courses[1].directory.endswith("_101577")

    def test_the_suffix_does_not_depend_on_which_courses_are_selected(self):
        from pku_sync.discover import assign_directories, select_courses

        def build():
            return [
                Course(course_id="_101578_1", name="认知心理学", code="26271-a"),
                Course(course_id="_101577_1", name="认知心理学", code="26271-b"),
            ]

        everything = assign_directories(build())
        from pku_sync.config import Settings

        one = select_courses(
            assign_directories(build()), Settings(_env_file=None, course_allowlist="_101577_1")
        )
        assert one[0].directory == everything[1].directory


class TestSafeName:
    def test_path_separators_and_control_characters_are_replaced(self):
        assert safe_name("作业1 强化/惩罚 四象限") == "作业1 强化_惩罚 四象限"

    def test_windows_reserved_names_get_a_prefix(self):
        assert safe_name("NUL.txt") == "_NUL.txt"

    def test_a_long_name_keeps_its_extension(self):
        name = safe_name("讲" * 200 + ".pdf")
        assert name.endswith(".pdf")
        assert len(name) <= 120

    def test_empty_input_falls_back(self):
        assert safe_name("   ", fallback="attachment") == "attachment"


class TestManifest:
    def test_a_recorded_file_is_current_until_its_source_id_changes(self, tmp_path):
        from pku_sync.manifest import Manifest

        target = tmp_path / "a.pdf"
        target.write_bytes(b"data")
        manifest = Manifest(tmp_path / "manifest.json")
        manifest.record("a.pdf", "/bbcswebdav/xid-1_1", b"data")

        assert manifest.is_current("a.pdf", "/bbcswebdav/xid-1_1", tmp_path)
        assert not manifest.is_current("a.pdf", "/bbcswebdav/xid-2_1", tmp_path)

    def test_a_deleted_file_is_no_longer_current(self, tmp_path):
        from pku_sync.manifest import Manifest

        manifest = Manifest(tmp_path / "manifest.json")
        manifest.record("gone.pdf", "u", b"data")
        assert not manifest.is_current("gone.pdf", "u", tmp_path)

    def test_it_survives_a_truncated_file_from_an_interrupted_run(self, tmp_path):
        from pku_sync.manifest import Manifest

        path = tmp_path / "manifest.json"
        path.write_text("{not json", "utf-8")
        assert Manifest(path).entries == {}

    def test_saved_entries_reload(self, tmp_path):
        from pku_sync.manifest import Manifest

        path = tmp_path / "manifest.json"
        first = Manifest(path)
        first.record("a.pdf", "u", b"data")
        first.save()
        assert Manifest(path).entries["a.pdf"]["source_url"] == "u"


class TestNoteWindows:
    def test_segments_are_grouped_into_fixed_length_windows(self):
        from pku_sync.media import _windows

        segments = [
            {"start": 0, "end": 10, "text": "一"},
            {"start": 100, "end": 110, "text": "二"},
            {"start": 500, "end": 510, "text": "三"},
        ]
        windows = _windows(segments, window_seconds=480)
        assert [w["text"] for w in windows] == ["一 二", "三"]
        assert windows[0]["end"] == 110

    def test_silent_segments_do_not_create_empty_windows(self):
        from pku_sync.media import _windows

        assert _windows([{"start": 0, "end": 1, "text": ""}], 480) == []
