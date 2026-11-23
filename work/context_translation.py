"""Optional, fully local subtitle translation with dialogue and video context.

Uses an already-running LM Studio OpenAI-compatible server. The app remains
usable without it: callers can fall back to their bundled Argos translator.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request
from typing import Callable, Sequence


API_ROOT = "http://127.0.0.1:1234/v1"
BATCH_SIZE = 12


class LocalContextTranslator:
    def __init__(self, video: str, logger: logging.Logger):
        self.video = video
        self.logger = logger
        self.model = self._find_model()

    @property
    def available(self) -> bool:
        return self.model is not None

    def _request(self, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{API_ROOT}/{path}", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _find_model(self) -> str | None:
        try:
            lms = shutil.which("lms")
            if not lms:
                candidate = Path.home() / ".lmstudio" / "bin" / "lms.exe"
                lms = str(candidate) if candidate.is_file() else None
            if not lms:
                self.logger.info("LM Studio CLI не найден; используется встроенный переводчик")
                return None
            proc = subprocess.run([lms, "ps", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
            loaded = json.loads(proc.stdout)
            models = [
                {"id": item.get("identifier") or item.get("modelKey", ""), "key": item.get("modelKey", "")}
                for item in loaded
                if item.get("type") == "llm" and item.get("status") in ("idle", "loaded", "processing")
            ]
        except Exception as exc:
            self.logger.info("Не удалось проверить загруженную локальную модель: %s", exc)
            return None
        for item in models:
            model_id = str(item.get("id", ""))
            model_key = str(item.get("key", ""))
            if "qwen2.5-vl" in (model_id + " " + model_key).lower():
                self.logger.info("Локальная модель контекста: %s", model_id)
                return model_id
        self.logger.info("В LM Studio не загружена Qwen2.5-VL; используется встроенный переводчик")
        return None

    def _frame_data_url(self, seconds: float) -> str | None:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(max(0, seconds)),
                 "-i", self.video, "-frames:v", "1", "-vf", "scale=640:-2",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=45, check=True,
            )
            if proc.stdout:
                return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")
        except Exception:
            self.logger.exception("Не удалось получить кадр для визуального контекста (%.2f сек.)", seconds)
        return None

    @staticmethod
    def _extract_translations(content: str) -> dict[int, str]:
        match = re.search(r"\[[\s\S]*\]", content)
        if not match:
            raise ValueError("Модель не вернула JSON-массив переводов")
        parsed = json.loads(match.group(0))
        result = {}
        for item in parsed:
            if isinstance(item, dict) and "id" in item and isinstance(item.get("ru"), str):
                result[int(item["id"])] = item["ru"].strip()
        return result

    def translate_batch(self, segments: Sequence[tuple[float, float, str]],
                        first_id: int, surrounding: Sequence[str]) -> list[str]:
        frame_time = sum((start + end) / 2 for start, end, _ in segments) / len(segments)
        frame = self._frame_data_url(frame_time)
        rows = [{"id": first_id + i, "en": text} for i, (_, _, text) in enumerate(segments)]
        prompt = (
            "Translate these English subtitle lines into natural Russian. Use the video frame and nearby dialogue "
            "to resolve pronouns, names, tone, and ambiguous words. Keep every line faithful and concise. "
            "Preserve profanity and vulgar wording at the same intensity; do not censor, euphemize, add, or omit meaning. "
            "Do not describe the image in the translations. Keep one Russian line for each id. "
            "Return only a JSON array of objects with integer id and string ru, for example "
            '[{"id":1,"ru":"Перевод"}].\n\n'
            "Nearby dialogue (context only):\n" + "\n".join(surrounding) +
            "\n\nTranslate these lines:\n" + json.dumps(rows, ensure_ascii=False)
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        if frame:
            content.append({"type": "image_url", "image_url": {"url": frame}})
        response = self._request("chat/completions", {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.15,
            "max_tokens": max(700, len(segments) * 100),
        }, timeout=240)
        text = response["choices"][0]["message"]["content"]
        translations = self._extract_translations(text)
        expected_ids = set(range(first_id, first_id + len(segments)))
        if set(translations) != expected_ids or any(not translations[i] for i in expected_ids):
            raise ValueError("Модель вернула неполный или пустой набор переводов")
        return [translations[first_id + i] for i in range(len(segments))]

    def translate(self, segments: Sequence[tuple[float, float, str]],
                  progress: Callable[[int, int], None]) -> tuple[list[str], set[int]]:
        results: list[str] = [""] * len(segments)
        failed: set[int] = set()
        for offset in range(0, len(segments), BATCH_SIZE):
            batch = segments[offset:offset + BATCH_SIZE]
            lo, hi = max(0, offset - 4), min(len(segments), offset + len(batch) + 4)
            context_rows = [f"{i + 1}: {segments[i][2]}" for i in range(lo, hi)]
            try:
                batch_translations = self.translate_batch(batch, offset + 1, context_rows)
                results[offset:offset + len(batch)] = batch_translations
            except Exception:
                self.logger.exception("Контекстный перевод не удался для реплик %d–%d", offset + 1, offset + len(batch))
                failed.update(range(offset, offset + len(batch)))
            progress(min(len(segments), offset + len(batch)), len(segments))
        return results, failed
