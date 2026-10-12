"""Real Windows PyAV encoding checks; these are not native E2E evidence."""
from fractions import Fraction
import math
import av
import pytest
from pku_sync import transcription


@pytest.fixture(scope='module')
def audio(tmp_path_factory):
    path = tmp_path_factory.mktemp('actual-audio-boundary') / 'source.wav'
    # Teaching recordings use a different source rate; resampling frames do
    # not generally land on a 600-second sample boundary.
    rate, length = 48000, 610 * 48000 + 951
    with av.open(str(path), mode='w') as out:
        stream = out.add_stream('pcm_s16le', rate=rate)
        stream.layout = 'mono'
        position = 0
        while position < length:
            count = min(4093, length - position)
            frame = av.AudioFrame(format='s16', layout='mono', samples=count)
            frame.planes[0].update(bytes(frame.planes[0].buffer_size))
            frame.sample_rate = rate
            frame.time_base = Fraction(1, rate)
            frame.pts = position
            for packet in stream.encode(frame):
                out.mux(packet)
            position += count
        for packet in stream.encode(None):
            out.mux(packet)
    return path, length // 3


@pytest.mark.parametrize('method', ['_extract_audio_parts', '_split_audio'])
def test_encoded_part_never_exceeds_original_six_hundred_second_limit(audio, tmp_path, method):
    path, samples = audio
    parts = getattr(transcription, method)(path, tmp_path, 600)
    assert len(parts) == math.ceil(samples / (600 * 16000))
    assert [offset for _, offset in parts] == [0, 600]
    durations = [transcription._audio_duration(part) for part, _ in parts]
    assert all(0 < duration <= 600 for duration in durations)
    assert abs(sum(durations) - samples / 16000) <= 1 / 16000
    for part, _ in parts:
        with av.open(str(part)) as source:
            stream = source.streams.audio[0]
            assert stream.sample_rate == 16000 and stream.layout.name == 'mono'
            assert sum(frame.samples for frame in source.decode(stream)) > 0
