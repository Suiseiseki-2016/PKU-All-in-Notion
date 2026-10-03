"""Run a bounded, fully offline ASR comparison on a local lecture excerpt."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--offset", type=float, default=0)
    parser.add_argument("--hotwords", default="")
    parser.add_argument("--model", default="small")
    args = parser.parse_args()
    if not args.audio.is_file():
        parser.error("audio file does not exist")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    from faster_whisper import WhisperModel

    started = time.perf_counter()
    model = WhisperModel(args.model, device="cuda", compute_type="float16",
                         local_files_only=True)
    segments, info = model.transcribe(
        str(args.audio), language="zh", beam_size=5, vad_filter=False,
        hotwords=args.hotwords or None,
    )
    rows = [{"start": round(args.offset + part.start, 3),
             "end": round(args.offset + part.end, 3),
             "text": part.text.strip()} for part in segments]
    result = {"model": args.model, "device": "cuda", "compute_type": "float16",
              "local_files_only": True, "hotwords": args.hotwords,
              "offset_seconds": args.offset,
              "duration_seconds": round(info.duration, 3),
              "elapsed_seconds": round(time.perf_counter() - started, 3),
              "segments": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps({"duration_seconds": result["duration_seconds"],
                      "elapsed_seconds": result["elapsed_seconds"],
                      "segments": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
