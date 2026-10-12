from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pku_sync.panel.note_images import publish_note_images


def test_publish_note_images_places_one_per_window_and_retry_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("pku_sync.panel.note_images._visual_score", lambda path: 2.0)
    frames = tmp_path / "keyframes"
    frames.mkdir()
    rows = []
    for index, seconds in enumerate((60, 240, 600)):
        name = f"frame_{index:04d}.jpg"
        (frames / name).write_bytes(b"jpeg")
        rows.append({"file": name, "timestamp": seconds, "time": f"{seconds // 60:02d}:00"})
    (frames / "index.json").write_text(json.dumps({"keyframes": rows}), "utf-8")
    blocks = [
        {"id": "h1", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "00:00–08:00"}]}},
        {"id": "p1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "讲解"}]}},
        {"id": "h2", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "08:00–16:00"}]}},
    ]

    class Notion:
        def __init__(self):
            self.uploads = []
            self.insertions = []
        def list_children(self, page_id):
            assert page_id == "note"
            return blocks
        def upload_file(self, path):
            self.uploads.append(path.name)
            return "file-upload://" + path.stem
        def append_blocks(self, page_id, values, *, after, retry):
            assert page_id == "note" and retry is False
            self.insertions.append((after, values[0]))
            value = values[0]
            image = value["image"]
            image["caption"] = [{"plain_text": image["caption"][0]["text"]["content"]}]
            blocks.append({"id": f"image-{len(self.insertions)}", **value})
            return []
    notion = Notion()
    assert publish_note_images(notion, "note", tmp_path) == 2
    assert notion.uploads == ["frame_0001.jpg", "frame_0002.jpg"]
    assert [after for after, _ in notion.insertions] == ["h1", "h2"]
    assert all(value["type"] == "image" for _, value in notion.insertions)
    assert publish_note_images(notion, "note", tmp_path) == 0
    assert len(notion.uploads) == 2


def test_publish_note_images_ignores_untrusted_paths(tmp_path):
    folder = tmp_path / "keyframes"
    folder.mkdir()
    (folder / "index.json").write_text(json.dumps({"keyframes": [
        {"file": "../outside.jpg", "timestamp": 3, "time": "00:03"},
        {"file": "missing.jpg", "timestamp": 5, "time": "00:05"},
    ]}), "utf-8")
    class Notion:
        def list_children(self, page_id):
            raise AssertionError("No valid files should reach Notion")
    assert publish_note_images(Notion(), "note", tmp_path) == 0


@pytest.mark.parametrize("heading", ["#", "##", "###"])
def test_merged_topic_images_use_chapter_times_and_leave_student_heading_alone(tmp_path, monkeypatch, heading):
    from pku_sync import transcript_notes
    from pku_sync.notion import markdown_to_blocks
    monkeypatch.setattr("pku_sync.panel.note_images._visual_score", lambda path: 2.0)
    folder = tmp_path / "keyframes"
    folder.mkdir()
    frames = []
    for n, stamp in enumerate((10, 610)):
        name = f"frame_{n}.jpg"
        (folder / name).write_bytes(b"original-image")
        frames.append({"file": name, "timestamp": stamp, "time": f"{stamp//60:02}:{stamp%60:02}"})
    (folder / "index.json").write_text(json.dumps({"keyframes": frames}), "utf-8")
    target = tmp_path / "notes.md"
    lecture = {"segments": [{"start": 0, "end": 20, "text": "第一段。"},
        {"start": 600, "end": 625, "text": "第二段。"}]}
    transcript_notes.write_topic_notes(lecture, target,
        SimpleNamespace(notes_model="qwen3.5-flash", pku_username="student"), "课程",
        ask=lambda *a: "分段知识点。",
        merge_ask=lambda *a: f"{heading} 知识点甲 [00:00–00:20]\n\n定义与例子。\n\n{heading} 知识点乙 [10:00–10:25]\n\n解释与条件。")
    saved = target.read_bytes()
    blocks = markdown_to_blocks("来源提示\n\n" + target.read_text("utf-8"))
    for n, block in enumerate(blocks):
        block["id"] = f"b{n}"
    student = markdown_to_blocks("## 学生补充 [00:00–00:20]")[0]
    student["id"] = "student-heading"
    blocks.append(student)
    insertions, uploads = [], []
    class Notion:
        def list_children(self, page_id): return blocks
        def upload_file(self, path):
            uploads.append(path)
            return "file-upload://" + path.stem
        def append_blocks(self, page_id, values, *, after, retry):
            assert after != "student-heading" and retry is False
            insertions.append(after)
            position = next(i for i, b in enumerate(blocks) if b["id"] == after)
            value = values[0]
            value["id"] = f"image-{len(insertions)}"
            blocks.insert(position + 1, value)
    notion = Notion()
    assert publish_note_images(notion, "note", tmp_path, body_offset=1) == 2
    assert insertions == ["b2", "b4"]
    assert publish_note_images(notion, "note", tmp_path, body_offset=1) == 0
    assert len(uploads) == 2 and blocks[-1] == student and target.read_bytes() == saved
    assert transcript_notes.topic_record_issues(target, lecture) == []


@pytest.mark.parametrize("staged", [False, True])
def test_plain_organized_notes_place_images_between_sections_without_rewriting_text(tmp_path, monkeypatch, staged):
    from pku_sync import transcript_notes
    from pku_sync.notion import markdown_to_blocks

    monkeypatch.setattr("pku_sync.panel.note_images._visual_score", lambda path: 2.0)
    folder = tmp_path / "keyframes"
    folder.mkdir()
    rows = []
    for number, stamp in enumerate((10, 610)):
        name = f"frame_{number}.jpg"
        (folder / name).write_bytes(b"original-jpeg")
        rows.append({"file":name,"timestamp":stamp,"time":f"{stamp//60:02}:{stamp%60:02}"})
    (folder / "index.json").write_text(json.dumps({"keyframes":rows}), "utf-8")
    target = tmp_path / ("notes.revised.md" if staged else "notes.md")
    if staged:
        (tmp_path / "notes.md").write_text("# 保留的旧版\n\n不能拿来定位新版插图。", "utf-8")
    calls = []
    raw = ["## 相同标题\n\n第一段原讲述。", "## 相同标题\n\n第二段例子，可能需要回听。"]
    transcript_notes.write_notes({"segments":[
        {"start":0,"end":20,"text":"第一段"}, {"start":600,"end":625,"text":"第二段"}]},
        target, SimpleNamespace(notes_model="qwen3.5-flash",pku_username="student"), "课程",
        ask=lambda *args: calls.append(args) or raw[len(calls)-1])
    saved = target.read_bytes()
    blocks = markdown_to_blocks("回看提示\n\n" + target.read_text("utf-8"))
    for number, block in enumerate(blocks):
        block["id"] = f"b{number}"
    blocks.append({"id":"student-annotation", "type":"paragraph", "paragraph":{
        "rich_text":[{"type":"text","text":{"content":"学生自己的批注，必须保留。"}}]}})

    class Notion:
        def __init__(self): self.uploads=[]
        def list_children(self, page_id): return blocks
        def upload_file(self, path):
            self.uploads.append(path.name)
            return "file-upload://" + path.stem
        def append_blocks(self, page_id, values, *, after, retry):
            assert page_id == "note" and retry is False and after is not None
            position = next(index for index, block in enumerate(blocks) if block["id"] == after)
            for value in values:
                value["id"] = "image-" + str(len(self.uploads))
                blocks.insert(position+1, value)

    notion = Notion()
    assert publish_note_images(notion,"note",tmp_path,notes_path=target,body_offset=1) == 2
    assert notion.uploads == ["frame_0.jpg","frame_1.jpg"]
    types = [block["type"] for block in blocks]
    assert types == ["paragraph","heading_1","heading_2","image","paragraph","heading_2","image","paragraph","paragraph"]
    assert blocks[-1]["id"] == "student-annotation"
    assert publish_note_images(notion,"note",tmp_path,notes_path=target,body_offset=1) == 0
    assert len(notion.uploads) == 2 and len(calls) == 2
    assert target.read_bytes() == saved
    assert transcript_notes.record_issues(target,{"segments":[
        {"start":0,"end":20,"text":"第一段"}, {"start":600,"end":625,"text":"第二段"}]}) == []


def test_missing_slide_window_uses_original_video_and_preserves_paid_note(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    from pku_sync import transcript_notes
    from pku_sync.notion import markdown_to_blocks

    video = tmp_path / "video.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5, (320, 180))
    assert writer.isOpened()
    try:
        for n in range(60):
            frame = np.full((180, 320, 3), 240, dtype=np.uint8)
            cv2.putText(frame, "Old diagram" if n < 15 else "New table", (25, 95),
                        cv2.FONT_HERSHEY_SIMPLEX, .9, (20, 20, 20), 2)
            writer.write(frame)
    finally:
        writer.release()
    folder = tmp_path / "keyframes"
    folder.mkdir()
    cv2.imwrite(str(folder / "old.jpg"), np.full((180, 320, 3), 240, dtype=np.uint8))
    (folder / "index.json").write_text(json.dumps({"keyframes": [
        {"file": "old.jpg", "timestamp": 0, "time": "00:00"}]}), "utf-8")
    target = tmp_path / "notes.md"
    lecture = {"segments": [{"start": 0, "end": 11, "text": "本段说明新表。"}]}
    transcript_notes.write_topic_notes(lecture, target,
        SimpleNamespace(notes_model="qwen3.5-flash", pku_username="student"), "课程",
        ask=lambda *a: "分段。", merge_ask=lambda *a: "## 新表 [00:03–00:07]\n\n表中关系。")
    preserved = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    blocks = markdown_to_blocks(target.read_text("utf-8"))
    for n, block in enumerate(blocks):
        block["id"] = f"b{n}"
    blocks.append({"id": "student", **markdown_to_blocks("## 批注 [00:08–00:10]")[0]})
    uploads, insertions = [], []
    class Notion:
        def list_children(self, page_id): return blocks
        def upload_file(self, path):
            uploads.append(path)
            return "file-upload://" + path.stem
        def append_blocks(self, page_id, values, *, after, retry):
            assert after == "b1" and retry is False
            insertions.extend(values)
            blocks.extend(values)
    notion = Notion()
    assert publish_note_images(notion, "note", tmp_path, body_offset=0) == 1
    assert uploads[0].parent == tmp_path / "note-frames"
    assert "00:05" in insertions[0]["image"]["caption"][0]["text"]["content"]
    assert len(list((tmp_path / "note-frames").glob("*.jpg"))) == 3
    assert all(p.read_bytes() == body for p, body in preserved.items())
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a: pytest.fail("Retry must reuse original/time cache"))
    assert publish_note_images(notion, "note", tmp_path, body_offset=0) == 0
    assert len(uploads) == 1 and blocks[3]["id"] == "student"


def test_missing_window_with_unreadable_video_does_not_publish_previous_slide(tmp_path):
    from pku_sync import transcript_notes
    from pku_sync.notion import markdown_to_blocks
    from pku_sync.panel.note_images import NoteFrameError
    (tmp_path / "video.mp4").write_bytes(b"damaged-video")
    target = tmp_path / "notes.md"
    lecture = {"segments": [{"start": 0, "end": 10, "text": "讲述。"}]}
    transcript_notes.write_topic_notes(lecture, target,
        SimpleNamespace(notes_model="qwen3.5-flash", pku_username="student"), "课程",
        ask=lambda *a: "分段。", merge_ask=lambda *a: "## 新图 [00:03–00:07]\n\n说明。")
    saved = target.read_bytes()
    blocks = markdown_to_blocks(target.read_text("utf-8"))
    for n, block in enumerate(blocks): block["id"] = f"b{n}"
    class Notion:
        def list_children(self, page_id): return blocks
        def upload_file(self, path): pytest.fail("No unrelated gallery image may be published")
    with pytest.raises(NoteFrameError, match="原录像"):
        publish_note_images(Notion(), "note", tmp_path, body_offset=0)
    assert target.read_bytes() == saved

