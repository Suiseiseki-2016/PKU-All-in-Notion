"""Lecture-source matching must prefer omission to a wrong-week document."""

import zipfile

from pku_sync.note_sources import collect_note_sources


def _pptx(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<a:t>{text}</a:t></p:sld>",
        )


def test_only_exactly_dated_material_is_used(tmp_path):
    root = tmp_path / "course"
    materials = root / "materials" / "教学内容"
    _pptx(materials / "20260916神经组织.pptx", "突触")
    _pptx(materials / "20260923进化.pptx", "进化")
    _pptx(materials / "Week 2 prenatal.pptx", "胚胎")
    sources = collect_note_sources(root, "2026-09-16", "第 2 讲")
    assert [(item["title"], item["locator"], item["text"])
            for item in sources] == [("20260916神经组织.pptx", "第 1 张", "突触")]


def test_unreadable_exact_date_pdf_is_flagged_instead_of_accepted(tmp_path):
    from pypdf import PdfWriter

    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916扫描课件.pdf"
    path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    sources = collect_note_sources(root, "2026-09-16", "第 2 讲")
    assert len(sources) == 1
    assert sources[0]["status"] == "unreadable"
    assert sources[0]["text"] == ""


def test_pdf_pages_with_image_and_sparse_text_are_not_treated_as_full_text(tmp_path, monkeypatch):
    from pku_sync import note_sources

    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916课堂习题.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PDF fixture read by the patched parser")

    class Page:
        def __init__(self, text, images):
            self.text = text
            self.images = images

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, _path):
            self.pages = [
                Page("作业1-习题6~7 ⑥ ⑦", [object(), object()]),
                Page("", [object()]),
                Page("这里是完全可提取的普通文字，没有嵌入图片。", []),
            ]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    sources = note_sources.collect_note_sources(root, "2026-09-16", "课堂")
    assert [(row["locator"], row["status"]) for row in sources] == [
        ("第 1 页", "partial"),
        ("第 2 页", "unreadable"),
        ("第 3 页", "readable"),
    ]
    assert sources[0]["text"] == "作业1-习题6~7 ⑥ ⑦"


def test_pdf_page_cap_is_reported_instead_of_silently_claiming_full_coverage(tmp_path, monkeypatch):
    from pku_sync import note_sources

    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916讲义.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PDF fixture read by the patched parser")

    class Page:
        images = []

        def extract_text(self):
            return "可读取的文字"

    class Reader:
        def __init__(self, _path):
            self.pages = [Page() for _ in range(note_sources._PAGE_LIMIT + 1)]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    sources = note_sources.collect_note_sources(root, "2026-09-16", "课堂")
    assert len(sources) == note_sources._PAGE_LIMIT + 1
    assert sources[note_sources._PAGE_LIMIT - 1]["status"] == "readable"
    assert sources[note_sources._PAGE_LIMIT] == {
        "title": path.name,
        "locator": f"第 {note_sources._PAGE_LIMIT + 1} 页（共 {note_sources._PAGE_LIMIT + 1} 页）",
        "path": str(path.relative_to(root)),
        "text": "",
        "status": "truncated",
    }


def test_explicit_long_lecture_deck_extracts_late_pages(tmp_path, monkeypatch):
    from pku_sync import note_sources

    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916古典密码.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PDF fixture read by the patched parser")

    class Page:
        images = []

        def __init__(self, number):
            self.number = number

        def extract_text(self):
            return f"第 {self.number} 页的详细课堂内容"

    class Reader:
        def __init__(self, _path):
            self.pages = [Page(index) for index in range(1, 166)]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    sources = note_sources.collect_note_sources(root, "2026-09-16", "课堂")
    assert len(sources) == 165
    assert sources[118]["locator"] == "第 119 页"
    assert sources[118]["status"] == "readable"
    assert sources[146]["text"] == "第 147 页的详细课堂内容"
    assert not any(row["status"] == "truncated" for row in sources)


def test_explicit_material_link_can_override_date_without_leaving_course(tmp_path):
    root = tmp_path / "course"
    linked = root / "materials" / "教学内容" / "Week 3 prenatal.pptx"
    other = tmp_path / "other-course" / "materials" / "20260916secret.pptx"
    _pptx(linked, "胚胎期")
    _pptx(other, "其他课程")
    sources = collect_note_sources(root, "2026-09-16", "课堂", allowed_paths=[linked, other, linked])
    assert [(item["title"], item["text"]) for item in sources] == [(linked.name, "胚胎期")]
    assert collect_note_sources(root, "2026-09-16", "课堂", allowed_paths=[]) == []


def test_long_page_preserves_tail_and_spacing_for_separate_term_review(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from pku_sync import note_editor, note_sources, transcript_notes
    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916完整页.pptx"
    text = "课堂段落。" * 1000 + "\n    保留缩进的代码\n页尾重要条件不得丢失。"
    _pptx(path, text)
    sources = collect_note_sources(root, "2026-09-16", "课堂")
    assert len(sources) == 1 and sources[0]["text"] == text
    seen = []
    def ask(system, prompt):
        seen.append(json.loads(prompt))
        return "课堂笔记。"
    transcript_notes.write_notes({"segments": [{"start": 0, "end": 10, "text": "课堂讲述。"}]},
                            tmp_path / "notes.md", SimpleNamespace(notes_model="qwen3.7-plus"),
                            "课堂", ask=ask, source_context=sources)
    assert len(seen) == 1 and "courseware" not in seen[0]
    assert sources[0]["text"] == text
    report = json.loads(note_editor.report_path(tmp_path / "notes.md").read_text("utf-8"))
    assert report["sources_sha256"] == note_editor.fingerprint(sources)


def test_office_page_ceiling_and_empty_slides_are_explicit(tmp_path, monkeypatch):
    from pku_sync import note_sources
    monkeypatch.setattr(note_sources, "_PAGE_LIMIT", 2)
    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916课件.pptx"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as archive:
        for number, text in enumerate(["有文字", "", "被资源上限限制的页"], 1):
            archive.writestr(f"ppt/slides/slide{number}.xml", f"<slide><t>{text}</t></slide>")
    sources = collect_note_sources(root, "2026-09-16", "课堂")
    assert [(row["locator"], row["status"]) for row in sources] == [
        ("第 1 张", "readable"), ("第 2 张", "unreadable"), ("第 3–3 张（共 3 张）", "truncated")]


def test_image_presence_is_disclosed_even_when_page_has_many_words(tmp_path, monkeypatch):
    from pku_sync import note_sources
    root = tmp_path / "course"
    path = root / "materials" / "教学内容" / "20260916图文.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"isolated PDF parser fixture")
    text = "可提取的长文本。" * 500
    class Page:
        images = [object()]
        def extract_text(self): return text
    class Reader:
        def __init__(self, _path): self.pages = [Page()]
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    sources = note_sources.collect_note_sources(root, "2026-09-16", "课堂")
    assert sources[0]["text"] == text and sources[0]["status"] == "partial"
