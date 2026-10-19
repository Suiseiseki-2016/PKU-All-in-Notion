"""Private OSS-backed Bailian file transcription.

Audio is streamed to a private OSS object. DashScope receives only a
short-lived signed GET URL. The object is deleted after the task completes,
including when the upstream task fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, BinaryIO, Callable
from urllib.request import urlopen

try:
    import oss2
except ImportError:  # pragma: no cover
    oss2 = None  # type: ignore[assignment]
try:
    from dashscope.audio.asr import Transcription
except ImportError:  # pragma: no cover
    Transcription = None  # type: ignore[assignment]

MODEL = "qwen-audio-3.0-asr-flash-filetrans"
SIGNED_URL_SECONDS = 30 * 60

# Audit logger for the transient OSS object lifecycle. The M4 Window A
# deletion proof (VAL-DEDUP-030) runs the least-privilege probe key that has
# no ListObjects grant -- it learns each temp key from the relay's own stdout
# log (pku-temp/<date>/<uuid>.<ext>; uuid + extension only, never the original
# filename, a signed URL, or any credential) and head_object-checks it.
ASR_AUDIT_LOGGER = "server.asr"
_audit_logger = logging.getLogger(ASR_AUDIT_LOGGER)


def configure_asr_audit_logging() -> None:
    """Wire the ASR audit logger to INFO on stdout (native uvicorn default).

    uvicorn's default logging config only wires its own loggers
    (``uvicorn.error`` / ``uvicorn.access``) and leaves the root logger
    without a handler, so INFO records from ``server.asr`` would otherwise be
    dropped. The production factory (``server.main:app``) calls this once so
    the upload/delete audit lines surface in the relay's stdout stream by
    default and the committed Window A probe's ``--relay-log`` key-diff works
    unmodified. Tests capture the same records through pytest caplog instead
    and never call this.
    """
    logger = logging.getLogger(ASR_AUDIT_LOGGER)
    # NOTSET (0) means "inherit from root", whose default level is WARNING,
    # so the asr logger must be lifted to INFO explicitly; an operator's
    # explicit DEBUG/INFO setting is kept.
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    # The logger carries its own stdout handler, so records must not also
    # propagate to a root logger that an operator may configure later.
    logger.propagate = False


class AsrError(RuntimeError):
    """The upstream vendor rejected or could not complete transcription."""


class _HashingReader:
    def __init__(self, source: BinaryIO):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        if chunk:
            self.digest.update(chunk)
        return chunk


@dataclass(frozen=True)
class UploadedAudio:
    key: str
    sha256: str


class OssTempStore:
    """Only manages short-lived objects under the dedicated pku-temp prefix."""

    def __init__(self, bucket: Any):
        self.bucket = bucket

    @classmethod
    def from_environment(cls) -> "OssTempStore | None":
        names = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT")
        if any(not os.environ.get(name) for name in names):
            return None
        auth = oss2.Auth(
            os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]
        )
        return cls(oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"]))

    def upload(self, source: BinaryIO, filename: str) -> UploadedAudio:
        suffix = PurePath(filename).suffix.lower()
        suffix = suffix if suffix and suffix[1:].isalnum() else ".audio"
        key = f"pku-temp/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}{suffix}"
        reader = _HashingReader(source)
        result = self.bucket.put_object(key, reader)
        if result.status != 200:
            raise AsrError(f"OSS upload failed (HTTP {result.status})")
        # INFO audit line for the Window A deletion proof: exactly the temp
        # key (uuid + extension), nothing derived from the original filename.
        from . import account_evidence
        observed = account_evidence.head_object(self, key)
        account_evidence.record_object(key, "created", exists=observed["exists"], size=observed["bytes"])
        _audit_logger.info("oss upload: %s", key)
        return UploadedAudio(key=key, sha256=reader.digest.hexdigest())

    def signed_download_url(self, key: str) -> str:
        return self.bucket.sign_url("GET", key, SIGNED_URL_SECONDS, slash_safe=True)

    def delete(self, key: str) -> None:
        self.bucket.delete_object(key)
        # Logged only after the delete call returns: a successful delete.
        from . import account_evidence
        observed = account_evidence.head_object(self, key)
        account_evidence.record_object(key, "deleted" if observed["exists"] is False else "delete-unverified",
                                       exists=observed["exists"], size=observed["bytes"])
        _audit_logger.info("oss delete: %s", key)


def _download_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - vendor result URL
        return json.load(response)


class BailianFileAsr:
    """Adapt the DashScope asynchronous file API to the relay payload shape."""

    def __init__(
        self,
        store: OssTempStore,
        api_key: str,
        transcription_api: Any = Transcription,
        download_json: Callable[[str], dict[str, Any]] = _download_json,
    ):
        self.store = store
        self.api_key = api_key
        self.transcription_api = transcription_api
        self.download_json = download_json

    @classmethod
    def from_environment(cls) -> "BailianFileAsr | None":
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        store = OssTempStore.from_environment()
        return cls(store, api_key) if api_key and store else None

    def __call__(self, source: BinaryIO, filename: str) -> dict[str, Any]:
        uploaded = self.store.upload(source, filename)
        try:
            return self.transcribe_uploaded(uploaded.key)
        finally:
            self.store.delete(uploaded.key)

    def transcribe_uploaded(self, key: str) -> dict[str, Any]:
        """Transcribe a private OSS object already staged by the client."""
        submitted = self.transcription_api.async_call(
            model=MODEL,
            file_urls=[self.store.signed_download_url(key)],
            api_key=self.api_key,
            diarization_enabled=True,
            timestamp_alignment_enabled=True,
        )
        if submitted.status_code != 200:
            raise AsrError(f"DashScope submission failed: {submitted.message}")
        completed = self.transcription_api.wait(
            submitted, api_key=self.api_key, wait_timeout=1800
        )
        if completed.status_code != 200:
            raise AsrError(f"DashScope transcription failed: {completed.message}")
        results = completed.output.get("results", [])
        if not results or not results[0].get("transcription_url"):
            raise AsrError("DashScope returned no transcription result")
        return self._to_payload(self.download_json(results[0]["transcription_url"]))

    @staticmethod
    def _to_payload(result: dict[str, Any]) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        duration_ms = 0
        for transcript in result.get("transcripts", []):
            for sentence in transcript.get("sentences", []):
                start = int(sentence.get("begin_time", 0))
                end = int(sentence.get("end_time", start))
                duration_ms = max(duration_ms, end)
                segments.append(
                    {
                        "start": start / 1000,
                        "end": end / 1000,
                        "text": sentence.get("text", ""),
                    }
                )
        if not segments:
            raise AsrError("DashScope returned no transcript sentences")
        return {
            "language": result.get("properties", {}).get("language", "zh"),
            "duration_seconds": math.ceil(duration_ms / 1000),
            "segments": segments,
        }
