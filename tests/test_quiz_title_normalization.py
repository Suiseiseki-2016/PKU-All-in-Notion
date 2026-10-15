"""Preserved normalization contract used by current quiz parsing."""
from pku_sync.panel import quiz_contract as titles


def test_strip_icon_and_normalize():
    assert titles.strip_icon("🌐 计算机网络") == "计算机网络"
    assert titles.strip_icon("📚 2026秋季学期学习中心") == "2026秋季学期学习中心"
    assert titles.normalize_title("🌐 计算机网络") == "计算机网络"
    # whitespace variants (ASCII + full-width U+3000) collapse for exact matching
    assert titles.normalize_title(" 计算机网络\u3000") == "计算机网络"
    assert titles.normalize_title("认知心理学  实验\u3000手册") == "认知心理学 实验 手册"
    # CJK brackets are punctuation, not icons — kept
    assert titles.normalize_title("《课程总结》") == "《课程总结》"
