"""English Whisper worker using the audiobook project's installed ASR environment."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    import torch
    import whisper

    model_name = os.environ.get("SUBTITLE_ASR_MODEL", "medium")
    model_cache = Path.home() / ".cache" / "whisper" / f"{model_name}.pt"
    if not model_cache.is_file():
        model_name = "small"
        model_cache = Path.home() / ".cache" / "whisper" / "small.pt"
        if not model_cache.is_file():
            raise RuntimeError("В локальном кэше не найдены модели Whisper medium.pt и small.pt; ASR не загружает модели из интернета.")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = whisper.load_model(model_name, device=device)
    except RuntimeError as exc:
        if device != "cuda" or not any(word in str(exc).lower() for word in ("cuda", "cublas", "cudnn", "out of memory")):
            raise
        device = "cpu"
        model = whisper.load_model(model_name, device=device)

    protocol_out = sys.stdout
    protocol_out.write("@@ready\n")
    protocol_out.flush()
    sys.stdout = sys.stderr

    for line in sys.stdin:
        try:
            video_path = json.loads(line)["video"]
            audio = whisper.load_audio(video_path)
            sample_rate = 16000
            duration = len(audio) / sample_rate
            chunk_seconds = 300
            overlap_seconds = 8
            words = []
            for core_start in range(0, max(1, int(duration)), chunk_seconds):
                core_end = min(duration, core_start + chunk_seconds)
                audio_start = max(0, core_start - overlap_seconds)
                audio_end = min(duration, core_end + overlap_seconds)
                chunk = audio[int(audio_start * sample_rate):int(audio_end * sample_rate)]
                try:
                    result = model.transcribe(
                        chunk,
                        language="en",
                        task="transcribe",
                        word_timestamps=True,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        fp16=(device == "cuda"),
                        verbose=None,
                    )
                except RuntimeError as exc:
                    if device != "cuda" or not any(word in str(exc).lower() for word in ("cuda", "cublas", "cudnn", "out of memory")):
                        raise
                    print(f"ASR GPU недоступна, повторяю отрезок {core_start // chunk_seconds + 1} на CPU", file=sys.stderr, flush=True)
                    model = None
                    torch.cuda.empty_cache()
                    device = "cpu"
                    model = whisper.load_model(model_name, device=device)
                    result = model.transcribe(
                        chunk,
                        language="en",
                        task="transcribe",
                        word_timestamps=True,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        fp16=False,
                        verbose=None,
                    )
                for item in result.get("segments", []):
                    item_words = item.get("words") or []
                    if not item_words:
                        start = audio_start + float(item["start"])
                        end = audio_start + float(item["end"])
                        if end - start > 5.2:
                            print(f"ASR_UNALIGNED_SKIP {start:.2f}-{end:.2f}s", file=sys.stderr, flush=True)
                            continue
                        if core_start <= (start + end) / 2 < core_end and item.get("text", "").strip():
                            words.append({"start": start, "end": end, "text": item["text"]})
                        continue
                    for word in item_words:
                        start = audio_start + float(word["start"])
                        end = audio_start + float(word["end"])
                        if core_start <= (start + end) / 2 < core_end and word.get("word", "").strip():
                            words.append({"start": start, "end": end, "text": word["word"]})
                percent = min(100, int(core_end * 100 / duration)) if duration else 100
                print(f"ASR_PROGRESS {percent}%", file=sys.stderr, flush=True)
            words.sort(key=lambda item: (item["start"], item["end"]))
            response = {"words": words, "model": model_name, "device": device}
        except Exception as exc:  # noqa: BLE001
            response = {"error": f"{type(exc).__name__}: {exc}"}
        protocol_out.write("@@" + json.dumps(response, ensure_ascii=True) + "\n")
        protocol_out.flush()


if __name__ == "__main__":
    main()
