from __future__ import annotations

import json

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

