from pku_sync.citation_review import review_source_citations


PAGE_29 = {
    "title": "ch02-古典密码.pdf", "locator": "第 29 页", "status": "readable",
    "text": (
        "29\n密码学的起源和发展-III\n◼ 1949～1975年:\n"
        "➢ 1949年Shannon的“The Communication Theory of Secret Systems”\n"
        "➢ 1967年David Kahn的《The Codebreakers： The Comprehensive\n"
        "History of Secret Communication from Ancient Times to the Internet》\n"
        "➢ 1971-73年IBM Watson实验室的Horst Feistel等的几篇技术报告"
    ),
}


def test_fullv6_kahn_year_uses_subject_span_not_page_heading():
    note = ("大卫·卡恩于 1975 年出版《破译者》，系统梳理了保密通信历史"
            "（课件：ch02-古典密码.pdf，第 29 页）。")
    issues = review_source_citations(
        note, [PAGE_29], subject_aliases={"大卫·卡恩": "David Kahn"})
    assert len(issues) == 1
    assert issues[0]["code"] == "subject_year_conflict"
    assert "1967" in issues[0]["detail"]


def test_matching_subject_year_and_unrelated_heading_are_not_flagged():
    note = ("大卫·卡恩于 1967 年出版《破译者》"
            "（课件：ch02-古典密码.pdf，第 29 页）。")
    assert review_source_citations(
        note, [PAGE_29], subject_aliases={"大卫·卡恩": "David Kahn"}) == []


def test_no_alias_or_ambiguous_subject_year_fails_closed():
    note = ("大卫·卡恩于 1975 年出版《破译者》"
            "（课件：ch02-古典密码.pdf，第 29 页）。")
    assert review_source_citations(note, [PAGE_29]) == []
    ambiguous = {**PAGE_29, "text": PAGE_29["text"] + "\n1975年David Kahn修订"}
    assert review_source_citations(
        note, [ambiguous], subject_aliases={"大卫·卡恩": "David Kahn"}) == []


def test_absent_or_unreadable_page_is_a_review_issue():
    note = "术语见课件（课件：missing.pdf，第 4 页）。"
    assert review_source_citations(note, [PAGE_29])[0]["code"] == "page_not_readable"
    unreadable = {**PAGE_29, "status": "partial"}
    cited = "术语见课件（课件：ch02-古典密码.pdf，第 29 页）。"
    assert review_source_citations(cited, [unreadable])[0]["code"] == "page_not_readable"


def test_verbatim_quote_missing_from_real_page_is_flagged():
    cited = ("页上写着别的内容"
             "（课件：ch02-古典密码.pdf，第 29 页；原文：“这句话不在该页”）。")
    assert review_source_citations(cited, [PAGE_29])[0]["code"] == "quote_not_on_page"
