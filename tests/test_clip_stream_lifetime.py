"""A clip must remain valid when the decoder releases its input streams."""
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import sys

from pku_sync.panel import note_videos


def test_clip_duration_does_not_read_released_input_stream(tmp_path, monkeypatch):
    class Stream:
        type = "video"

        def __init__(self):
            self.closed = False

        @property
        def index(self):
            assert not self.closed, "input stream has been released"
            return 0

        @property
        def time_base(self):
            assert not self.closed, "input stream has been released"
            return Fraction(1, 10)

    stream = Stream()

    class Source:
        streams = SimpleNamespace(video=[stream], audio=[])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            stream.closed = True

        def seek(self, *args, **kwargs):
            pass

        def demux(self, *args):
            for timestamp in (0, 10, 20, 30):
                yield SimpleNamespace(stream=stream, pts=timestamp, dts=timestamp)

    class Output:
        def __init__(self, path):
            self.path = Path(path)

        def __enter__(self):
            self.path.write_bytes(b"muxed video")
            return self

        def __exit__(self, *args):
            pass

        def add_stream_from_template(self, template):
            return SimpleNamespace(type="video", index=0)

        def mux(self, packet):
            pass

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(
        open=lambda path, mode=None, **kwargs: Output(path) if mode == "w" else Source()))
    target = tmp_path / "clip.mp4"
    note_videos._extract_clip(tmp_path / "source.mp4", target, 0, 20)
    assert stream.closed
    assert target.read_bytes() == b"muxed video"
    assert not target.with_suffix(".tmp.mp4").exists()
