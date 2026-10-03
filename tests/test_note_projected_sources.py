"""Chapter evidence must follow what the projector showed, not word overlap.

A deck reuses the same vocabulary across dozens of pages, so lexical ranking
alone regularly cites a page from a different part of the lecture.  These
checks pin the behaviour that makes a citation mean "the class saw this page".
"""

from __future__ import annotations

from pku_sync import media


def _page(number: int, text: str, title: str = "ch02.pdf") -> dict:
    return {"title": title, "locator": f"第 {number} 页", "path": f"materials/{title}",
            "text": text, "status": "readable"}


def test_projected_pages_become_evidence_even_without_shared_wording():
    sources = [_page(1, "标题页"), _page(30, "RSA 公钥算法 1978 年"),
               _page(36, "置换密码的定义与例子")]
    projected = [{"title": "ch02.pdf", "page": 30, "seconds": 210.0, "certain": True}]
    rows = media._projected_source_pages(sources, projected)
    assert [row["locator"] for row in rows] == ["第 30 页"]
    assert rows[0]["projected"] is True


def test_a_page_the_matcher_could_not_separate_is_offered_without_the_mark():
    sources = [_page(30, "RSA 公钥算法"), _page(31, "RSA 公钥算法（续）")]
    projected = [{"title": "ch02.pdf", "page": 30, "seconds": 90.0, "certain": True},
                 {"title": "ch02.pdf", "page": 31, "seconds": 90.0, "certain": False}]
    rows = media._projected_source_pages(sources, projected)
    assert [row.get("projected") for row in rows] == [True, None]


def test_projected_marker_reaches_the_prompt():
    sources = [_page(30, "RSA 公钥算法 1978 年")]
    projected = [{"title": "ch02.pdf", "page": 30, "seconds": 210.0, "certain": True}]
    prompt = media._source_prompt(media._projected_source_pages(sources, projected))
    assert "这一页在本段课堂上投影显示过" in prompt
    # A page that was merely retrieved must not claim it was on screen.
    assert "这一页在本段课堂上投影显示过" not in media._source_prompt(sources)


def test_pages_outside_the_chapter_span_are_not_offered_as_candidates():
    sources = [_page(30, "RSA"), _page(93, "Vigenere 1553"), _page(123, "字母频率表")]
    allowed = {"ch02.pdf": set(range(25, 40))}
    kept = media._pages_within_projection(sources, allowed)
    assert [row["locator"] for row in kept] == ["第 30 页"]


def test_a_deck_with_no_frame_match_keeps_all_of_its_pages():
    sources = [_page(30, "RSA"), _page(4, "附录", title="ch01.pdf")]
    allowed = {"ch02.pdf": {30, 31}}
    kept = media._pages_within_projection(sources, allowed)
    assert {row["title"] for row in kept} == {"ch02.pdf", "ch01.pdf"}
    # Without any alignment at all nothing is filtered.
    assert media._pages_within_projection(sources, {}) == sources


def test_projected_pages_survive_the_page_budget_merge():
    """The merge that bounds prompt size must not drop the proven pages."""
    projected = media._projected_source_pages(
        [_page(30, "RSA 公钥算法")],
        [{"title": "ch02.pdf", "page": 30, "seconds": 210.0, "certain": True}])
    ranked = [_page(number, "其他内容 " * 40) for number in range(40, 60)]
    merged = media._merge_source_pages(projected, ranked)
    assert merged[0]["locator"] == "第 30 页"
    assert merged[0]["projected"] is True


def test_missing_alignment_leaves_chapter_evidence_unchanged():
    sources = [_page(30, "RSA"), _page(93, "Vigenere")]
    assert media._projected_source_pages(sources, []) == []
    assert media._pages_within_projection(sources, {}) == sources


def test_scoped_lexical_selection_does_not_apply_full_deck_position_bias():
    sources = [
        _page(20, "RSA 公钥算法 参数 计算 示例"),
        _page(90, "RSA 公钥算法 参数 计算 示例"),
    ]
    selected = media._select_source_pages(
        "RSA 公钥算法 参数 计算 示例", sources, position=0,
    )
    assert selected[0]["locator"] == "第 20 页"
    scoped = media._select_source_pages(
        "RSA 公钥算法 参数 计算 示例", [sources[1]], position=None,
    )
    assert scoped[0]["locator"] == "第 90 页"


def test_empty_projected_scope_can_fall_back_to_unscoped_evidence():
    sources = [_page(90, "RSA 公钥算法 参数 计算 示例")]
    allowed = {"ch02.pdf": {20, 30}}
    scoped = media._pages_within_projection(sources, allowed)
    assert scoped == []
    fallback = media._source_pages_for_window(
        {"start": 0, "text": "RSA 公钥算法 参数 计算 示例"},
        sources, [], scoped, 100,
    )
    assert fallback[0]["locator"] == "第 90 页"


def test_slide_timeline_is_skipped_without_frames_or_course(tmp_path):
    sources = [_page(30, "RSA")]
    assert media._lecture_slide_timeline([], tmp_path, sources, tmp_path,
                                         duration=100.0) == {}
    frames = [{"file": "frame_0000.jpg", "timestamp": 0.0}]
    assert media._lecture_slide_timeline(frames, tmp_path, sources, None,
                                         duration=100.0) == {}
    # A recording with no readable deck cannot be aligned either.
    assert media._lecture_slide_timeline(frames, tmp_path, [], tmp_path,
                                         duration=100.0) == {}
