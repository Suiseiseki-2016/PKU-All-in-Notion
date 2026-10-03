from __future__ import annotations

import json
from pathlib import Path

import pytest

from pku_sync.slide_alignment import (
    ALIGNMENT_FILENAME,
    align_signatures,
    build_timeline,
    load_or_build_timeline,
    pages_on_screen,
    plausible_pages,
    render_deck,
)

PAGE_WIDTH, PAGE_HEIGHT = 480, 270


def _write_pdf(path: Path, pages: list[list[tuple[int, int, int, int]]]) -> None:
    """Emit a minimal multi-page PDF whose pages differ visibly."""
    objects: list[bytes] = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"",  # page tree, filled in once the page object numbers are known
    ]
    page_refs = []
    for rectangles in pages:
        content = b"0 0 0 rg\n" + b"".join(
            f"{x} {y} {w} {h} re f\n".encode("ascii") for x, y, w, h in rectangles
        )
        content_number = len(objects) + 2
        page_number = len(objects) + 1
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {PAGE_WIDTH} {PAGE_HEIGHT}]"
            f"/Contents {content_number} 0 R/Resources<<>>>>".encode("ascii")
        )
        objects.append(b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream")
        page_refs.append(page_number)
    objects[1] = ("<</Type/Pages/Kids[" + " ".join(f"{n} 0 R" for n in page_refs)
                  + f"]/Count {len(page_refs)}>>").encode("ascii")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj".encode("ascii") + body + b"endobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode("ascii")
    path.write_bytes(bytes(out))


def _deck_rectangles(count: int) -> list[list[tuple[int, int, int, int]]]:
    pages = []
    for number in range(count):
        step = 20 + number * 17
        pages.append([
            (20, 200 - number * 8, 200 + number * 20, 30),
            (step, 40, 60, 90 + number * 10),
            (300 - number * 15, 120, 120, 40),
            (60 + number * 30, 150, 40, 40),
        ])
    return pages


def _projector_capture(page_gray):
    """Degrade a rendered page the way a filmed projector screen degrades it."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    blurred = cv2.GaussianBlur(page_gray, (0, 0), 1.2)
    washed = (blurred.astype(np.float32) * 0.45 + 105).clip(0, 255).astype(np.uint8)
    return cv2.resize(washed, (1920, 1080), interpolation=cv2.INTER_LINEAR)


def _build_fixture(tmp_path: Path, page_count: int = 6):
    """A course directory with one matched deck and one keyframe per page."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    pytest.importorskip("pypdfium2")
    import pypdfium2 as pdfium

    course = tmp_path / "course"
    materials = course / "materials" / "课程内容"
    materials.mkdir(parents=True)
    pdf = materials / "ch02-古典密码.pdf"
    _write_pdf(pdf, _deck_rectangles(page_count))

    frames_dir = tmp_path / "recording" / "keyframes"
    frames_dir.mkdir(parents=True)
    document = pdfium.PdfDocument(str(pdf))
    keyframes = []
    try:
        for number in range(page_count):
            page = document[number]
            bitmap = page.render(scale=2.0)
            gray = cv2.cvtColor(bitmap.to_numpy(), cv2.COLOR_RGB2GRAY)
            bitmap.close()
            page.close()
            name = f"frame_{number:04d}.jpg"
            cv2.imencode(".jpg", _projector_capture(gray))[1].tofile(
                str(frames_dir / name))
            keyframes.append({"index": number, "timestamp": float(number * 60),
                              "time": f"{number:02d}:00", "file": name})
    finally:
        document.close()

    sources = [{"title": pdf.name, "locator": f"第 {number} 页",
                "path": str(pdf.relative_to(course)), "text": f"page {number}",
                "status": "readable"}
               for number in range(1, page_count + 1)]
    return course, frames_dir, keyframes, sources, pdf


def test_render_deck_produces_one_signature_per_page(tmp_path):
    pytest.importorskip("pypdfium2")
    pytest.importorskip("cv2")
    pdf = tmp_path / "deck.pdf"
    _write_pdf(pdf, _deck_rectangles(4))
    signatures = render_deck(pdf)
    assert signatures.shape[0] == 4
    # Distinct pages must not collapse onto identical signatures.
    assert float(abs(signatures[0] - signatures[3]).mean()) > 0.1


def test_timeline_matches_each_frame_to_the_page_it_shows(tmp_path):
    course, frames_dir, keyframes, sources, _ = _build_fixture(tmp_path)
    timeline = build_timeline(keyframes, frames_dir, sources, course, duration=400.0)
    anchors = timeline["anchors"]
    assert len(anchors) == len(keyframes)
    for position, row in enumerate(anchors):
        assert row["page"] == position + 1
        assert row["title"] == "ch02-古典密码.pdf"
    assert timeline["decks"] == {"ch02-古典密码.pdf": 6}
    # Each entry covers the span until the next projected page.
    assert anchors[0]["start"] == 0.0 and anchors[0]["end"] == 60.0
    assert anchors[-1]["end"] == 400.0


def test_a_frame_that_shows_no_slide_stays_unmatched(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    course, frames_dir, keyframes, sources, _ = _build_fixture(tmp_path, page_count=4)
    generator = np.random.default_rng(7)
    noise = generator.integers(0, 255, (1080, 1920), dtype=np.uint8)
    cv2.imencode(".jpg", noise)[1].tofile(str(frames_dir / "frame_9999.jpg"))
    keyframes.append({"index": 99, "timestamp": 600.0, "time": "10:00",
                      "file": "frame_9999.jpg"})

    anchors = build_timeline(keyframes, frames_dir, sources, course,
                             duration=700.0)["anchors"]
    assert [row["page"] for row in anchors] == [1, 2, 3, 4]
    assert all(row["start"] != 600.0 for row in anchors)


def test_unmatched_deck_or_missing_frames_yield_no_timeline(tmp_path):
    course, frames_dir, keyframes, sources, _ = _build_fixture(tmp_path, page_count=3)
    assert build_timeline([], frames_dir, sources, course)["anchors"] == []
    assert build_timeline(keyframes, frames_dir, [], course)["anchors"] == []
    # A source row whose file is absent must not be rendered as a deck.
    absent = [{**row, "path": "materials/missing.pdf"} for row in sources]
    assert build_timeline(keyframes, frames_dir, absent, course)["anchors"] == []


def test_stored_alignment_is_reused_and_invalidated(tmp_path):
    course, frames_dir, keyframes, sources, _ = _build_fixture(tmp_path, page_count=4)
    first = load_or_build_timeline(keyframes, frames_dir, sources, course,
                                   duration=300.0)
    stored = frames_dir / ALIGNMENT_FILENAME
    assert first["anchors"] and stored.is_file()

    payload = json.loads(stored.read_text("utf-8"))
    payload["anchors"][0]["page"] = 99
    stored.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    assert load_or_build_timeline(keyframes, frames_dir, sources, course,
                                  duration=300.0)["anchors"][0]["page"] == 99

    # A different set of frames describes a different recording state.
    changed = load_or_build_timeline(keyframes[:-1], frames_dir, sources, course,
                                     duration=300.0)
    assert changed["anchors"][0]["page"] == 1


def _sample_timeline() -> dict:
    return {
        "decks": {"deck.pdf": 12},
        "duration": 300.0,
        "anchors": [
            {"start": 0.0, "end": 60.0, "title": "deck.pdf", "page": 3, "also": []},
            {"start": 60.0, "end": 260.0, "title": "deck.pdf", "page": 4, "also": [5]},
            {"start": 260.0, "end": 300.0, "title": "deck.pdf", "page": 9, "also": []},
        ],
    }


def test_pages_on_screen_reports_overlap_ordered_by_time_shown():
    timeline = _sample_timeline()
    rows = pages_on_screen(timeline, 30.0, 270.0)
    assert [(row["page"], row["certain"]) for row in rows] == [
        (4, True), (3, True), (9, True), (5, False)]
    assert rows[0]["seconds"] == 200.0
    assert pages_on_screen(timeline, 400.0, 500.0) == []


def test_plausible_pages_bridges_the_gap_between_matched_frames():
    timeline = _sample_timeline()
    # Between the p4 and p9 frames the lecturer passed through 5 to 8.
    assert plausible_pages(timeline, 100.0, 250.0)["deck.pdf"] == set(range(4, 10))
    # Consecutive frames bound the span to those two pages, plus the build of
    # page 4 that the matcher could not tell apart from it.
    assert plausible_pages(timeline, 10.0, 50.0)["deck.pdf"] == {3, 4, 5}
    # Nothing was observed after the final frame, so the deck tail stays open.
    assert plausible_pages(timeline, 290.0, 320.0)["deck.pdf"] == set(range(9, 13))
    # Before the first matched frame any earlier page could have been shown.
    late = {"decks": {"deck.pdf": 12}, "duration": 300.0,
            "anchors": [{"start": 90.0, "end": 300.0, "title": "deck.pdf",
                         "page": 5, "also": []}]}
    assert plausible_pages(late, 0.0, 60.0)["deck.pdf"] == {1, 2, 3, 4, 5}


def test_every_page_offered_as_projected_is_also_a_plausible_page():
    """The two views of one alignment must not contradict each other.

    Evidence selection offers the pages from ``pages_on_screen`` while the
    candidate filter uses ``plausible_pages``; a page allowed by one and
    refused by the other would be dropped from the note for no reason.
    """
    timeline = _sample_timeline()
    for window in ((0.0, 80.0), (30.0, 270.0), (250.0, 300.0), (0.0, 300.0)):
        allowed = plausible_pages(timeline, *window).get("deck.pdf", set())
        for row in pages_on_screen(timeline, *window):
            assert row["page"] in allowed, (window, row)


def test_plausible_pages_opens_fully_when_a_deck_was_never_recognized():
    timeline = {"decks": {"deck.pdf": 5}, "duration": 100.0, "anchors": []}
    assert plausible_pages(timeline, 0.0, 100.0)["deck.pdf"] == set(range(1, 6))
    assert plausible_pages({"decks": {}, "anchors": []}, 0.0, 10.0) == {}


def test_align_signatures_without_pages_returns_no_assignment():
    np = pytest.importorskip("numpy")
    frames = np.zeros((3, 8), dtype=np.float32)
    assigned, _ = align_signatures(frames, np.zeros((0, 8), dtype=np.float32), [])
    assert assigned == [None, None, None]
