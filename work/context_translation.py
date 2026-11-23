"""Run the local Qwen GGUF model directly with llama.cpp and video context."""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from typing import Callable, Sequence


BATCH_SIZE = 12
MODEL_FILENAME = "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
MMPROJ_FILENAME = "mmproj-model-f16.gguf"
MODEL_SEARCH_ROOTS = (
    Path(r"J:\models\lmstudio-community\Qwen2.5-VL-7B-Instruct-GGUF"),
    Path.home() / ".lmstudio" / "models" / "lmstudio-community" / "Qwen2.5-VL-7B-Instruct-GGUF",
)
MODEL_ALIAS = "subtitle-qwen"


def default_model_path() -> Path:
    for root in MODEL_SEARCH_ROOTS:
        candidate = root / MODEL_FILENAME
        if candidate.is_file():
            return candidate
    return MODEL_SEARCH_ROOTS[0] / MODEL_FILENAME


class LocalContextTranslator:
    """Starts its own loopback llama.cpp process; never starts LM Studio."""

    def __init__(self, video: str, logger: logging.Logger, model_path: str | Path | None = None):
        self.video = video
        self.logger = logger
        self.model_path = Path(model_path) if model_path else default_model_path()
        self.mmproj_path = self.model_path.parent / MMPROJ_FILENAME
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.log_handle = None
        self.available = False
        self.api_root = ""
        self._start_server()

    @staticmethod
    def find_server() -> Path | None:
        explicit = os.environ.get("SUBTITLE_LLAMA_SERVER")
        if explicit and Path(explicit).is_file():
            return Path(explicit)
        bundled = Path(__file__).resolve().parent.parent / "runtime" / "llama-server.exe"
        if bundled.is_file():
            return bundled

        backends = Path.home() / ".lmstudio" / "extensions" / "backends"
        patterns = (
            "llama.cpp-win-x86_64-vulkan-avx2-*",
            "llama.cpp-win-x86_64-avx2-*",
        )
        for pattern in patterns:
            folders = sorted(backends.glob(pattern), key=LocalContextTranslator._version_key, reverse=True)
            for folder in folders:
                server = folder / "llama-server.exe"
                if server.is_file():
                    return server
        return shutil.which("llama-server") and Path(shutil.which("llama-server"))

    @staticmethod
    def _version_key(path: Path) -> tuple[int, ...]:
        found = re.search(r"-(\d+(?:\.\d+)+)$", path.name)
        return tuple(int(part) for part in found.group(1).split(".")) if found else (0,)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _start_server(self) -> None:
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Не найден файл Qwen GGUF: {self.model_path}")
        if not self.mmproj_path.is_file():
            raise FileNotFoundError(f"Рядом с Qwen не найден файл обработки изображений: {self.mmproj_path}")
        server = self.find_server()
        if not server:
            raise FileNotFoundError("Не найден llama-server.exe. Укажите его через переменную SUBTITLE_LLAMA_SERVER.")

        self.port = self._free_port()
        self.api_root = f"http://127.0.0.1:{self.port}"
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubtitleTranslator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_handle = (log_dir / "qwen_engine.log").open("ab", buffering=0)
        command = [
            str(server), "-m", str(self.model_path), "--mmproj", str(self.mmproj_path),
            "--host", "127.0.0.1", "--port", str(self.port), "--alias", MODEL_ALIAS,
            "-c", "8192", "-ngl", "99", "--no-webui",
        ]
        environment = os.environ.copy()
        environment["PATH"] = str(server.parent) + os.pathsep + environment.get("PATH", "")
        self.logger.info("Запускаю автономный llama.cpp: %s", server)
        self.logger.info("Загружаю Qwen: %s", self.model_path)
        self.process = subprocess.Popen(
            command, cwd=str(server.parent), env=environment,
            stdin=subprocess.DEVNULL, stdout=self.log_handle, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"llama.cpp завершился при загрузке модели (код {self.process.returncode}). См. qwen_engine.log")
            try:
                request = urllib.request.Request(f"{self.api_root}/health")
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status == 200:
                        self.available = True
                        self.logger.info("Qwen загружена автономным llama.cpp на порту %d", self.port)
                        return
            except Exception:
                pass
            time.sleep(1)
        self.close()
        raise TimeoutError("Qwen не успела загрузиться за 4 минуты. Подробности: qwen_engine.log")

    def _request(self, payload: dict, timeout: int = 15) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_root}/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

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
        response = self._request({
            "model": MODEL_ALIAS,
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

    def close(self) -> None:
        self.available = False
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
