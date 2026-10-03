"""Narrow checks for slide quotations that drift into a different lecture.

A course file can be correctly associated with a recording while containing
pages from an earlier class.  A valid page citation alone therefore does not
prove that a named quotation belongs in the current chapter.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping


_PERSON = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2}\b")
_QUOTE_CUE = re.compile(r"观点|名言|引述|引用|曾说|指出|认为|强调|主张|提到")
_SENTENCE = re.compile(r"[^。！？\n]+[。！？]?", re.UNICODE)
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]{5,}")


def _spoken_topic_anchor(claim: str, transcript: str) -> bool:
    """Find a distinctive quoted phrase without treating generic words as one."""
    spoken = re.sub(r"\s+", "", transcript)
    for run in _HAN_RUN.findall(claim):
        for index in range(len(run) - 5):
            if run[index:index + 6] in spoken:
                return True
    return False


def unspoken_slide_quote_issues(
    summary: str,
    transcript: str,
    sources: list[dict],
    *,
    person_aliases: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, str]]:
    """Report named slide quotations with no anchor in this chapter's speech.

    Examine a quoted name and its cited page within the same paragraph, up to
    two sentences apart.  Models often put the attribution in one sentence and
    the page citation at the end of the next.  Other slide material is left to
    normal source checks.  An issue is diagnostic, not proof the slide is wrong:
    callers should require review or focused repair before publication.
    """
    issues: list[dict[str, str]] = []
    spoken = transcript.casefold()
    aliases = person_aliases or {}
    for paragraph in re.split(r"\n\s*\n", summary):
        sentences = [match.group(0).strip() for match in _SENTENCE.finditer(paragraph)
                     if match.group(0).strip()]
        for citation_index, cited_sentence in enumerate(sentences):
            if "课件：" not in cited_sentence:
                continue
            for row in sources:
                title = str(row.get("title") or "")
                locator = str(row.get("locator") or "")
                if not title or not locator or not row.get("text"):
                    continue
                if f"课件：{title}，{locator}" not in cited_sentence:
                    continue
                source_text = str(row["text"])
                # A quotation may span two prose sentences before its trailing
                # citation.  Do not pull names from a distant paragraph topic.
                for person_index in range(max(0, citation_index - 1), citation_index + 1):
                    first_sentence = sentences[person_index]
                    if not _QUOTE_CUE.search(first_sentence):
                        continue
                    for person_match in _PERSON.finditer(first_sentence):
                        person = person_match.group(0)
                        if not re.search(rf"(?<!\w){re.escape(person)}(?!\w)", source_text, re.I):
                            continue
                        names = [person, person.split()[-1], *aliases.get(person, ())]
                        if any(name and name.casefold() in spoken for name in names):
                            continue
                        claim_span = "".join(sentences[person_index:citation_index + 1])
                        # The teacher may discuss the quotation without naming
                        # its author.  Require a continuous six-Han-character
                        # phrase, not a generic two-character topic overlap.
                        without_citation = claim_span.split("（课件：", 1)[0]
                        if _spoken_topic_anchor(without_citation, transcript):
                            continue
                        issues.append({
                            "reason": "named_slide_quote_not_heard_in_chapter",
                            "person": person,
                            "source_title": title,
                            "source_locator": locator,
                            "sentence": claim_span,
                        })
    return issues
