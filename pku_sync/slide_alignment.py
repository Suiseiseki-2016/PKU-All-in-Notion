"""Locate the slide page that was actually projected at each point of a lecture.

Lexical retrieval cannot reliably tell which deck page a chapter belongs to.
The transcript rarely repeats a page's wording, and a deck reuses the same
terms across dozens of pages, so ranking pages by shared vocabulary picks a
page that merely sounds similar.  Notes built that way cite a page the class
never saw.

Matching saved video frames against rendered deck pages answers the question
mechanically instead: a page citation can then name the page a student
actually looked at.  Matching is deliberately conservative — a frame that does
not clearly correspond to a page yields no page at all, because a confident
wrong citation is worse for a student than a missing one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Compare size. Large enough to keep slide layout and heading structure,
# small enough that a whole deck stays in memory as one array.
_SIZE = (256, 144)
# Render above the compare size and downsample with area averaging. Rendering
# straight to the compare size aliases thin slide text into noise, which makes
# every page look equally (dis)similar to a projected frame.
_RENDER_SCALE = 0.5
# A projector washes out colour and contrast, so brightness and gain carry no
# information. These bounds are on 1 - normalized correlation of edge maps.
_ACCEPT_DISTANCE = 0.55
# Pages within this distance of the best one are usually animation builds of
# the same slide exported as consecutive PDF pages. A deck also repeats layouts
# far apart, so a near tie only counts as the same slide when it is nearby;
# otherwise a coincidental resemblance would reopen a page the lecture had
# long left behind.
_TIE_DISTANCE = 0.05
_TIE_PAGE_SPAN = 3
# A deck advances; revisiting an earlier page happens but is less common.
_FORWARD_COST = 0.004
_BACKWARD_COST = 0.02
_DECK_SWITCH_COST = 0.15
# Matches note_sources._PAGE_LIMIT: pages past it are not read as evidence
# either, so aligning to them would produce uncitable results.
_PAGE_LIMIT = 256

ALIGNMENT_FILENAME = "slide-alignment.json"
_ALIGNMENT_VERSION = 1


def _signature(gray) -> "object":
    """Contrast-normalized edge map, flattened.

    Edges survive projector washout, screen colour shifts and JPEG artefacts,
    while raw grey levels do not: a dim capture of the right page scores worse
    than a bright capture of an unrelated one.
    """
    import cv2
    import numpy as np

    resized = cv2.resize(gray, _SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)
    gx = cv2.Sobel(resized, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(resized, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.sqrt(gx * gx + gy * gy)
    magnitude -= magnitude.mean()
    deviation = float(magnitude.std())
    if deviation <= 1e-6:
        return np.zeros(magnitude.size, dtype=np.float32)
    return (magnitude / deviation).reshape(-1)


def _read_gray(path: Path):
    """Decode an image through Python file APIs.

    cv2.imread uses a narrow-char path on Windows and silently fails for the
    Chinese course directories this pipeline writes into.
    """
    import cv2
    import numpy as np

    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)


def render_deck(pdf: Path, *, page_limit: int = _PAGE_LIMIT):
    """Return one signature per rendered page of a slide deck."""
    import cv2
    import numpy as np
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf))
    try:
        signatures = []
        for number in range(min(len(document), page_limit)):
            page = document[number]
            bitmap = page.render(scale=_RENDER_SCALE)
            try:
                gray = cv2.cvtColor(bitmap.to_numpy(), cv2.COLOR_RGB2GRAY)
                signatures.append(_signature(gray))
            finally:
                bitmap.close()
                page.close()
    finally:
        document.close()
    if not signatures:
        return np.zeros((0, _SIZE[0] * _SIZE[1]), dtype=np.float32)
    return np.stack(signatures)


def _transition_costs(deck_ids):
    """Cost of moving from one page to another between consecutive frames."""
    import numpy as np

    decks = np.asarray(deck_ids)
    position = np.arange(decks.size)
    delta = position[None, :] - position[:, None]
    same_deck = decks[:, None] == decks[None, :]
    within = np.where(delta >= 0, _FORWARD_COST * delta, _BACKWARD_COST * -delta)
    return np.where(same_deck, within, _DECK_SWITCH_COST).astype(np.float32)


def _viterbi(cost, deck_ids):
    """Pick the page sequence that best explains the frames in deck order."""
    import numpy as np

    frames, pages = cost.shape
    if frames == 0 or pages == 0:
        return np.zeros(frames, dtype=np.int32)
    step = _transition_costs(deck_ids)
    back = np.zeros((frames, pages), dtype=np.int32)
    total = cost[0].astype(np.float32).copy()
    columns = np.arange(pages)
    for index in range(1, frames):
        candidates = total[:, None] + step
        back[index] = np.argmin(candidates, axis=0)
        total = candidates[back[index], columns] + cost[index]
    chosen = np.zeros(frames, dtype=np.int32)
    chosen[-1] = int(np.argmin(total))
    for index in range(frames - 1, 0, -1):
        chosen[index - 1] = back[index, chosen[index]]
    return chosen


def align_signatures(frame_signatures, page_signatures, deck_ids,
                     *, accept_distance: float = _ACCEPT_DISTANCE):
    """Match frames to pages, leaving unclear frames unassigned.

    Only frames that already resemble a page on their own take part in the
    ordering pass.  A lecturer's camera shot or a desktop window would
    otherwise be pulled onto whichever page its neighbours use.
    """
    import numpy as np

    frames = np.asarray(frame_signatures, dtype=np.float32)
    pages = np.asarray(page_signatures, dtype=np.float32)
    if frames.size == 0 or pages.size == 0:
        return [None] * len(frame_signatures), np.zeros(len(frame_signatures))
    # Signatures are zero-mean and unit-variance, so the mean product is the
    # normalized correlation and 1 - it behaves as a distance in [0, 2].
    cost = 1.0 - (frames @ pages.T) / frames.shape[1]
    best = cost.min(axis=1)
    confident = np.flatnonzero(best <= accept_distance)
    assigned: list[int | None] = [None] * frames.shape[0]
    if confident.size:
        chosen = _viterbi(cost[confident], deck_ids)
        for frame_index, page_index in zip(confident, chosen):
            # The ordering pass may only reorder among plausible pages; it must
            # not move a frame onto a page it does not resemble at all.
            if cost[frame_index, page_index] <= accept_distance:
                assigned[int(frame_index)] = int(page_index)
            else:
                assigned[int(frame_index)] = int(np.argmin(cost[frame_index]))
    return assigned, cost


def _deck_paths(sources: list[dict], course_directory: Path) -> list[tuple[str, Path]]:
    """Slide files that were explicitly matched to this recording."""
    decks: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for row in sources:
        title = str(row.get("title") or "")
        relative = str(row.get("path") or "")
        if not title or not relative or title in seen:
            continue
        if not title.lower().endswith(".pdf"):
            continue
        if row.get("status") not in {"readable", "partial", "truncated"}:
            continue
        path = Path(course_directory) / relative
        if path.is_file():
            decks.append((title, path))
            seen.add(title)
    return decks


def build_timeline(keyframes: list[dict], keyframe_directory: Path,
                   sources: list[dict], course_directory: Path,
                   *, duration: float | None = None) -> dict:
    """Return which deck page was on screen over each span of the recording.

    Saved keyframes are sparse — a lecture yields a few dozen images for a few
    hundred slide changes — so the result has two parts.  ``anchors`` are the
    frames that clearly show one page.  ``decks`` records each deck's length so
    callers can bound the pages that could have been shown between anchors.
    """
    empty: dict = {"decks": {}, "anchors": []}
    decks = _deck_paths(sources, Path(course_directory))
    if not keyframes or not decks:
        return empty
    try:
        import numpy as np
    except ImportError:
        return empty

    page_signatures = []
    page_labels: list[tuple[str, int]] = []
    deck_ids: list[int] = []
    deck_lengths: dict[str, int] = {}
    for deck_index, (title, path) in enumerate(decks):
        try:
            rendered = render_deck(path)
        except Exception as exc:
            logger.warning("slide deck %s could not be rendered: %s", title,
                           type(exc).__name__)
            continue
        deck_lengths[title] = len(rendered)
        for number in range(len(rendered)):
            page_signatures.append(rendered[number])
            page_labels.append((title, number + 1))
            deck_ids.append(deck_index)
    if not page_signatures:
        return empty

    frame_signatures = []
    frame_rows: list[dict] = []
    for row in keyframes:
        name = str(row.get("file") or "")
        if not name:
            continue
        gray = _read_gray(Path(keyframe_directory) / name)
        if gray is None:
            continue
        frame_signatures.append(_signature(gray))
        frame_rows.append(row)
    if not frame_signatures:
        return empty

    assigned, cost = align_signatures(np.stack(frame_signatures),
                                      np.stack(page_signatures), deck_ids)
    anchors: list[dict] = []
    for position, (row, page_index) in enumerate(zip(frame_rows, assigned)):
        if page_index is None:
            continue
        start = float(row.get("timestamp") or 0.0)
        end = duration if duration else start
        for later in frame_rows[position + 1:]:
            later_start = float(later.get("timestamp") or 0.0)
            if later_start > start:
                end = later_start
                break
        title, page = page_labels[page_index]
        distance = float(cost[position, page_index])
        near = sorted({page_labels[other][1]
                       for other in np.flatnonzero(
                           cost[position] <= distance + _TIE_DISTANCE)
                       if page_labels[other][0] == title
                       and abs(page_labels[other][1] - page) <= _TIE_PAGE_SPAN})
        anchors.append({
            "start": round(start, 2),
            "end": round(float(end), 2),
            "title": title,
            "page": page,
            "distance": round(distance, 4),
            "also": [number for number in near if number != page][:4],
        })
    return {"decks": deck_lengths, "anchors": anchors,
            "duration": round(float(duration), 2) if duration else 0.0}


def load_or_build_timeline(keyframes: list[dict], keyframe_directory: Path,
                           sources: list[dict], course_directory: Path,
                           *, duration: float | None = None) -> dict:
    """Reuse a stored alignment so a resumed lecture does not re-render decks."""
    directory = Path(keyframe_directory)
    stored = directory / ALIGNMENT_FILENAME
    titles = sorted({title for title, _ in _deck_paths(sources, Path(course_directory))})
    try:
        payload = json.loads(stored.read_text("utf-8"))
        if (payload.get("version") == _ALIGNMENT_VERSION
                and sorted(payload.get("decks") or {}) == titles
                and payload.get("frames") == len(keyframes)):
            return {"decks": payload.get("decks") or {},
                    "anchors": list(payload.get("anchors") or []),
                    "duration": payload.get("duration") or 0.0}
    except (OSError, ValueError):
        pass
    timeline = build_timeline(keyframes, directory, sources, course_directory,
                              duration=duration)
    if timeline.get("anchors"):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = stored.with_name(stored.name + ".tmp")
            temporary.write_text(json.dumps(
                {"version": _ALIGNMENT_VERSION, "frames": len(keyframes), **timeline},
                ensure_ascii=False, indent=1), "utf-8")
            temporary.replace(stored)
        except OSError as exc:
            logger.warning("slide alignment could not be saved: %s", type(exc).__name__)
    return timeline


def pages_on_screen(timeline: dict, start: float, end: float) -> list[dict]:
    """Pages proven to be projected during a span, most-shown first.

    Neighbouring pages that the matcher could not separate are reported
    alongside, because a deck exports one animated slide as several nearly
    identical pages.
    """
    shown: dict[tuple[str, int], float] = {}
    tied: dict[tuple[str, int], float] = {}
    for row in timeline.get("anchors") or []:
        row_start = float(row.get("start", 0.0))
        row_end = float(row.get("end", row_start))
        overlap = min(end, row_end) - max(start, row_start)
        if overlap <= 0:
            continue
        title = str(row.get("title") or "")
        key = (title, int(row.get("page") or 0))
        shown[key] = shown.get(key, 0.0) + overlap
        for number in row.get("also") or []:
            alternate = (title, int(number))
            tied[alternate] = tied.get(alternate, 0.0) + overlap
    result = [{"title": title, "page": page, "seconds": round(seconds, 1),
               "certain": True}
              for (title, page), seconds in shown.items()]
    result += [{"title": title, "page": page, "seconds": round(seconds, 1),
                "certain": False}
               for (title, page), seconds in tied.items() if (title, page) not in shown]
    result.sort(key=lambda row: (not row["certain"], -row["seconds"], row["page"]))
    return result


def plausible_pages(timeline: dict, start: float, end: float) -> dict[str, set[int]]:
    """Pages that could have been shown during a span, per deck.

    Between two matched frames the lecturer moved from one page to the other,
    so every page in between was plausibly on screen even though no frame
    recorded it.  Outside the matched range nothing was observed, so the bound
    opens to the start or the end of the deck rather than pretending to know.
    """
    anchors = sorted((row for row in timeline.get("anchors") or []),
                     key=lambda row: float(row.get("start", 0.0)))
    decks: dict[str, int] = {str(title): int(count)
                             for title, count in (timeline.get("decks") or {}).items()}
    if not decks:
        return {}
    duration = float(timeline.get("duration") or 0.0)
    allowed: dict[str, set[int]] = {}

    def widen(title: str, low: int, high: int) -> None:
        total = decks.get(title)
        if not total:
            return
        low, high = max(1, min(low, high)), min(total, max(low, high))
        if low <= high:
            allowed.setdefault(title, set()).update(range(low, high + 1))

    for title, total in decks.items():
        deck_anchors = [row for row in anchors if str(row.get("title")) == title]
        if not deck_anchors:
            # Nothing was recognized in this deck; every page stays possible.
            widen(title, 1, total)
            continue
        def bounds(anchor: dict) -> tuple[int, int]:
            # A page the matcher could not separate from this one reads the
            # frame just as well, so it bounds the span just as legitimately.
            page = int(anchor.get("page") or 1)
            numbers = [page, *(int(number) for number in anchor.get("also") or [])]
            return min(numbers), max(numbers)

        first, last = deck_anchors[0], deck_anchors[-1]
        if start < float(first.get("start", 0.0)) and end > 0:
            widen(title, 1, bounds(first)[1])
        for current, following in zip(deck_anchors, deck_anchors[1:]):
            span_start = float(current.get("start", 0.0))
            span_end = float(following.get("start", span_start))
            if min(end, span_end) - max(start, span_start) <= 0:
                continue
            low = min(bounds(current)[0], bounds(following)[0])
            high = max(bounds(current)[1], bounds(following)[1])
            widen(title, low, high)
        tail_start = float(last.get("start", 0.0))
        tail_end = max(duration, tail_start)
        if min(end, tail_end) - max(start, tail_start) > 0:
            widen(title, bounds(last)[0], total)
    return allowed
