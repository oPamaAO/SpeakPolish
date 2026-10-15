"""Голосовой ввод для opencode — локальный STT + кнопка «полировка».

Расположение: ~/.config/opencode/voice/voice.py (глобально, для всех чатов).
Запуск:  python voice.py   (слушает хоткеи, висит в фоне)

Как пользоваться (коротко, полный гайд — в README.md рядом):
  F9              — старт/стоп записи. Каждый готовый кусок ДОПИСЫВАЕТСЯ в draft.txt
                    и вставляется в активное окно. Можно: стоп -> подумать ->
                    подправить руками -> снова F9 -> дописать дальше.
  Shift+F9        — то же самое (алиас «продолжить»).
  Ctrl+F9         — очистить черновик, начать заново.
  Ctrl+Shift+Space— отполировать весь черновик через
                    `opencode run --agent prompt-polisher` и вставить результат. F10 — полировка текста из БУФЕРА ОБМЕНА (выделил, Ctrl+C, F10).

Пайплайн куска: микрофон (16k mono) -> faster-whisper large-v3-turbo локально
  -> замена по terminology.yaml -> append в draft.txt -> буфер обмена + Ctrl+V.
"""

import os
import re
import subprocess
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
DRAFT_PATH = os.path.join(BASE_DIR, "draft.txt")
POLISHED_PATH = os.path.join(BASE_DIR, "draft_polished.txt")

# --- Конфиг -----------------------------------------------------------------
DEFAULTS = {
    "model": "large-v3-turbo",
    "device": "cuda",
    "compute_type": "int8",
    "initial_prompt": "Это голосовая диктовка программиста. Русский язык с английскими IT-терминами. Расставь знаки препинания.",
    "samplerate": 16000,
    "separator": " ",
    "hallucination_silence_threshold": 2.0,
    "hallucinated_phrases": [
        "спокойная музыка",
        "спасибо за просмотр",
        "подписывайтесь на канал",
        "продолжение следует",
        "спасибо за внимание",
    ],
    "agent": "prompt-polisher",
    "agent_model": "opencode/muse-spark-1.3-contributor-free",
    "polish_mode": "replace",  # replace = выделить всё в поле ввода и заменить; append = вставить после
    "terminology_file": "terminology.yaml",
}

_config_cache = {}


def load_yaml(path):
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def get_cfg():
    if _config_cache:
        return _config_cache
    raw = load_yaml(CONFIG_PATH)
    stt = raw.get("stt", {})
    audio = raw.get("audio", {})
    draft = raw.get("draft", {})
    polish = raw.get("polish", {})
    paths = raw.get("paths", {})
    _config_cache.update({
        "model": stt.get("model", DEFAULTS["model"]),
        "device": stt.get("device", DEFAULTS["device"]),
        "compute_type": stt.get("compute_type", DEFAULTS["compute_type"]),
        "initial_prompt": stt.get("initial_prompt", DEFAULTS["initial_prompt"]),
        "samplerate": audio.get("samplerate", DEFAULTS["samplerate"]),
        "audio_device": audio.get("device", None),  # подстрока имени микрофона; null = системный
        "separator": draft.get("separator", DEFAULTS["separator"]),
        "hallucination_silence_threshold": stt.get(
            "hallucination_silence_threshold", DEFAULTS["hallucination_silence_threshold"]),
        "hallucinated_phrases": [str(p).lower() for p in
                                 raw.get("filter", {}).get("hallucinated_phrases",
                                                           DEFAULTS["hallucinated_phrases"])],
        "agent": polish.get("agent", DEFAULTS["agent"]),
        "agent_model": polish.get("model", DEFAULTS["agent_model"]),
        "polish_mode": polish.get("mode", DEFAULTS.get("polish_mode", "replace")),
        "delete_before_paste": polish.get("delete_before_paste", True),
        "terminology_file": paths.get("terminology", DEFAULTS["terminology_file"]),
    })
    return _config_cache


# --- Терминология (пост-замены, только однозначное) ---------------------------
_terminology_cache = None


def load_terminology():
    global _terminology_cache
    if _terminology_cache is not None:
        return _terminology_cache
    cfg = get_cfg()
    path = os.path.join(BASE_DIR, cfg["terminology_file"])
    table = load_yaml(path)
    # yaml может вернуть не-словарь — страхуемся
    if not isinstance(table, dict):
        table = {}
    # Сортируем по убыванию длины ключа, чтобы «гит хаб» чинилось раньше «хаб»
    _terminology_cache = sorted(
        ((str(k).lower(), str(v)) for k, v in table.items() if k and v),
        key=lambda kv: -len(kv[0]),
    )
    return _terminology_cache


def apply_terminology(text):
    table = load_terminology()
    # Замена ТОЛЬКО целых слов/фраз: «апи» внутри «запись» не трогаем.
    # (?<!\w) / (?!\w) — границы слова, регистронезависимо.
    for spoken, proper in table:
        pattern = re.compile(r"(?<!\w)" + re.escape(spoken) + r"(?!\w)", re.IGNORECASE)
        text = pattern.sub(proper, text)
    # Склейка пробелов
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


# --- Тишина и галлюцинации -------------------------------------------------------------
def trim_silence(audio, samplerate, frame_ms=100, threshold=0.008, pad_ms=300):
    """Отрезает ведущую/хвостовую тишину по энергии. Возвращает None, если речи нет.
    Именно хвост тишины после стопа Whisper любит дорисовывать
    («спокойная музыка», зацикленные «у меня зашу...»).
    Порог низкий специально, чтобы не съесть тихое начало фразы."""
    import numpy as np
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32).flatten()
    frame = max(1, int(samplerate * frame_ms / 1000))
    n_frames = len(audio) // frame
    if n_frames == 0:
        return None
    rms = np.sqrt(np.mean(audio[:n_frames * frame].reshape(n_frames, frame) ** 2, axis=1))
    loud = np.nonzero(rms >= threshold)[0]
    if len(loud) == 0:
        return None
    pad = int(samplerate * pad_ms / 1000)
    start = max(0, loud[0] * frame - pad)
    end = min(len(audio), (loud[-1] + 1) * frame + pad)
    return audio[start:end]


def strip_repetitions(text, min_words=2, min_repeats=3):
    """Режет зацикленные повторы вида «У меня зашу. У меня зашу. У меня зашу.» —
    типичный артефакт Whisper на шуме/конце записи. Оставляет первое вхождение.
    Сравнение по словам без пунктуации; одиночные повторы (min_repeats=3) не трогаем."""
    strip_chars = ".,!?…:;\"'«»()—–-"
    changed = True
    while changed:
        changed = False
        words = text.split()
        norm = [w.strip(strip_chars).lower() for w in words]
        n = len(words)
        for size in range(8, min_words - 1, -1):
            i = 0
            while i + size * min_repeats <= n:
                block = norm[i:i + size]
                if any(not w for w in block):
                    i += 1
                    continue
                reps = 1
                while (i + size * (reps + 1) <= n
                       and norm[i + size * reps:i + size * (reps + 1)] == block):
                    reps += 1
                if reps >= min_repeats:
                    del words[i + size:i + size * reps]
                    del norm[i + size:i + size * reps]
                    n = len(words)
                    changed = True
                    continue  # проверяем то же место заново
                i += 1
            if changed:
                text = " ".join(words)  # зафиксировать вырезанное перед перепроверкой
                break
    return " ".join(words)


def strip_hallucinations(text):
    """Вычищает типовые выдумки Whisper («спокойная музыка» и т.п.).
    Удаляется ТОЛЬКО сама фраза, окружающий текст не трогаем:
    старая версия вырезала «целое предложение», а в длинной диктовке без точек
    это сносило почти весь текст."""
    cfg = get_cfg()
    for phrase in cfg["hallucinated_phrases"]:
        if phrase:
            text = re.compile(re.escape(phrase), re.IGNORECASE).sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\s+([.,!?…;:])", r"\1", text).strip()


# --- Уровень записи и авто-усиление ------------------------------------------------------
def audio_stats(audio, samplerate, threshold=0.008, frame_ms=100):
    """Пик + доля громких фреймов. Если почти всё тихо — проблема в микрофоне,
    а не в распознавании (см. разбор: 83с записи с 2% речи)."""
    import numpy as np
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    frame = max(1, int(samplerate * frame_ms / 1000))
    nf = len(audio) // frame
    if nf == 0:
        return peak, 0.0
    rms = np.sqrt(np.mean(audio[:nf * frame].reshape(nf, frame) ** 2, axis=1))
    return peak, float(np.mean(rms >= threshold))


def auto_gain(audio, target=0.5, min_peak=1e-6):
    """Тихую запись подтягиваем к target-пику — Whisper тишину почти не слышит."""
    import numpy as np
    peak = float(np.max(np.abs(audio)))
    if peak < 0.1 and peak > min_peak:
        return (audio / peak * target).astype(np.float32), round(target / peak, 1)
    return audio, 1.0


def resolve_input_device(want=None):
    """Индекс входного устройства по подстроке имени (config audio.device).
    None/пусто — системный микрофон по умолчанию."""
    if not want:
        return None
    try:
        import sounddevice as sd
        want = str(want).lower()
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and want in str(d.get("name", "")).lower():
                return i
        print(f"[voice] Микрофон «{want}» не найден, беру системный по умолчанию.")
    except Exception as e:
        print(f"[voice] Не смог опросить микрофоны: {e}")
    return None


def describe_input_device(idx=None):
    try:
        import sounddevice as sd
        if idx is None:
            idx = sd.default.device[0]
        d = sd.query_devices(idx)
        return f"#{idx} {d.get('name')} ({d.get('hostapi')})"
    except Exception:
        return "?"


# --- Архив записей (разбор потерь: аудио vs текст) ------------------------------------
def save_recording(audio, samplerate, keep=20):
    """Сохраняет сырой кусок в recordings/chunk_*.wav (16-bit PCM, stdlib wave).
    Позволяет доказать, где потерялся текст: в микрофоне или в STT.
    Хранит последние `keep` файлов."""
    import wave
    import numpy as np
    from datetime import datetime
    recdir = os.path.join(BASE_DIR, "recordings")
    os.makedirs(recdir, exist_ok=True)
    name = datetime.now().strftime("chunk_%Y%m%d_%H%M%S_%f")[:-3] + ".wav"
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    try:
        with wave.open(os.path.join(recdir, name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(samplerate)
            w.writeframes(pcm.tobytes())
    except Exception as e:
        print(f"[voice] Не смог сохранить запись: {e}")
        return
    try:
        files = sorted(f for f in os.listdir(recdir) if f.endswith(".wav"))
        for old in files[:-keep]:
            os.remove(os.path.join(recdir, old))
    except Exception:
        pass


# --- Черновик (append-буфер) ---------------------------------------------------
_draft_lock = threading.Lock()


def read_draft():
    if not os.path.exists(DRAFT_PATH):
        return ""
    with open(DRAFT_PATH, encoding="utf-8") as f:
        return f.read().strip()


def append_draft(piece):
    cfg = get_cfg()
    piece = piece.strip()
    if not piece:
        return ""
    with _draft_lock:
        current = read_draft()
        new = (current + cfg["separator"] + piece).strip() if current else piece
        with open(DRAFT_PATH, "w", encoding="utf-8") as f:
            f.write(new)
    return new


def clear_draft():
    with _draft_lock:
        with open(DRAFT_PATH, "w", encoding="utf-8") as f:
            f.write("")
    print("[voice] Черновик очищен.")


# --- Вставка в активное окно ----------------------------------------------------
def archive_and_clear_draft(keep=20):
    """Черновик — одноразовый: после успешной полировки уезжает в history/,
    draft.txt очищается. Иначе старые задачи вечно подмешиваются в новые
    полировки («агент тянет предыдущее»). Храним последние `keep`."""
    from datetime import datetime
    hist = os.path.join(BASE_DIR, "history")
    os.makedirs(hist, exist_ok=True)
    text = read_draft()
    if text.strip():
        name = datetime.now().strftime("draft_%Y%m%d_%H%M%S_%f")[:-3] + ".txt"
        try:
            with open(os.path.join(hist, name), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"[voice] Не смог сохранить историю: {e}")
            return False
    try:
        with open(DRAFT_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception as e:
        print(f"[voice] Не смог очистить черновик: {e}")
        return False
    try:
        files = sorted(f for f in os.listdir(hist) if f.endswith(".txt"))
        for old in files[:-keep]:
            os.remove(os.path.join(hist, old))
    except Exception:
        pass
    return True


def normalize_polish_layout(text):
    """Чинит склейки вида '## ЦельУстранить...': JSON-события режут текст
    в произвольных местах, а склейка без разделителя склеивает заголовок
    с текстом. Известные разделы допиливаются принудительно."""
    import re
    sections = ["Цель", "Контекст", "Задачи", "Ожидаемый результат",
                "Ограничения", "?? Уточнить"]
    text = text.replace("\u200b", "").replace("\u2060", "").replace("\ufeff", "")
    # "**Цель** ..." вместо заголовка — приводим к "## Цель"
    for s in sections:
        text = re.sub(r"^\*{1,2}\s*" + re.escape(s) + r"\s*\*{1,2}[ \t]*:?[ \t]*",
                      "## " + s + "\n", text, flags=re.MULTILINE)
    # "### Цель" -> "## Цель" СНАЧАЛА, иначе правило ниже съест два # из трёх
    for s in sections:
        text = re.sub(r"^#{3,} " + re.escape(s), "## " + s, text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]*##[ \t]*", "\n## ", text)  # каждый заголовок с новой строки
    for s in sections:
        esc = re.escape(s)
        text = re.sub(r"## " + esc + r":(\S)", "## " + s + ":\n\\1", text)  # '## Цель:Текст'
        text = re.sub(r"## " + esc + r"(?!:)(?=\S)", "## " + s + "\n", text)  # '## ЦельТекст' (но не '## Цель: ...')
        # тело раздела на той же строке ("## Цель Создать...", "## Цель: Создать...")
        # — переносим вниз; двоеточие остаётся на строке заголовка
        text = re.sub(r"^(## " + esc + r":?)[ \t]+(\S.*)$", r"\1\n\2",
                      text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _select_all_input(quick=False):
    """Выделить весь ввод: Ctrl+A. Проверено вживую в приложении opencode —
    там это работает как select-all. Больше никаких End/Home/Up-связок."""
    import time
    import keyboard
    keyboard.press_and_release("ctrl+a")
    time.sleep(0.05 if quick else 0.15)
    keyboard.press_and_release("ctrl+a")
    if not quick:
        time.sleep(0.15)


def paste_to_active_window(text, replace=False, delete_first=False):
    """Вставка через буфер обмена.
    replace=False — вставить в позицию курсора (для кусков диктовки).
    replace=True  — выделить всё поле ввода и заменить.
    delete_first=True — после выделения нажать Backspace перед вставкой
      (твой «простой» вариант; по умолчанию выкл: при неполном выделении
      Backspace СТИРАЕТ выделенное, а вставка-поверх — только дублирует)."""
    """Вставка через буфер обмена.
    replace=False — вставить в позицию курсора (для кусков диктовки).
    replace=True  — выделить всё поле ввода (End, Shift+Home до границ буфера) и заменить
                    (для полировки). Работает, когда фокус в поле ввода opencode;
                    иначе — обычная вставка."""
    import time
    import pyperclip
    pyperclip.copy(text)
    try:
        import keyboard
        if replace:
            # Два полных прохода: одиночные нажатия иногда теряются, когда TUI занят
            # отрисовкой или идёт генерация ответа, — второй добирает.
            # Плюс финальный быстрый проход ВПРИТЫК к вставке: между выделением
            # и Ctrl+V ничего не должно успеть снять выделение (ре-рендер TUI).
            _select_all_input()
            _select_all_input()
            _select_all_input(quick=True)
            import time
            time.sleep(0.05)
        if replace and delete_first:
            keyboard.press_and_release("backspace")
            time.sleep(0.1)
        keyboard.press_and_release("ctrl+v")
    except Exception as e:
        print(f"[voice] Скопировано в буфер (автовставка не сработала: {e}). Нажми Ctrl+V сам.")


# --- STT (локально) --------------------------------------------------------------
_model_cache = {}


def _ensure_cuda_dlls():
    """Подсовываем Windows DLL из pip-пакетов nvidia-* (cublas и др.),
    иначе CTranslate2 падает с 'cublas64_12.dll is not found'."""
    try:
        import site
        roots = []
        try:
            roots += site.getsitepackages()
        except Exception:
            pass
        try:
            roots.append(site.getusersitepackages())
        except Exception:
            pass
        for root in roots:
            nvidia = os.path.join(root, "nvidia")
            if not os.path.isdir(nvidia):
                continue
            for pkg in os.listdir(nvidia):
                bindir = os.path.join(nvidia, pkg, "bin")
                if os.path.isdir(bindir):
                    try:
                        os.add_dll_directory(bindir)
                    except Exception:
                        pass
                    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


_ensure_cuda_dlls()


def get_model():
    if "model" in _model_cache:
        return _model_cache["model"]
    from faster_whisper import WhisperModel
    cfg = get_cfg()
    compute = cfg["compute_type"]
    # На GTX 1660S нет тензорных ядер — int8 на CUDA медленный/проблемный, берём float16
    if cfg["device"] == "cuda" and compute == "int8":
        print("[voice] int8 на CUDA без тензорных ядер не годится, беру float16.")
        compute = "float16"
    print(f"[voice] Загружаю Whisper {cfg['model']} ({cfg['device']}/{compute})...")
    try:
        model = WhisperModel(cfg["model"], device=cfg["device"], compute_type=compute)
    except Exception as e:
        print(f"[voice] CUDA не взлетела ({e}), падаю на CPU/int8.")
        model = WhisperModel(cfg["model"], device="cpu", compute_type="int8")
    _model_cache["model"] = model
    print("[voice] Модель готова.")
    return model


def transcribe(audio, samplerate):
    import numpy as np
    cfg = get_cfg()
    try:
        return _transcribe_with(get_model(), audio, cfg)
    except Exception as e:
        msg = str(e).lower()
        # Нет CUDA-библиотек (типично для Windows) — пересобираем модель на CPU и повторяем
        if any(k in msg for k in ("cublas", "cudnn", "cuda", "cudart", "cusparse")):
            print("[voice] CUDA-библиотек нет, переключаюсь на CPU...")
            from faster_whisper import WhisperModel
            _model_cache["model"] = WhisperModel(cfg["model"], device="cpu", compute_type="int8")
            return _transcribe_with(_model_cache["model"], audio, cfg)
        raise


def _transcribe_with(model, audio, cfg):
    import numpy as np
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32).flatten()
    segments, _info = model.transcribe(
        audio,
        language=None,  # автоопределение RU/EN
        beam_size=5,
        vad_filter=True,
        hallucination_silence_threshold=cfg["hallucination_silence_threshold"],
        initial_prompt=cfg["initial_prompt"],
    )
    text = "".join(s.text for s in segments).strip()
    text = strip_repetitions(text)
    text = strip_hallucinations(text)
    return apply_terminology(text)


# --- Запись (тоггл) ---------------------------------------------------------------
_recording = {"active": False, "frames": [], "stream": None}
_rec_lock = threading.Lock()


def _audio_callback(indata, frames, time_info, status):
    with _rec_lock:
        if _recording["active"]:
            _recording["frames"].append(indata.copy())


def toggle_record():
    import sounddevice as sd
    cfg = get_cfg()
    with _rec_lock:
        active = _recording["active"]
    if not active:
        dev = resolve_input_device(cfg.get("audio_device"))
        _recording["frames"] = []
        stream = sd.InputStream(
            samplerate=cfg["samplerate"], channels=1, dtype="float32",
            device=dev, callback=_audio_callback,
        )
        stream.start()
        with _rec_lock:
            _recording["active"] = True
            _recording["stream"] = stream
        print("[voice] ● Запись... (нажми F9 ещё раз для стопа)")
    else:
        with _rec_lock:
            _recording["active"] = False
            stream = _recording["stream"]
            frames = _recording["frames"]
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        print("[voice] ■ Стоп, распознаю...")
        threading.Thread(target=_finish_segment, args=(frames, cfg["samplerate"]), daemon=True).start()


def _finish_segment(frames, samplerate):
    import numpy as np
    if not frames:
        print("[voice] Пусто, ничего не записалось.")
        return
    audio = np.concatenate(frames, axis=0)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32).flatten()
    orig_s = len(audio) / samplerate
    save_recording(audio, samplerate)  # архив для разбора потерь (recordings/)
    # Режем хвост тишины ДО распознавания — иначе Whisper дорисует «музыку»
    trimmed = trim_silence(audio, samplerate)
    if trimmed is None:
        print(f"[voice] Одна тишина ({orig_s:.1f}с), пропускаю (ничего не дописано).")
        return
    print(f"[voice] Аудио {orig_s:.1f}с -> после обрезки тишины {len(trimmed)/samplerate:.1f}с.")
    peak, frac = audio_stats(trimmed, samplerate)
    print(f"[voice] Уровень: пик {peak:.3f}, речи {frac:.0%}.")
    if peak < 0.02 or frac < 0.05:
        print("[voice] ⚠ ОЧЕНЬ ТИХО! Проверь микрофон (громкость/выбранное устройство). "
              "Текст может потеряться не из-за распознавания, а из-за тишины на входе.")
    trimmed, gain = auto_gain(trimmed)
    if gain != 1.0:
        print(f"[voice] Применил авто-усиление x{gain}.")
    try:
        piece = transcribe(trimmed, samplerate)
    except Exception as e:
        print(f"[voice] Ошибка STT: {e}")
        return
    if not piece:
        print("[voice] Речь не распознана, попробуй ещё раз.")
        return
    full = append_draft(piece)
    print(f"[voice] Кусок: {piece}")
    print(f"[voice] Черновик: {len(full)} символов всего (draft.txt открыт — видно вживую).")
    # В чат вставляем ТОЛЬКО новый кусок. Весь черновик целиком — только в draft.txt,
    # иначе каждый стоп дублировал бы всё ранее надиктованное.
    paste_to_active_window(piece)


# --- Поиск opencode (терминал voice.py часто не видит npm-global в PATH) ---------------
_opencode_cache = {}


def find_opencode():
    if "exe" in _opencode_cache:
        return _opencode_cache["exe"]
    import shutil
    found = shutil.which("opencode")
    # .cmd/.ps1-шим через subprocess без shell не запускается — ищем настоящий exe
    if found and found.lower().endswith(".exe") and os.path.isfile(found):
        _opencode_cache["exe"] = found
        return found
    candidates = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates += [
            os.path.join(appdata, "npm", "node_modules", "opencode-ai", "bin", "opencode.exe"),
            os.path.join(appdata, "npm", "opencode.exe"),
        ]
    localapp = os.environ.get("LOCALAPPDATA", "")
    if localapp:
        candidates.append(os.path.join(localapp, "Programs", "opencode", "opencode.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            _opencode_cache["exe"] = c
            return c
    _opencode_cache["exe"] = None
    return None


# --- Полировка через opencode run --------------------------------------------------
def parse_polish_json(output):
    """Вытаскивает чистый текст из `opencode run --format json`.
    Пропускает служебные реплики (phase=commentary: «сейчас соберу...»),
    берёт только финальный ответ. В default-формате всё это склеивалось
    в одну кашу и попадало в чат как «преамбула»."""
    import json
    texts = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not (isinstance(ev, dict) and ev.get("type") == "text"):
            continue
        part = ev.get("part", {})
        if not isinstance(part, dict):
            continue
        try:
            if part.get("metadata", {}).get("openai", {}).get("phase") == "commentary":
                continue
        except Exception:
            pass
        t = part.get("text", "")
        if t:
            texts.append(t)
    return "".join(texts).strip()


def strip_console_chrome(text):
    """Запасной вариант: вычистить служебные строки из default-формата."""
    import re
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^[•✓✗❯→‼⚠!]\s", s):
            continue
        if re.match(r"^>\s+\S+\s+·\s+", s):  # '> build · model'
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# Полные правила полировки ИНЛАЙН: голосовой путь не зависит от agent-файла
# (раньше build делегировал субагенту и пересказывал ответ своими словами —
# отсюда преамбулы и вольная структура). Без --agent, прямой ответ build.
POLISH_RULES = (
    "Ты — редактор технических заданий для ИИ-агента. "
    "Возьми сырой текст и верни ТОЛЬКО Markdown-ТЗ, без преамбул и комментариев. "
    "Служебные обёртки («передаю в работу», «отполируй», «улучши») отбрось — только сама задача. "
    "НЕ ВЫДУМЫВАЙ: файлы, пути, MCP, команды упоминай только если есть в исходнике; новых фактов не добавляй. "
    "Структуру исходника НЕ копируй: всегда приводи к 6 разделам, свои заголовки запрещены. "
    "КОНТЕКСТ НЕПРИКОСНОВЕНЕН: описательный текст автора (лор, внешность, расположения, характеристики) "
    "сохраняй ПОЛНОСТЬЮ, чисти только опечатки распознавания. "
    "Сжимай структуру, повторы и воду (одна мысль — один пункт), но красивые описания, точные эпитеты "
    "и детальные характеристики НЕ вырезай — заменяй на более точные полные синонимы. "
    "Цитаты и тесткейсы — дословно, цитатой. Пункты «без действий исполнителя» («проверю сам», «только план») — "
    "не в Задачи, а в Ограничения с пометкой «на исполнении не делать». "
    "СТРУКТУРА СТРОГО ТАКАЯ, ВСЕ РАЗДЕЛЫ ВСЕГДА. Заголовок — отдельная строка, текст ВСЕГДА с новой строки (запрещены склейки вида ЦельТекст). ## Цель (1-3 предложения). ## Контекст (описания полностью + что есть). "
    "## Задачи (нумерованный список 1. 2. 3., не буллеты, каждый пункт с глагола, только делать). "
    "## Ожидаемый результат (обязателен; нет в исходнике — «—» + вопрос в ?? Уточнить). "
    "## Ограничения (что НЕ делать; нет — «—»). "
    "## ?? Уточнить (только задачи исполнителю вида «Исполнителю: ...», НЕ вопросы пользователю; всё ясно — «—»). "
    "Пути, имена, клавиши — в обратных кавычках, главное — жирным."
)


def polish_draft():
    """Полировка черновика диктовки (Ctrl+Shift+Space)."""
    text = read_draft()
    if not text:
        print("[voice] Черновик пуст, полировать нечего.")
        return
    polish_text(text, source="черновик")


def polish_clipboard():
    """Полировка вручную написанного текста из буфера обмена.
    Сценарий без голосового ввода: выделил текст в чате -> Ctrl+C -> хоткей.
    Отполированное заменяет текст в поле ввода (тот же replace-режим)."""
    import pyperclip
    try:
        text = (pyperclip.paste() or "").strip()
    except Exception as e:
        print(f"[voice] Не смог прочитать буфер обмена: {e}")
        return
    if not text:
        print("[voice] Буфер обмена пуст — выдели текст и скопируй (Ctrl+C), потом жми хоткей.")
        return
    if len(text) > 20000:
        print("[voice] В буфере слишком много текста (>20000 символов), обрежь и повтори.")
        return
    polish_text(text, source="буфер обмена")


def polish_text(text, source="текст"):
    """Общий раннер полировки: opencode run (инлайн-правила, json) -> замена в окне."""
    cfg = get_cfg()
    print(f"[voice] Полирую ({source}) через opencode run...")
    exe = find_opencode()
    if not exe:
        print("[voice] Не нашёл opencode: ни в PATH, ни в %APPDATA%\\npm. "
              "Проверь `where opencode` в терминале, где стоит npm-global.")
        return
    cmd = [
        exe, "run",
        "-m", cfg["agent_model"],
        "--format", "json",  # только чистый текст, без служебных строк TUI
        "--title", "voice-polish",
        POLISH_RULES + "\n\nТекст:\n" + text,
    ]
    try:
        # Байты + ручной utf-8 decode: вывод opencode.exe — UTF-8 с эмодзи,
        # а text=True декодировал бы системным cp1251 и падал с UnicodeDecodeError.
        proc = subprocess.run(cmd, capture_output=True, text=False, timeout=180)
        out_raw = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        out = parse_polish_json(out_raw)
        if not out:
            # json не распарсился — запасной путь с чисткой служебных строк
            out = strip_console_chrome(out_raw)
        out = normalize_polish_layout(out)  # чиним склейки вида '## ЦельТекст'
        # Диагностический след: сырой вывод + итог. Если склейка вылезет снова —
        # по этому файлу будет видно, что выдала модель, а что сделал нормалайз.
        try:
            with open(os.path.join(BASE_DIR, "polish_debug_last.txt"), "w",
                      encoding="utf-8") as f:
                f.write("=== RAW ===\n" + out_raw + "\n=== PARSED ===\n" + out)
        except Exception:
            pass
        if "free tier can only be used" in out + err:
            print("[voice] Free-tier guard: opencode run отклонён. Черновик цел — "
                  "вставь его в TUI и вызови /polish там (внутри OpenCode лимита нет).")
            return
        if proc.returncode != 0 or not out:
            print(f"[voice] opencode run не дал текста (code={proc.returncode}): {err[:500]}")
            return
        with open(POLISHED_PATH, "w", encoding="utf-8") as f:
            f.write(out)
        if source == "черновик":
            # Черновик одноразовый: иначе старые задачи вечно лезут в новые полировки.
            if archive_and_clear_draft():
                print("[voice] Черновик ушёл в history/ и очищен — диктуй новое.")
        replace = cfg.get("polish_mode", "replace") == "replace"
        if replace:
            # Даём 2 секунды вернуть фокус в поле ввода opencode — замена бьёт
            # именно туда, где курсор. Не трогай клавиатуру, пока идёт замена.
            # Если в момент замены шла генерация ответа — клавиши могут уйти мимо:
            # тогда результат уже лежит в буфере, выдели сам (Ctrl+A) и вставь (Ctrl+V).
            print("[voice] Фокус в поле ввода! Замена через 2 сек — не трогай клавиатуру...")
            import time
            time.sleep(2.0)
        paste_to_active_window(out, replace=replace,
                                 delete_first=cfg.get("delete_before_paste", False))
        how = "заменил текст в поле ввода" if replace else "вставил после текста"
        print(f"[voice] Готово, «конфетка» {how} (копия: draft_polished.txt).")
        if replace:
            print("[voice] Если старый текст местами остался — выдели сам (Ctrl+A) и вставь (Ctrl+V): результат уже в буфере.")
    except FileNotFoundError:
        print("[voice] Не найден `opencode` в PATH. Открой новый терминал или добавь npm-global в PATH.")
    except subprocess.TimeoutExpired:
        print("[voice] Таймаут 180с — free-модель висит, повтори.")


# --- main --------------------------------------------------------------------------
BUILD = "2026-10-15p"  # метка сборки: видна при старте, по ней проверяем, что запущен новый код


def _preload_model():
    try:
        get_model()
    except Exception as e:
        print(f"[voice] Не смог загрузить модель STT: {e}")


def main():
    import keyboard
    print(f"[voice] Сборка {BUILD}. Если чинил баги, а поведение не изменилось — "
          "ты запустил старый процесс, убей его и стартуй заново.")
    print("[voice] Слушаю хоткеи: F9 запись/стоп(+дописать) | Shift+F9 продолжить | "
          "Ctrl+F9 очистить | Ctrl+Shift+Space полировать черновик | F10 полировать из буфера.")
    print(f"[voice] Черновик: {DRAFT_PATH}")
    print(f"[voice] Микрофон: {describe_input_device(resolve_input_device(get_cfg().get('audio_device')))} "
          "(сменить: config.yaml -> audio.device = подстрока имени)")
    print("[voice] Подгружаю модель Whisper в фоне (первый раз качает ~1.5GB, жди)...")
    threading.Thread(target=_preload_model, daemon=True).start()
    keyboard.add_hotkey("F9", toggle_record)
    keyboard.add_hotkey("shift+F9", toggle_record)
    keyboard.add_hotkey("ctrl+F9", clear_draft)
    keyboard.add_hotkey("ctrl+shift+space", polish_draft)
    keyboard.add_hotkey("F10", polish_clipboard)
    keyboard.wait()


if __name__ == "__main__":
    sys.exit(main())
