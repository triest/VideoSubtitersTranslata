from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Перевод субтитров"
VIDEO_TYPES = ("Видео", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v")


def configure_logging():
    logger = logging.getLogger("subtitle_translator")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    candidates = [
        Path(__file__).resolve().parent / "logs",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubtitleTranslator" / "logs",
    ]
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "subtitle_translator.log"
            handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
            logger.addHandler(handler)
            return logger, log_file
        except OSError:
            continue
    logger.addHandler(logging.NullHandler())
    return logger, candidates[-1] / "subtitle_translator.log"


LOGGER, LOG_FILE = configure_logging()


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def ffmpeg_subtitle_path(path: Path) -> str:
    """Escape a filename for FFmpeg's subtitles filter on Windows and Unix."""
    value = path.resolve().as_posix()
    value = value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return value


class SubtitleApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("690x440")
        self.minsize(600, 400)
        self.video_path: Path | None = None
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self._build_ui()
        self.after(100, self._poll_events)
        LOGGER.info("Приложение запущено")

    def report_callback_exception(self, exc, value, tb) -> None:
        LOGGER.error("Необработанная ошибка интерфейса", exc_info=(exc, value, tb))
        messagebox.showerror(APP_TITLE, f"Ошибка интерфейса. Подробности записаны в файл:\n{LOG_FILE}")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Английская речь → русские субтитры", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Распознавание и перевод работают локально. Модель перевода уже включена в пакет.", wraplength=630).pack(anchor="w", pady=(6, 18))

        choose = ttk.Frame(outer)
        choose.pack(fill="x")
        self.path_label = ttk.Label(choose, text="Видео не выбрано", wraplength=490)
        self.path_label.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.choose_btn = ttk.Button(choose, text="Выбрать видео…", command=self.choose_video)
        self.choose_btn.pack(side="right")

        opts = ttk.LabelFrame(outer, text="Настройки", padding=12)
        opts.pack(fill="x", pady=18)
        row = ttk.Frame(opts)
        row.pack(fill="x")
        ttk.Label(row, text="Распознавание:").pack(side="left")
        self.model = tk.StringVar(value="small")
        ttk.Combobox(row, textvariable=self.model, values=("tiny", "base", "small", "medium", "large-v3"), state="readonly", width=11).pack(side="left", padx=(8, 18))
        ttk.Label(row, text="Переводчик: встроенная модель EN → RU").pack(side="left")
        ttk.Label(opts, text="Перевод выполняется без Ollama и интернета. Модель встроена в программу.", wraplength=620).pack(anchor="w", pady=(8, 0))
        self.burn_in = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Вшить субтитры в выходной MP4 (дополнительно сохранится SRT)", variable=self.burn_in).pack(anchor="w", pady=(12, 0))

        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(3, 7))
        self.status = tk.StringVar(value="Готово к работе")
        ttk.Label(outer, textvariable=self.status, wraplength=630).pack(anchor="w")
        self.start_btn = ttk.Button(outer, text="Распознать, перевести и сохранить", command=self.start)
        self.start_btn.pack(anchor="e", pady=(22, 0))

    def choose_video(self) -> None:
        name = filedialog.askopenfilename(title="Выберите видео на английском", filetypes=(VIDEO_TYPES, ("Все файлы", "*.*")))
        if name:
            self.video_path = Path(name)
            self.path_label.configure(text=str(self.video_path))

    def start(self) -> None:
        if not self.video_path or not self.video_path.is_file():
            LOGGER.warning("Обработка не начата: видеофайл не выбран или недоступен")
            messagebox.showinfo(APP_TITLE, "Сначала выберите видеофайл.")
            return
        if not shutil.which("ffmpeg"):
            LOGGER.error("Не найден ffmpeg в PATH")
            messagebox.showerror("Не найден FFmpeg", f"Установите FFmpeg и добавьте его в PATH.\nЛог: {LOG_FILE}")
            return
        self.start_btn.configure(state="disabled")
        self.choose_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.set("Подготовка… При первом запуске модель распознавания скачается автоматически.")
        self.worker = threading.Thread(target=self._process, args=(self.video_path, self.model.get(), self.burn_in.get()), daemon=True)
        self.worker.start()

    def _process(self, video: Path, model_name: str, burn: bool) -> None:
        try:
            from faster_whisper import WhisperModel
            from argostranslate import package, translate

            self.events.put(("progress", 1, "Подключаю встроенный переводчик…"))
            model_file = Path(__file__).resolve().parent / "models" / "translate-en_ru-1_9.argosmodel"
            installed = translate.get_installed_languages()
            en = next((lang for lang in installed if lang.code == "en"), None)
            ru = next((lang for lang in installed if lang.code == "ru"), None)
            if not en or not ru or not any(t.to_lang.code == "ru" for t in en.translations):
                if not model_file.is_file():
                    raise RuntimeError("Не найдена встроенная модель перевода. Распакуйте программу полностью вместе с папкой models.")
                package.install_from_path(str(model_file))
                installed = translate.get_installed_languages()
                en = next((lang for lang in installed if lang.code == "en"), None)
                ru = next((lang for lang in installed if lang.code == "ru"), None)
            if not en or not ru:
                raise RuntimeError("Не удалось инициализировать встроенную модель английского → русского перевода.")
            translator = en.get_translation(ru)

            self.events.put(("progress", 2, f"Загружаю модель {model_name}…"))
            model = WhisperModel(model_name, device="auto", compute_type="int8")
            self.events.put(("progress", 5, "Распознаю английскую речь…"))
            duration = self._probe_duration(video)
            segments, info = model.transcribe(str(video), language="en", vad_filter=True, beam_size=5)
            source = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    source.append((float(seg.start), float(seg.end), text))
                if duration:
                    self.events.put(("progress", min(25, 5 + int(20 * float(seg.end) / duration)), "Распознаю английскую речь…"))
            if not source:
                raise RuntimeError("Речь не найдена. Проверьте, что в видео есть английская речь.")

            translated = []
            failed = 0
            total = len(source)
            for i, (start, end, original) in enumerate(source, 1):
                try:
                    translated_text = translator.translate(original).strip()
                    if not translated_text:
                        raise RuntimeError("Пустой ответ переводчика")
                except Exception:
                    LOGGER.exception("Ошибка перевода субтитра %d (%.3f–%.3f сек.)", i, start, end)
                    translated_text = original
                    failed += 1
                translated.append((start, end, translated_text))
                if i % 4 == 0 or i == total:
                    self.events.put(("progress", int(25 + 53 * i / total), f"Перевод: {i} из {total}"))

            srt = video.with_name(video.stem + ".ru.srt")
            with srt.open("w", encoding="utf-8-sig", newline="\n") as f:
                for i, (start, end, text) in enumerate(translated, 1):
                    f.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")
            self.events.put(("progress", 80, "SRT сохранён. Готовлю выходной MP4…" if burn else "SRT сохранён."))

            output = video.with_name(video.stem + ".ru.mp4")
            if burn:
                filter_path = ffmpeg_subtitle_path(srt)
                command = ["ffmpeg", "-y", "-i", str(video), "-vf", f"subtitles='{filter_path}'", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                assert proc.stdout is not None
                ffmpeg_tail = []
                for line in proc.stdout:
                    ffmpeg_tail.append(line.rstrip())
                    if len(ffmpeg_tail) > 60:
                        del ffmpeg_tail[0]
                    match = re.match(r"out_time_ms=(\d+)", line)
                    if match and duration:
                        percent = min(99, 80 + int(19 * int(match.group(1)) / (duration * 1_000_000)))
                        self.events.put(("progress", percent, "Вшиваю субтитры в MP4…"))
                code = proc.wait()
                if code:
                    LOGGER.error("FFmpeg завершился с кодом %s. Последний вывод:\n%s", code, "\n".join(ffmpeg_tail))
                    raise RuntimeError("FFmpeg не смог обработать видео. Проверьте logs\subtitle_translator.log.")
            note = f"\nНе удалось перевести фрагментов: {failed}; в этих строках оставлен английский текст." if failed else ""
            self.events.put(("done", f"Готово!\nSRT: {srt}\n" + (f"MP4: {output}" if burn else "Вшивание в видео отключено.") + note))
        except Exception as exc:
            LOGGER.exception("Ошибка обработки видео %s", video)
            self.events.put(("error", f"{exc}\n\nПодробный лог: {LOG_FILE}"))

    @staticmethod
    def _probe_duration(video: Path) -> float | None:
        try:
            out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)], text=True, stderr=subprocess.DEVNULL).strip()
            return float(out)
        except Exception:
            return None

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "status":
                    self.status.set(event[1])
                elif event[0] == "progress":
                    _, value, label = event
                    self.progress.configure(value=value)
                    self.status.set(label)
                elif event[0] == "done":
                    self.progress.configure(value=100)
                    self.status.set("Готово")
                    messagebox.showinfo("Обработка завершена", event[1])
                    self._unlock()
                elif event[0] == "error":
                    self.status.set("Обработка не завершена")
                    messagebox.showerror("Ошибка обработки", event[1] + f"\n\nЛог ошибок: {LOG_FILE}")
                    self._unlock()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _unlock(self) -> None:
        self.start_btn.configure(state="normal")
        self.choose_btn.configure(state="normal")


if __name__ == "__main__":
    try:
        SubtitleApp().mainloop()
    except Exception:
        LOGGER.exception("Критическая ошибка при запуске приложения")
        raise
