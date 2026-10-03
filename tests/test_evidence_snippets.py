"""The model may select evidence, but may not author its quotation text."""

import json

import pytest

from pku_sync.evidence_snippets import (
    build_evidence_snippet_catalog,
    resolve_evidence_choice_ids,
    resolve_snippet_ids,
)


def _catalog(blocks=None, pages=None, **kwargs):
    return build_evidence_snippet_catalog(blocks or [], pages or [], **kwargs)


def test_bullet_separated_pdf_lines_are_never_joined() -> None:
    text = "• 单表代换密码\n• 凯撒密码\n加密密钥3\n图示结束"
    catalog = _catalog(pages=[{"title": "ch02.pdf", "locator": "第 7 页",
                               "status": "readable", "text": text}])
    snippets = catalog["snippets"]
    quotes = [row["quote"] for row in snippets]
    assert "• 单表代换密码" in quotes
    assert "• 凯撒密码" in quotes
    assert "加密密钥3" in quotes
    assert all("单表代换密码" not in quote or "凯撒密码" not in quote
               for quote in quotes)
    assert all(text[row["start"]:row["end"]] == row["quote"]
               for row in snippets)
    assert {row["locator"] for row in snippets} == {"第 7 页"}


def test_short_numeric_label_is_not_padded_with_neighbor() -> None:
    text = "加密密钥3\n解密密钥7"
    catalog = _catalog(pages=[{"text": text}])
    assert [row["quote"] for row in catalog["snippets"]] == [
        "加密密钥3", "解密密钥7"]


def test_support_bundle_keeps_related_short_lines_exact_and_traceable() -> None:
    text = "加密密钥3\n解密密钥3\nA 明文\nD 密文"
    catalog = _catalog(pages=[{"title": "ch02.pdf", "locator": "第 8 页",
                               "status": "readable", "text": text}])
    bundle = next(
        row for row in catalog["support_bundles"]
        if row["members"][:2] == ["加密密钥3", "解密密钥3"]
    )
    assert text[bundle["start"]:bundle["end"]] == bundle["quote"]
    assert bundle["members"][:2] == ["加密密钥3", "解密密钥3"]
    assert resolve_evidence_choice_ids(catalog, [bundle["id"]]) == [
        {"id": "S1", "quote": bundle["quote"]}]


def test_stable_ids_and_exact_resolution() -> None:
    blocks = [{"block_id": "B0001", "text": "凯撒密码使用字母位移。"}]
    first = _catalog(blocks=blocks)
    second = _catalog(blocks=blocks)
    assert first == second
    item = first["snippets"][0]
    assert resolve_snippet_ids(first, [item["id"]]) == [
        {"id": "B0001", "quote": "凯撒密码使用字母位移。"}]
    with pytest.raises(ValueError, match="unknown"):
        resolve_snippet_ids(first, ["Eforged"])
    with pytest.raises(ValueError, match="duplicate"):
        resolve_snippet_ids(first, [item["id"], item["id"]])


def test_long_line_is_split_into_contiguous_bounded_spans() -> None:
    text = "计算过程先取模运算的余数，然后继续代入下一行。" * 10
    catalog = _catalog(blocks=[{"block_id": "B2", "text": text}],
                       max_snippet_chars=40)
    assert catalog["snippets"]
    assert all(len(row["quote"]) <= 40 for row in catalog["snippets"])
    assert all(text[:220][row["start"]:row["end"]] == row["quote"]
               for row in catalog["snippets"])


def test_payload_budget_is_hard_and_query_only_changes_selection() -> None:
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": "\n".join(f"第{i}条内容：凯撒密码示例数字{i}。" for i in range(60))}]
    small = _catalog(pages=pages, max_payload_chars=600,
                     query="凯撒密码 示例数字58")
    full = _catalog(pages=pages, max_payload_chars=20000)
    assert small["truncated"]
    assert small["payload_chars"] <= 600
    assert small["eligible_count"] == full["eligible_count"]
    assert {row["id"] for row in small["snippets"]} <= {
        row["id"] for row in full["snippets"]}
    assert any("58" in row["quote"] for row in small["snippets"])
    assert len(json.dumps(
        {"snippets": small["snippets"],
         "support_bundles": small["support_bundles"]},
        ensure_ascii=False, separators=(",", ":"))) == small["payload_chars"]


def test_transcript_and_page_provenance_do_not_collide() -> None:
    catalog = _catalog(
        blocks=[{"block_id": "B0001", "text": "老师讲了课堂示例。"}],
        pages=[{"title": "ch02.pdf", "locator": "第 7 页",
                "status": "partial", "text": "页面展示凯撒密码。"}])
    assert len({row["id"] for row in catalog["snippets"]}) == 2
    assert {row["source_kind"] for row in catalog["snippets"]} == {
        "transcript", "page"}
    assert next(row for row in catalog["snippets"]
                if row["source_kind"] == "page")["status"] == "partial"
