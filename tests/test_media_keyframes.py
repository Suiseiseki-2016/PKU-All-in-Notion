from __future__ import annotations

import pytest

from pku_sync.media import extract_keyframes


def test_keyframe_extraction_keeps_opening_slide(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    video = tmp_path / "slides.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5, (320, 180))
    if not writer.isOpened():
        pytest.skip("MJPG encoding unavailable")
    try:
        for _ in range(10):
            frame = np.full((180, 320, 3), 240, dtype=np.uint8)
            cv2.putText(frame, "Slide 1", (45, 95), cv2.FONT_HERSHEY_SIMPLEX,
                        1, (20, 20, 20), 2)
            writer.write(frame)
    finally:
        writer.release()
    rows = extract_keyframes(video, tmp_path / "keyframes")
    assert rows and rows[0]["timestamp"] == 0
    assert (tmp_path / "keyframes" / rows[0]["file"]).is_file()
    assert extract_keyframes(video, tmp_path / "keyframes") == rows

