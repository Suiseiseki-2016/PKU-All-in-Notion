"""Read-only, conservative checks of note citations against extracted page text.

This is a review aid, not a proof of factual correctness. In particular, an
unmatched subject or ambiguous page context produces no year verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


_CITATION = re.compile(
    r"（课件：(?P<title>[^，（）]+)，第\s*(?P<page>\d{1,4})\s*页"
    r"(?:；原文：[“\"](?P<quote>[^”\"]+)[”\"])?）"
)
_YEAR = re.compile(r"(?<!\d)(?:1[5-9]\d{2}|20\d{2})(?!\d)")
_NAMED = re.compile(
    r"[\u4e00-\u9fff]{1,5}·[\u4e00-\u9fff]{1,5}"
    r"|(?<![A-Za-z])[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+(?![A-Za-z])"
)


def _claim_before(note: str, citation_start: int) -> str:
    """Return only the local clause that the citation appears to support."""
    prefix = note[:citation_start]
    boundary = max(prefix.rfind(mark) for mark in "。！？；\n")
    return prefix[boundary + 1:].strip()


def _names_near_year(claim: str, aliases: Mapping[str, str]) -> list[tuple[str, str]]:
    """Find explicit named subjects linked to a single year in the claim."""
    years = list(_YEAR.finditer(claim))
    if len(years) != 1:
        return []
    year = years[0]
    candidates = [(key, aliases[key]) for key in aliases if key in claim]
    candidates += [(m.group(), m.group()) for m in _NAMED.finditer(claim)]
    linked = []
    for written, source_name in candidates:
        for match in re.finditer(re.escape(written), claim, re.I):
            gap = (year.start() - match.end() if match.end() <= year.start()
                   else match.start() - year.end())
            if 0 <= gap <= 12 and source_name:
                linked.append((written, source_name))
                break
    return list(dict.fromkeys(linked))


def _local_page_years(page_text: str, subject: str) -> set[str]:
    """Use the subject's line, rather than any year on the page."""
    years: set[str] = set()
    for line in page_text.splitlines():
        for match in re.finditer(re.escape(subject), line, re.I):
            # A date on the opposite end of a long line is not evidence for
            # this subject. PDF extraction usually preserves slide lines.
            start = max(0, match.start() - 45)
            end = min(len(line), match.end() + 45)
            years.update(_YEAR.findall(line[start:end]))
    return years


def review_source_citations(
    note: str,
    sources: Sequence[dict],
    *,
    subject_aliases: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return concrete citation issues for manual review.

    ``subject_aliases`` maps a name in the note to its exact spelling on the
    page (for example a Chinese transliteration to a Latin name). The mapping
    must come from known material, not a guessed translation. A year conflict
    is reported only when one claim year and one different year are explicitly
    bound to the same named subject on the cited readable page.
    """
    aliases = subject_aliases or {}
    pages = {(str(row.get("title") or ""), str(row.get("locator") or "")): row
             for row in sources}
    issues: list[dict[str, str]] = []
    for citation in _CITATION.finditer(note):
        title = citation.group("title").strip()
        locator = f"第 {int(citation.group('page'))} 页"
        claim = _claim_before(note, citation.start())
        row = pages.get((title, locator))
        if row is None or row.get("status", "readable") != "readable" or not row.get("text"):
            issues.append({"code": "page_not_readable", "citation": citation.group(),
                           "claim": claim, "detail": "Cited page is absent or not readable."})
            continue
        quote = citation.group("quote")
        if quote and re.sub(r"\s+", "", quote) not in re.sub(r"\s+", "", str(row["text"])):
            issues.append({"code": "quote_not_on_page", "citation": citation.group(),
                           "claim": claim, "detail": "Quoted source text is absent from the cited page."})
        claim_years = _YEAR.findall(claim)
        if len(claim_years) != 1:
            continue
        for written, source_name in _names_near_year(claim, aliases):
            page_years = _local_page_years(str(row["text"]), source_name)
            if len(page_years) == 1 and claim_years[0] not in page_years:
                issues.append({
                    "code": "subject_year_conflict",
                    "citation": citation.group(),
                    "claim": claim,
                    "detail": (f"{written} is dated {claim_years[0]} in the note; "
                               f"the cited page places {source_name} with {next(iter(page_years))}."),
                })
                break
    return issues
