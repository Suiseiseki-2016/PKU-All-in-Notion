"""Semester-scoped, ambiguity-safe hub discovery (VAL-META-001).

The runbook discipline (daily_review_mcp.md §2): search 'Class Notes', scope
to the current semester, and STOP with the candidates listed when the result
is not unique. Ids are never hardcoded — every user's workspace differs.
"""

from __future__ import annotations

from ..notion import page_title, page_url
from .entities import HubInfo
from .errors import HubAmbiguityError, HubNotFoundError
from .titles import current_semester_label, is_hub_family, matches_semester, normalize_title

SEARCH_QUERY = "Class Notes"


def discover_hub(client, *, semester: str | None = None, now=None) -> HubInfo:
    """Resolve exactly one current-semester 'Class Notes …' hub.

    The real workspace also holds an older-semester hub ('Clash Notes
    2026上半学期', typo included), and Notion search is fuzzy — so the family
    and semester filters run client-side over the whole result set.
    """
    label = semester or current_semester_label(now)
    candidates: list[tuple[dict, str]] = []
    for page in client.search(SEARCH_QUERY, object_type="page"):
        raw_title = page_title(page)
        if not raw_title:
            continue
        title = normalize_title(raw_title)
        if not is_hub_family(title):
            continue
        if not matches_semester(title, label):
            continue
        candidates.append((page, title))
    if len(candidates) == 1:
        page, title = candidates[0]
        return HubInfo(id=page["id"], url=page_url(page), title=title)
    if candidates:
        raise HubAmbiguityError(label, [title for _, title in candidates])
    raise HubNotFoundError(label)
