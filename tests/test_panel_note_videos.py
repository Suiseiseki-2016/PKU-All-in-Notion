from __future__ import annotations

import json

from pku_sync.panel import note_videos

_MB = 1024 * 1024


def _write_transcript(directory, spoken: dict[int, str]) -> None:
    """A transcript that says ``spoken[t]`` around second ``t``."""
    (directory / "transcript.json").write_text(json.dumps({
        "duration": max(spoken) + 60 if spoken else 60,
        "segments": [{"start": float(start), "end": start + 4.0, "text": text}
                     for start, text in sorted(spoken.items())],
    }, ensure_ascii=False), "utf-8")


class _FakeNotion:
    def __init__(self, paragraphs=()):
        self.uploads = []
        self.blocks = [
            {"id": f"p{index}", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": text}]}}
            for index, text in enumerate(paragraphs, start=1)
        ]

    def list_children(self, page_id):
        assert page_id == "note"
        return self.blocks

    def upload_file(self, path):
        self.uploads.append(path.name)
        return f"file-upload://{path.stem}"

    def append_blocks(self, page_id, values, *, after, retry):
        assert page_id == "note" and retry is False
        self.blocks.append({**values[0], "id": f"v{len(self.blocks)}"})
        return []


def test_stamps_are_unique_and_keep_note_order():
    assert note_videos._stamps(
        "甲（回看 01:02）乙（回看 00:07）丙（回看 01:02）"
    ) == [("01:02", 62), ("00:07", 7)]


def test_clip_for_timestamp_reuses_hash_named_file(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    calls = []

    def fake_run(source, target, start, duration):
        calls.append((source, start, duration))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip")

    monkeypatch.setattr(note_videos, "_extract_clip", fake_run)
    first = note_videos.clip_for_timestamp(video, tmp_path / "clips", 62)
    second = note_videos.clip_for_timestamp(video, tmp_path / "clips", 62)

    assert first == second
    assert first.name.endswith("-000062.mp4")
    assert len(calls) == 1
    assert calls[0][1:] == (54, 20)


def test_cached_clip_larger_than_remaining_budget_is_rejected(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    monkeypatch.setattr(
        note_videos,
        "_extract_clip",
        lambda source, target, start, duration: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_bytes(b"x" * (2 * _MB)),
        ),
    )
    clip = note_videos.clip_for_timestamp(video, tmp_path / "clips", 62)
    assert clip.stat().st_size == 2 * _MB

    try:
        note_videos.clip_for_timestamp(video, tmp_path / "clips", 62, _MB)
    except note_videos.VideoClipError as exc:
        assert "剩余额度" in str(exc)
    else:
        raise AssertionError("oversized cached clip must not be published")


def test_publish_note_videos_is_idempotent_and_attaches_after_marker(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text(
        "定义（回看 01:02）\n\n另一个数字（回看 03:04）\n", "utf-8"
    )
    _write_transcript(tmp_path, {62: "我们先看这个定义", 184: "另一个数字是多少"})

    def fake_clip(source, directory, timestamp, max_bytes=None):
        assert max_bytes is None or max_bytes > 0
        target = directory / f"clip-{timestamp}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip")
        return target

    monkeypatch.setattr(note_videos, "clip_for_timestamp", fake_clip)
    notion = _FakeNotion(["定义（回看 01:02）", "另一个数字（回看 03:04）"])

    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 2
    assert len(notion.uploads) == 2
    assert [block["type"] for block in notion.blocks[2:]] == ["video", "video"]
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["complete"] is True and report["failed"] == 0
    assert report["skipped_budget"] == 0
    assert report["used_bytes"] == 8
    assert note_videos.video_publication_complete(tmp_path) is True

    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 0
    assert len(notion.uploads) == 2


def test_publish_note_videos_counts_all_clips_against_one_budget(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text(
        "第一处（回看 01:02）\n第二处（回看 03:04）\n第三处（回看 05:06）\n", "utf-8"
    )
    _write_transcript(tmp_path, {62: "这是第一处", 184: "这是第二处", 306: "这是第三处"})
    sizes = {62: 2 * _MB, 184: 2 * _MB, 306: 2 * _MB}
    calls = []

    def fake_run(source, target, start, duration):
        timestamp = start + note_videos._CLIP_PAD_BEFORE
        calls.append(timestamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * sizes[timestamp])

    monkeypatch.setattr(note_videos, "_extract_clip", fake_run)
    notion = _FakeNotion(["第一处（回看 01:02）", "第二处（回看 03:04）", "第三处（回看 05:06）"])

    inserted = note_videos.publish_note_videos(notion, "note", tmp_path)

    assert inserted == 2
    assert len(notion.uploads) == 2
    assert calls == [62, 184, 306]  # the third clip was attempted, then discarded
    clips_dir = tmp_path / "video-clips"
    assert len(list(clips_dir.glob("*.mp4"))) == 2
    assert not list(clips_dir.glob("*-000306.mp4"))
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "partial"
    assert report["complete"] is False
    assert report["inserted"] == 2 and report["failed"] == 1
    assert report["used_bytes"] == 4 * _MB
    assert report["budget_bytes"] == note_videos._MAX_TOTAL_CLIP_BYTES
    assert report["clips"][2]["status"] == "failed"
    assert "剩余额度" in report["clips"][2]["error"]
    assert note_videos.video_publication_complete(tmp_path) is False


def test_publish_note_videos_skips_markers_once_budget_is_exhausted(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text(
        "第一处（回看 01:02）\n第二处（回看 03:04）\n第三处（回看 05:06）\n", "utf-8"
    )
    _write_transcript(tmp_path, {62: "这是第一处", 184: "这是第二处", 306: "这是第三处"})
    half = note_videos._MAX_TOTAL_CLIP_BYTES // 2
    sizes = {62: half, 184: half, 306: half}
    calls = []

    def fake_run(source, target, start, duration):
        timestamp = start + note_videos._CLIP_PAD_BEFORE
        calls.append(timestamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * sizes[timestamp])

    monkeypatch.setattr(note_videos, "_extract_clip", fake_run)
    notion = _FakeNotion(["第一处（回看 01:02）", "第二处（回看 03:04）", "第三处（回看 05:06）"])

    inserted = note_videos.publish_note_videos(notion, "note", tmp_path)

    assert inserted == 2
    assert calls == [62, 184]  # the third marker never reaches extraction
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "partial"
    assert report["complete"] is False
    assert report["skipped_budget"] == 1
    assert report["used_bytes"] == note_videos._MAX_TOTAL_CLIP_BYTES
    assert report["clips"][2]["status"] == "budget_exceeded"
    assert report["clips"][2]["remaining_bytes"] == 0


def test_publish_note_videos_keeps_used_budget_across_retries(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text(
        "第一处（回看 01:02）\n第二处（回看 03:04）\n", "utf-8"
    )
    _write_transcript(tmp_path, {62: "这是第一处", 184: "这是第二处"})
    sizes = {62: 2 * _MB, 184: 4 * _MB}
    state = {"fail_184": True}

    def fake_run(source, target, start, duration):
        timestamp = start + note_videos._CLIP_PAD_BEFORE
        if timestamp == 184 and state["fail_184"]:
            raise RuntimeError("extraction crashed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * sizes[timestamp])

    monkeypatch.setattr(note_videos, "_extract_clip", fake_run)
    notion = _FakeNotion(["第一处（回看 01:02）", "第二处（回看 03:04）"])

    # Run 1: first clip published, second clip fails outright.
    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 1
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["used_bytes"] == 2 * _MB
    assert report["complete"] is False
    assert note_videos.video_publication_complete(tmp_path) is False

    # Run 2: a 4 MB clip still does not fit the 3 MB left for this lecture.
    # If the retry had reset the budget to the full 5 MB, it would pass.
    state["fail_184"] = False
    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 0
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["clips"][0]["status"] == "existing"
    assert report["clips"][1]["status"] == "failed"
    assert report["used_bytes"] == 2 * _MB
    assert report["complete"] is False

    # Run 3: a clip that fits the remaining budget completes the lecture.
    sizes[184] = 2 * _MB
    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 1
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "complete"
    assert report["used_bytes"] == 4 * _MB
    assert report["complete"] is True
    assert note_videos.video_publication_complete(tmp_path) is True


def test_video_clip_failure_keeps_text_publishable(tmp_path, monkeypatch):
    (tmp_path / "video.mp4").write_bytes(b"source")
    (tmp_path / "notes.md").write_text("数字（回看 01:02）", "utf-8")
    _write_transcript(tmp_path, {62: "这个数字很重要"})
    monkeypatch.setattr(
        note_videos,
        "clip_for_timestamp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            note_videos.VideoClipError("extraction unavailable")
        ),
    )
    notion = _FakeNotion(["数字（回看 01:02）"])

    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 0
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "partial"
    assert report["complete"] is False
    assert report["used_bytes"] == 0


def test_publish_without_markers_or_source_video(tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"source")
    (tmp_path / "notes.md").write_text("普通段落，没有回看标记。\n", "utf-8")
    assert note_videos.publish_note_videos(_FakeNotion(), "note", tmp_path) == 0
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "not_needed"
    assert report["complete"] is True
    assert note_videos.video_publication_complete(tmp_path) is True

    other = tmp_path / "lecture-2"
    other.mkdir()
    (other / "notes.md").write_text("数字（回看 01:02）", "utf-8")
    assert note_videos.publish_note_videos(_FakeNotion(), "note", other) == 0
    report = json.loads((other / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "unavailable"
    assert report["complete"] is False
    assert note_videos.video_publication_complete(other) is False


def test_marker_pointing_at_the_wrong_moment_publishes_no_clip(tmp_path, monkeypatch):
    """A real note anchored a deadline at 92:23 while it was said at 95:13.

    The text marker is a hint a student can shrug off; a clip is quoted
    evidence, so an anchor that lands on unrelated audio must not become one.
    """
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text(
        "老师提到第一项正式作业截止时间为 10 月 25 日（回看 92:23）\n"
        "调用 listen 后服务器可以在监听套接字上等待新连接到达（回看 44:51）\n",
        "utf-8",
    )
    _write_transcript(tmp_path, {
        5543: "它就把这两个版本给它合并在一起了",       # 92:23 — git merge talk
        5713: "你看这个作业的截止时间是十月二十五号",     # 95:13 — the real moment
        2691: "调用listen之后服务器就在监听套接字上等待新的连接到达",
    })
    monkeypatch.setattr(note_videos, "_extract_clip", lambda source, target, start, duration: (
        target.parent.mkdir(parents=True, exist_ok=True), target.write_bytes(b"clip")))
    notion = _FakeNotion(["截止时间（回看 92:23）", "listen（回看 44:51）"])

    assert note_videos.publish_note_videos(notion, "note", tmp_path) == 1

    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    by_stamp = {clip["timestamp"]: clip for clip in report["clips"]}
    assert by_stamp["92:23"]["status"] == "unverified"
    assert by_stamp["92:23"]["score"] == 0.0
    assert by_stamp["44:51"]["status"] == "inserted"
    assert report["skipped_unverified"] == 1
    assert notion.uploads == ["clip-2691.mp4"] or len(notion.uploads) == 1
    # Nothing is pending, so the recording is not pinned to disk forever.
    assert report["complete"] is True


def test_clips_need_a_transcript_to_check_the_marker_against(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source")
    (tmp_path / "notes.md").write_text("这个定义很重要（回看 01:02）", "utf-8")
    monkeypatch.setattr(note_videos, "_extract_clip",
                        lambda *args: (_ for _ in ()).throw(
                            AssertionError("must not cut without a transcript")))

    assert note_videos.publish_note_videos(_FakeNotion(), "note", tmp_path) == 0
    report = json.loads((tmp_path / note_videos._REPORT_NAME).read_text("utf-8"))
    assert report["corroborated"] is False
    assert report["clips"][0]["status"] == "unverified"
    assert report["skipped_unverified"] == 1
    assert report["verification_pending"] == 1
    assert report["status"] == "partial"
    assert report["complete"] is False


def test_corroboration_scores_the_best_claim_at_that_moment():
    segments = [
        {"start": 100.0, "text": "我们来看这个可靠传输的滑动窗口"},
        {"start": 400.0, "text": "完全无关的另一个话题"},
    ]
    assert note_videos.corroboration(
        ["可靠传输的滑动窗口"], segments, 100) > 0.8
    assert note_videos.corroboration(
        ["完全无关的另一个话题", "可靠传输的滑动窗口"], segments, 100) > 0.8
    assert note_videos.corroboration(["可靠传输的滑动窗口"], segments, 400) == 0.0
    assert note_videos.corroboration([], segments, 100) == 0.0


def _write_sample_recording(path, seconds: float = 6.0) -> None:
    """Synthesize a real mpeg4+aac MP4 in-process: no network, no CLI tools."""
    import av

    def encode_video(stream, count):
        for index in range(count):
            frame = av.VideoFrame(160, 120, "yuv420p")
            for plane in frame.planes:
                plane.update(b"\x80" * plane.buffer_size)
            frame.pts = index
            for packet in stream.encode(frame):
                yield (packet.pts * 0.04, packet)
        for packet in stream.encode():
            yield (packet.pts * 0.04, packet)

    def encode_audio(stream, count):
        for index in range(count):
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            frame.rate = 48000
            for plane in frame.planes:
                plane.update(b"\x00" * plane.buffer_size)
            frame.pts = index * 1024
            for packet in stream.encode(frame):
                yield (packet.pts * 1024 / 48000, packet)
        for packet in stream.encode():
            yield (packet.pts * 1024 / 48000, packet)

    with av.open(str(path), "w", format="mp4") as container:
        video = container.add_stream("mpeg4", rate=25)
        video.width, video.height, video.pix_fmt = 160, 120, "yuv420p"
        video.codec_context.gop_size = 12  # frequent keyframes
        audio = container.add_stream("aac", 48000)
        audio.layout = "stereo"
        audio.codec_context.bit_rate = 32000
        packets = list(encode_video(video, int(seconds * 25)))
        packets += list(encode_audio(audio, int(seconds * 48000) // 1024))
        for _, packet in sorted(packets, key=lambda item: item[0]):
            container.mux(packet)


def test_extract_clip_produces_keyframe_aligned_playable_clip(tmp_path):
    sample = tmp_path / "video.mp4"
    _write_sample_recording(sample)
    target = tmp_path / "clips" / "cut.mp4"

    note_videos._extract_clip(sample, target, 2, 20)

    import av

    assert target.is_file() and target.stat().st_size > 0
    with av.open(str(target)) as clip:
        assert sorted(stream.type for stream in clip.streams) == ["audio", "video"]
        duration = clip.duration / 1e6
        # The clip starts at the keyframe (0.48 s grid) before the 2 s mark and
        # runs to the end of the 6 s sample.
        assert 3.5 <= duration <= 4.6
        first: dict[str, tuple] = {}
        for packet in clip.demux():
            if packet.dts is None:
                continue
            if packet.stream.type not in first:
                first[packet.stream.type] = (packet.pts, packet.is_keyframe)
        video_pts, video_keyframe = first["video"]
        assert video_pts == 0 and video_keyframe is True


def test_extract_clip_rejects_markers_outside_the_recording(tmp_path):
    sample = tmp_path / "video.mp4"
    _write_sample_recording(sample)
    target = tmp_path / "clips" / "cut.mp4"

    try:
        note_videos._extract_clip(sample, target, 999, 20)
    except note_videos.VideoClipError as exc:
        assert "超出录像范围" in str(exc)
    else:
        raise AssertionError("a marker past the recording must not produce a clip")
    assert not target.exists()
    assert not list((tmp_path / "clips").glob("*.tmp.mp4"))


def test_extract_clip_requires_a_video_track(tmp_path):
    import av

    audio_only = tmp_path / "audio-only.m4a"
    with av.open(str(audio_only), "w", format="ipod") as container:
        audio = container.add_stream("aac", 48000)
        audio.layout = "stereo"
        for index in range(280):
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            frame.rate = 48000
            for plane in frame.planes:
                plane.update(b"\x00" * plane.buffer_size)
            frame.pts = index * 1024
            for packet in audio.encode(frame):
                container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)

    try:
        note_videos._extract_clip(audio_only, tmp_path / "clip.mp4", 1, 20)
    except note_videos.VideoClipError as exc:
        assert "没有视频轨" in str(exc)
    else:
        raise AssertionError("an audio-only file must not produce a clip")
