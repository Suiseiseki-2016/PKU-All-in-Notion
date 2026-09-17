"""Unit tests for the HLS fast-download path (parsers + decryption)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import media
from pku_sync.media import (
    HlsSegment,
    _decrypt_segment,
    _parse_media_playlist,
    _parse_iv,
    _split_attributes,
)

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="https://h.pku.edu.cn/key.bin",IV=0x0000000000000000000000000000000A
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:9.0,
seg2.ts
"""


def test_split_attributes_respects_quotes():
    parts = _split_attributes('METHOD=AES-128,URI="a,b.ts",IV=0x1')
    assert parts == ['METHOD=AES-128', 'URI="a,b.ts"', "IV=0x1"]


def test_parse_iv_hex_to_16_bytes():
    assert _parse_iv("0x0A") == (10).to_bytes(16, "big")
    assert _parse_iv("0x") is None
    assert _parse_iv("nope") is None


def test_parse_media_playlist_key_scope_and_iv_fallback():
    segments, is_master = _parse_media_playlist(PLAYLIST, "https://h.pku.edu.cn/play/x.m3u8")
    assert not is_master
    assert len(segments) == 3
    s0, s1, s2 = segments
    assert s0.uri == "https://h.pku.edu.cn/play/seg0.ts"
    assert s0.key_uri == "https://h.pku.edu.cn/key.bin"
    # Explicit IV from the tag applies to both key-scoped segments…
    assert s0.iv == (10).to_bytes(16, "big")
    assert s1.iv == (10).to_bytes(16, "big")
    # …and the tag's IV is carried by the segment that switches scope.
    assert s1.key_uri == "https://h.pku.edu.cn/key.bin"
    # Cleartext scope after the NONE key.
    assert s2.key_uri is None and s2.iv is None


def test_parse_media_playlist_master_and_unsupported():
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nhi/index.m3u8\n"
    segments, is_master = _parse_media_playlist(master, "https://h/x.m3u8")
    assert is_master and not segments

    bad = "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"k\"\n#EXTINF:1,\na.ts\n"
    segments, is_master = _parse_media_playlist(bad, "https://h/x.m3u8")
    assert not is_master and not segments


def test_decrypt_segment_roundtrip_aes128():
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = b"k" * 16
    iv = (7).to_bytes(16, "big")
    plaintext = b"compressed lecture bytes \xe4\xb8\xad\xe6\x96\x87" * 4
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encrypted = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encrypted.update(padded) + encrypted.finalize()
    assert _decrypt_segment(ciphertext, key, iv) == plaintext


def _fake_cookie_jar():
    """minimal ``client.cookies.jar`` for cookie_header()"""
    return SimpleNamespace(cookies=SimpleNamespace(jar=[]))


def test_fetch_segment_skips_existing(tmp_path):
    seg = HlsSegment(uri="https://h/seg0.ts", key_uri=None, iv=None)
    target = tmp_path / "seg_00000.ts"
    target.write_bytes(b"already")
    client = SimpleNamespace(**vars(_fake_cookie_jar()))
    client.get = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no fetch"))
    assert media._fetch_segment(client, seg, 0, 100, target) is target


def test_fetch_segment_downloads_cleartext_with_headers(tmp_path):
    seen: dict = {}

    class Resp:
        status_code = 200
        content = b"payload-one"

    class Client:
        cookies = SimpleNamespace(jar=[])

        def get(self, url, headers=None, follow_redirects=None):
            seen["url"] = url
            seen["headers"] = headers
            return Resp()

    target = tmp_path / "seg_00000.ts"
    seg = HlsSegment(uri="https://h/seg0.ts", key_uri=None, iv=None)
    out = media._fetch_segment(Client(), seg, 0, 100, target)
    assert out.read_bytes() == b"payload-one"
    assert seen["url"] == "https://h/seg0.ts"
    assert seen["headers"]["Referer"] == media.PLAYER_REFERER
    assert "Cookie" in seen["headers"]
    assert not (tmp_path / "seg_00000.tmp").exists()


def test_download_hls_segments_concurrent_and_remux(tmp_path, monkeypatch):
    playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
seg0.ts
#EXTINF:4.0,
seg1.ts
#EXTINF:4.0,
seg2.ts
"""
    responses = {
        "https://h/x.m3u8": SimpleNamespace(status_code=200, text=playlist, url="https://h/x.m3u8"),
        "https://h/seg0.ts": SimpleNamespace(status_code=200, content=b"AAA"),
        "https://h/seg1.ts": SimpleNamespace(status_code=200, content=b"BBB"),
        "https://h/seg2.ts": SimpleNamespace(status_code=200, content=b"CCC"),
    }

    class Client:
        cookies = SimpleNamespace(jar=[])

        def get(self, url, headers=None, follow_redirects=None):
            return responses[url]

    target = tmp_path / "video.mp4"

    def fake_ffmpeg(argv, **kwargs):
        # Concatenate the segments back together to emulate the remux.
        merged = Path(argv[argv.index("-i") + 1])
        data = merged.read_bytes()
        out_path = Path(argv[-1])
        out_path.write_bytes(data)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(media.subprocess, "run", fake_ffmpeg)

    result = media._download_hls_segments(Client(), "https://h/x.m3u8", target, workers=4)
    assert result.path is not None and not result.error, result.error
    assert target.read_bytes() == b"AAABBBCCC"
    # Cache dir is cleaned up after a successful remux.
    assert not (tmp_path / ".video.mp4.hls").exists()


def test_download_hls_segments_skips_existing_target(tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * (1_000_001))

    class Client:
        cookies = SimpleNamespace(jar=[])

        def get(self, *a, **kw):
            raise AssertionError("existing target must skip the playlist fetch")

    result = media._download_hls_segments(Client(), "https://h/x.m3u8", target)
    assert result.skipped and result.path == target


def test_download_hls_segments_master_falls_back(tmp_path):
    class Client:
        cookies = SimpleNamespace(jar=[])

        def get(self, url, headers=None, follow_redirects=None):
            return SimpleNamespace(
                status_code=200,
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8\nv.m3u8\n",
                url="https://h/x.m3u8",
            )

    result = media._download_hls_segments(Client(), "https://h/x.m3u8", tmp_path / "video.mp4")
    assert result.error and "master playlist" in result.error
