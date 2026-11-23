from __future__ import annotations

import logging
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import uuid
import tkinter as tk
from logging.handlers import RotatingFileHandler
from datetime import datetime
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
        self.geometry("740x500")
        self.minsize(650, 460)
        self.video_path: Path | None = None
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.settings_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubtitleTranslator" / "settings.json"
        try:
            saved_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved_settings = {}
        if not isinstance(saved_settings, dict):
            saved_settings = {}
        try:
            from work.context_translation import default_model_path
            default_qwen_path = str(default_model_path())
        except Exception:
            default_qwen_path = r"J:\models\lmstudio-community\Qwen2.5-VL-7B-Instruct-GGUF\Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
        translation_mode = saved_settings.get("translation_mode", "Qwen2.5-VL 7B")
        if translation_mode not in ("Qwen2.5-VL 7B", "Argos — встроенный"):
            translation_mode = "Qwen2.5-VL 7B"
        self.translation_mode = tk.StringVar(value=translation_mode)
        self.qwen_model_path = tk.StringVar(value=saved_settings.get("qwen_model_path", default_qwen_path))
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
        ttk.Label(outer, text="Распознавание использует локальный ASR из J:\\audiobook; встроенный перевод работает без интернета.", wraplength=630).pack(anchor="w", pady=(6, 18))

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
        self.model = tk.StringVar(value="medium")
        ttk.Combobox(row, textvariable=self.model, values=("small", "medium"), state="readonly", width=11).pack(side="left", padx=(8, 18))
        ttk.Label(row, text="Перевод:").pack(side="left")
        ttk.Combobox(row, textvariable=self.translation_mode, values=("Qwen2.5-VL 7B", "Argos — встроенный"), state="readonly", width=24).pack(side="left", padx=(8, 0))
        model_row = ttk.Frame(opts)
        model_row.pack(fill="x", pady=(10, 0))
        ttk.Label(model_row, text="Файл Qwen:").pack(side="left")
        ttk.Entry(model_row, textvariable=self.qwen_model_path).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(model_row, text="Выбрать…", command=self.choose_qwen_model).pack(side="right")
        ttk.Label(opts, text="Qwen запускается отдельным локальным llama.cpp: LM Studio открывать не нужно. Если Qwen или движок недоступны, используется Argos.", wraplength=660).pack(anchor="w", pady=(8, 0))
        self.burn_in = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Вшить субтитры в исходный MP4 (оригинал сохранится в резервной копии)", variable=self.burn_in).pack(anchor="w", pady=(12, 0))

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

    def choose_qwen_model(self) -> None:
        current = Path(self.qwen_model_path.get())
        name = filedialog.askopenfilename(
            title="Выберите локальный файл модели Qwen",
            initialdir=str(current.parent) if current.parent.exists() else str(Path.home()),
            filetypes=(("Модель GGUF", "*.gguf"), ("Все файлы", "*.*")),
        )
        if name:
            self.qwen_model_path.set(name)
            self._save_settings()

    def _save_settings(self) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps({
                "translation_mode": self.translation_mode.get(),
                "qwen_model_path": self.qwen_model_path.get().strip(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Не удалось сохранить настройки модели")

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
        self.status.set("Подготовка локального распознавания…")
        self._save_settings()
        self.worker = threading.Thread(target=self._process, args=(self.video_path, self.model.get(), self.burn_in.get(), self.translation_mode.get(), self.qwen_model_path.get().strip()), daemon=True)
        self.worker.start()

    def _process(self, video: Path, model_name: str, burn: bool, translation_mode: str = "Qwen2.5-VL 7B", qwen_model_path: str = "") -> None:
        temp_output = None
        context_model = None
        try:
            from argostranslate import package, translate

            self.events.put(("progress", 1, "Подключаю встроенный переводчик…"))
            model_file = Path(__file__).resolve().parent / "models" / "translate-en_ru-1_9.argosmodel"
            installed = translate.get_installed_languages()
            en = next((lang for lang in installed if lang.code == "en"), None)
            ru = next((lang for lang in installed if lang.code == "ru"), None)
            translator = None
            if en and ru:
                try:
                    translator = en.get_translation(ru)
                except Exception:
                    translator = None
            if translator is None:
                if not model_file.is_file():
                    raise RuntimeError("Не найдена встроенная модель перевода. Распакуйте программу полностью вместе с папкой models.")
                package.install_from_path(str(model_file))
                installed = translate.get_installed_languages()
                en = next((lang for lang in installed if lang.code == "en"), None)
                ru = next((lang for lang in installed if lang.code == "ru"), None)
                if en and ru:
                    translator = en.get_translation(ru)
            if translator is None:
                raise RuntimeError("Не удалось инициализировать встроенную модель английского → русского перевода.")

            duration = self._probe_duration(video)
            source = self._transcribe_with_audiobook_asr(video, model_name)
            if not source:
                raise RuntimeError("Речь не найдена. Проверьте, что в видео есть английская речь.")
            source = self._group_words_into_cues(source)
            if not source:
                raise RuntimeError("ASR не вернул слов с таймкодами. Субтитры не созданы.")
            LOGGER.info("Субтитры сгруппированы по словам: %d реплик; длинная реплика не более %.2f с", len(source), max(end - start for start, end, _ in source))

            total = len(source)
            context_translations: list[str] = [""] * total
            context_failed: set[int] = set()
            if translation_mode == "Qwen2.5-VL 7B":
                try:
                    from work.context_translation import LocalContextTranslator
                    self.events.put(("progress", 25, "Запускаю Qwen напрямую на компьютере…"))
                    context_model = LocalContextTranslator(str(video), LOGGER, qwen_model_path or None)
                except Exception as exc:
                    LOGGER.exception("Не удалось запустить автономный Qwen/llama.cpp")
                    self.events.put(("progress", 25, f"Qwen недоступна ({exc}); использую встроенный перевод…"))
                if context_model and context_model.available:
                    self.events.put(("progress", 25, "Учитываю диалог и кадры видео локальной моделью…"))
                    context_translations, context_failed = context_model.translate(
                        source,
                        lambda done, count: self.events.put(("progress", int(25 + 43 * done / count), f"Контекстный перевод: {done} из {count}")),
                    )
                else:
                    self.events.put(("progress", 25, "Использую встроенный перевод…"))

            translated = []
            failed = 0
            for index, (start, end, original) in enumerate(source):
                if context_model and context_model.available and index not in context_failed and context_translations[index]:
                    translated_text = context_translations[index]
                else:
                    try:
                        translated_text = translator.translate(original).strip()
                        if not translated_text:
                            raise RuntimeError("Пустой ответ переводчика")
                    except Exception:
                        LOGGER.exception("Ошибка перевода субтитра %d (%.3f–%.3f сек.)", index + 1, start, end)
                        translated_text = original
                        failed += 1
                translated.append((start, end, translated_text))
                if (index + 1) % 12 == 0 or index + 1 == total:
                    self.events.put(("progress", int(68 + 10 * (index + 1) / total), f"Подготовка субтитров: {index + 1} из {total}"))
            if context_model and context_model.available:
                LOGGER.info("Контекстный перевод: %d/%d реплик; резервный перевод Argos использован для %d", total - len(context_failed), total, len(context_failed))

            srt_dir = video.parent / "subtitles" if burn else video.parent
            if burn:
                srt_dir.mkdir(parents=True, exist_ok=True)
            srt = srt_dir / (video.stem + ".ru.srt")
            with srt.open("w", encoding="utf-8-sig", newline="\n") as f:
                for i, (start, end, text) in enumerate(translated, 1):
                    f.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")
            self.events.put(("progress", 80, "SRT сохранён. Готовлю выходной MP4…" if burn else "SRT сохранён."))

            output = video if burn else None
            backup = None
            if burn:
                temp_output = video.with_name(f"{video.stem}.burnin.{uuid.uuid4().hex}.mp4")
                filter_path = ffmpeg_subtitle_path(srt)
                command = ["ffmpeg", "-y", "-i", str(video), "-map", "0:v:0", "-map", "0:a?", "-sn", "-vf", f"subtitles='{filter_path}'", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(temp_output)]
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
                if code or not temp_output.is_file() or temp_output.stat().st_size == 0:
                    LOGGER.error("FFmpeg завершился с кодом %s. Последний вывод:\n%s", code, "\n".join(ffmpeg_tail))
                    raise RuntimeError("FFmpeg не создал корректный MP4. Исходник сохранён без изменений; см. logs\subtitle_translator.log.")
                if not self._probe_duration(temp_output):
                    raise RuntimeError("Проверка временного MP4 не прошла. Исходник сохранён без изменений.")

                backup = video.with_name(f"{video.stem}.original{video.suffix}")
                if backup.exists():
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup = video.with_name(f"{video.stem}.original.{stamp}.{uuid.uuid4().hex[:6]}{video.suffix}")
                video.replace(backup)
                try:
                    temp_output.replace(video)
                except Exception:
                    if backup.exists() and not video.exists():
                        backup.replace(video)
                    raise
                LOGGER.info("Исходный MP4 заменён версией с субтитрами; оригинал сохранён в %s", backup)

            note = f"\nНе удалось перевести фрагментов: {failed}; в этих строках оставлен английский текст." if failed else ""
            video_result = f"Исходный MP4 обновлён: {output}\nРезервная копия оригинала: {backup}" if burn else "Вшивание в видео отключено."
            self.events.put(("done", f"Готово!\nSRT: {srt}\n{video_result}" + note))
        except Exception as exc:
            if burn and temp_output is not None and temp_output.exists():
                try:
                    temp_output.unlink()
                except OSError:
                    LOGGER.exception("Не удалось удалить временный файл %s", temp_output)
            LOGGER.exception("Ошибка обработки видео %s", video)
            self.events.put(("error", f"{exc}\n\nПодробный лог: {LOG_FILE}"))
        finally:
            if context_model:
                try:
                    context_model.close()
                except Exception:
                    LOGGER.exception("Не удалось корректно остановить локальный Qwen")

    def _transcribe_with_audiobook_asr(self, video: Path, model_name: str) -> list[tuple[float, float, str]]:
        asr_python = Path(r"J:\audiobook\.venv_cosyvoice3\Scripts\python.exe")
        asr_worker = Path(__file__).resolve().parent / "work" / "asr_video_worker.py"
        if not asr_python.is_file() or not asr_worker.is_file():
            raise RuntimeError("Не найден локальный ASR из J:\\audiobook. Проверьте папку проекта и его окружение.")

        env = os.environ.copy()
        env["SUBTITLE_ASR_MODEL"] = model_name
        self.events.put(("progress", 2, f"Подключаю ASR из J:\\audiobook ({model_name})…"))
        proc = subprocess.Popen(
            [str(asr_python), str(asr_worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            ready = proc.stdout.readline().strip()
            if ready != "@@ready":
                tail = [ready] if ready else []
                tail.extend(line.rstrip() for _, line in zip(range(12), proc.stdout))
                proc.wait(timeout=10)
                raise RuntimeError("Локальный ASR не запустился: " + " | ".join(tail[-12:]))
            proc.stdin.write(json.dumps({"video": str(video)}) + "\n")
            proc.stdin.flush()
            self.events.put(("progress", 5, f"Распознаю английскую речь локальным ASR ({model_name})…"))
            response = None
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith("ASR_UNALIGNED_SKIP "):
                    LOGGER.warning("Пропускаю фразу без точных таймкодов: %s", line[len("ASR_UNALIGNED_SKIP "):])
                    continue
                if line.startswith("@@"):
                    response = json.loads(line[2:])
                    break
                match = re.search(r"(\d{1,3})%", line)
                if match:
                    value = max(5, min(25, 5 + int(int(match.group(1)) * 0.2)))
                    LOGGER.info("Прогресс ASR: %s%%", match.group(1))
                    self.events.put(("progress", value, f"Распознаю английскую речь локальным ASR… {match.group(1)}%"))
            proc.stdin.close()
            code = proc.wait()
            if code:
                raise RuntimeError(f"Локальный ASR завершился с кодом {code}.")
            if not response:
                raise RuntimeError("Локальный ASR завершил работу без результата.")
            if response.get("error"):
                raise RuntimeError("Ошибка локального ASR: " + response["error"])
            LOGGER.info("ASR завершил распознавание: модель %s, устройство %s, слов %d", response.get("model"), response.get("device"), len(response.get("words", [])))
            return [(float(item["start"]), float(item["end"]), str(item["text"])) for item in response.get("words", [])]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    @staticmethod
    def _group_words_into_cues(words: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
        """Create short, non-overlapping captions using Whisper's word-level timestamps."""
        cleaned = []
        for start, end, raw_token in sorted(words, key=lambda row: (row[0], row[1])):
            token = raw_token.strip()
            if not token or end <= start:
                continue
            normalized = re.sub(r"[^a-z0-9]", "", token.lower())
            if cleaned:
                prev_start, _, prev_token = cleaned[-1]
                prev_normalized = re.sub(r"[^a-z0-9]", "", prev_token.strip().lower())
                if normalized and normalized == prev_normalized and abs(start - prev_start) < 0.25:
                    continue
            cleaned.append((start, end, raw_token))

        cues = []
        group = []
        last_end = 0.0

        def text_of(items):
            return "".join(item[2] for item in items).strip()

        def flush():
            nonlocal group, last_end
            if not group:
                return
            start = max(group[0][0], last_end)
            end = max(group[-1][1], start + 0.12)
            text = text_of(group)
            if text and end - start <= 5.2:
                cues.append((start, end, text))
                last_end = end
            elif text:
                LOGGER.warning("Пропускаю субтитр без пригодного интервала (%.2f–%.2f с): %s", start, end, text[:80])
            group = []

        for word in cleaned:
            candidate = group + [word]
            candidate_text = text_of(candidate)
            elapsed = candidate[-1][1] - candidate[0][0]
            gap = word[0] - group[-1][1] if group else 0.0
            if group and (gap > 0.85 or len(candidate_text) > 68 or elapsed > 5.2 or len(candidate) > 14):
                flush()
                candidate = [word]
                candidate_text = text_of(candidate)
                elapsed = word[1] - word[0]
            group = candidate
            if re.search(r"[.!?…][\"')\]]?$", word[2].strip()) and (elapsed >= 1.4 or len(candidate_text) >= 28):
                flush()
        flush()
        return cues

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
