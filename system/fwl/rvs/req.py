"""
req.py — модуль распознавания речи (Req) для SMOS FWL

ИСТОРИЯ (2026-08-19): раньше req.py сам открывал отдельный поток
микрофона и записывал живую речь, приклеивая к ней "довесок" (prebuffer)
от wake.py — сначала "встык", потом с затуханием на стыке. Два
независимо открытых потока на один физический микрофон регулярно давали
проблемы (ошибки ALSA при попытке делить устройство, щелчки/потери на
границе склейки). Теперь wake.py — единственный, кто трогает микрофон:
он сам дожидается конца фразы и сохраняет ЦЕЛЬНУЮ запись в
flags/utterance.wav (см. wake.py за подробностями). req.py этот файл
только читает и отправляет в Google STT — микрофон не открывает вообще.

Логика:
- Скрипт сам находит папку, в которой лежит (не зависит от того,
  откуда его запустили).
- Все настройки читает из config.json рядом с собой (через config.py).
  Если config.json нет или он битый — используются значения по
  умолчанию, скрипт не падает (подробности — в config.py).
- Ждёт появления flags/utterance.wav (его пишет wake.py атомарно —
  сначала во временный файл, потом переименование, так что req.py
  никогда не увидит недописанный WAV).
    - Файла нет -> ничего не делает, ждёт и проверяет снова.
    - Файл появился -> читает его (и сразу удаляет), отправляет в Google
      STT, записывает результат (текст + метаданные: уверенность,
      альтернативные варианты, время, язык) в output/recognized.json.
- При каждом УСПЕШНОМ распознавании обновляет flags/activity.flag
  (просто трогает mtime файла). wake.py следит за этим файлом и
  продлевает "окно продолжения диалога" — разрешает начать запись
  следующей фразы без повторного произнесения активационного слова.
- Оба процесса независимы: пока req.py ждёт ответа от Google (может
  занять секунду и больше), wake.py уже слушает следующую активацию —
  никакой блокировки между ними больше нет (раньше был flags/req.lock —
  теперь не нужен, делить микрофон больше не с кем).

Установка зависимостей:
    pip install SpeechRecognition
(pyaudio req.py больше не нужен — микрофон он не открывает)
"""

import json
import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

# --- Пути (скрипт сам находит себя, не зависит от места запуска) ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти config.py рядом с собой

import config  # noqa: E402  (импорт после правки sys.path — так и задумано)
import log_client  # noqa: E402
import speech_recognition as sr  # noqa: E402

CFG = config.load(SCRIPT_DIR)

# --- Пути к файлам-флагам и выходным данным — берутся из config["paths"] ---
FLAGS_DIR = SCRIPT_DIR / CFG["paths"]["flags_dir"]
UTTERANCE_FILE = FLAGS_DIR / CFG["paths"]["utterance_file"]  # готовая цельная запись фразы, от wake.py
ACTIVITY_FILE = FLAGS_DIR / CFG["paths"]["activity_file"]  # общий с wake.py сигнал "было успешное распознавание"

OUTPUT_DIR = SCRIPT_DIR / CFG["paths"]["output_dir"]
OUTPUT_FILE = OUTPUT_DIR / CFG["paths"]["output_file"]

DEBUG_AUDIO_DIR = SCRIPT_DIR / CFG["paths"]["debug_audio_dir"]
DEBUG_AUDIO_FILE_NAME = CFG["paths"]["debug_audio_file"]

# --- Настройки (все — из config.json, см. config.py -> DEFAULTS за описанием) ---
LANGUAGE = CFG["language"]
CHECK_INTERVAL = CFG["check_interval_sec"]

# ВАЖНО: sample_rate/bit_depth должны совпадать с audio.* в config.json,
# который читает и wake.py — иначе WAV от wake.py будет читаться неверно.
SAMPLE_RATE = CFG["audio"]["sample_rate"]
SAMPLE_WIDTH = CFG["audio"]["bit_depth"] // 8  # байт на сэмпл


def load_utterance() -> bytes:
    """Читает и сразу удаляет flags/utterance.wav — готовую, уже
    полностью записанную wake.py фразу (довесок + сама речь + немного
    тишины после неё). Возвращает сырые PCM-байты (формат — как в
    config["audio"]) или b'', если файла ещё нет — это нормальное,
    основное состояние между фразами."""
    if not UTTERANCE_FILE.exists():
        return b""
    try:
        with wave.open(str(UTTERANCE_FILE), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, OSError) as e:
        print(f"[req] Не удалось прочитать utterance.wav: {e}")
        frames = b""
    finally:
        try:
            UTTERANCE_FILE.unlink()
        except FileNotFoundError:
            pass
    return frames


def recognize(recognizer: sr.Recognizer, frame_data: bytes) -> dict:
    """Отправляет уже готовую (записанную wake.py) фразу в Google STT.

    Возвращает словарь {"text": str, "confidence": float | None, "alternatives": list[str]}.
    text == "" означает неудачу (Google не расслышал ничего разборчивого) —
    тогда confidence == None и alternatives == [].

    confidence — уверенность Google STT в лучшем варианте (0.0-1.0). Google
    ЧАСТО не присылает это поле вообще (особенность бесплатного API) —
    тогда confidence остаётся None, это нормально, не ошибка.
    alternatives — остальные варианты распознавания той же фразы (без
    текста из "text"), от наиболее к наименее вероятного."""
    audio_data = sr.AudioData(frame_data, SAMPLE_RATE, SAMPLE_WIDTH)

    # --- ОТЛАДКА: сохраняем запись в файл, чтобы можно было прослушать и ---
    # понять, обрезается ли звук физически, или проблема в распознавании Google.
    DEBUG_AUDIO_DIR.mkdir(exist_ok=True)
    debug_path = DEBUG_AUDIO_DIR / DEBUG_AUDIO_FILE_NAME
    with open(debug_path, "wb") as f:
        f.write(audio_data.get_wav_data())
    print(f"[req] Запись сохранена для отладки: {debug_path}")

    try:
        # show_all=True — просим у Google не только лучший вариант, но и
        # уверенность/альтернативы. Без этого recognize_google() вернул бы
        # готовую строку и молча выбросил бы всё остальное.
        response = recognizer.recognize_google(audio_data, language=LANGUAGE, show_all=True)
    except sr.UnknownValueError:
        # речь была, но распознать не удалось
        return {"text": "", "confidence": None, "alternatives": []}
    except sr.RequestError as e:
        print(f"[req] Ошибка запроса к Google STT: {e}")
        log_client.send_log("ERROR", "stt_request_error", {"error": str(e)})
        return {"text": "", "confidence": None, "alternatives": []}

    alternatives_raw = response.get("alternative", []) if response else []
    if not alternatives_raw:
        # Google ответил, но по факту ничего разборчивого не расслышал.
        return {"text": "", "confidence": None, "alternatives": []}

    best = alternatives_raw[0]
    text = best.get("transcript", "")
    confidence = best.get("confidence")  # см. докстринг выше — часто None, это нормально
    alternatives = [alt["transcript"] for alt in alternatives_raw[1:] if "transcript" in alt]

    return {"text": text, "confidence": confidence, "alternatives": alternatives}


def save_phrase(result: dict) -> None:
    """Записывает результат распознавания в выходной файл (перезаписывая
    предыдущий) как JSON: текст + метаданные (уверенность, альтернативные
    варианты, время, язык). Один файл с последней фразой (перезапись), не
    журнал — журналом когда-нибудь займётся отдельная система
    логирования, это не задача rvs."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        "text": result["text"],
        "confidence": result["confidence"],
        "alternatives": result["alternatives"],
        "language": LANGUAGE,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_activity() -> None:
    """Отмечает успешное распознавание (обновляет mtime activity.flag),
    чтобы wake.py продлил окно продолжения диалога ещё на
    wake_word.continuation_window_sec секунд."""
    FLAGS_DIR.mkdir(exist_ok=True)
    ACTIVITY_FILE.touch(exist_ok=True)
    os.utime(ACTIVITY_FILE, None)  # гарантированно обновляем mtime, даже если файл уже существовал


def main() -> None:
    FLAGS_DIR.mkdir(exist_ok=True)  # чтобы папка точно существовала

    recognizer = sr.Recognizer()

    print("[req] Запущен. Жду готовую запись фразы (flags/utterance.wav)...")
    log_client.send_log("INFO", "req_started")

    while True:
        frame_data = load_utterance()

        if not frame_data:
            time.sleep(CHECK_INTERVAL)
            continue

        print("[req] Новая запись — распознаю...")
        log_client.send_log("INFO", "utterance_received", {"bytes": len(frame_data)})
        result = recognize(recognizer, frame_data)

        if result["text"]:
            conf_str = f"{result['confidence']:.2f}" if result["confidence"] is not None else "?"
            print(f"[req] Распознано (confidence={conf_str}): {result['text']}")
            if result["alternatives"]:
                print(f"[req] Другие варианты: {result['alternatives']}")
            save_phrase(result)
            mark_activity()
            log_client.send_log(
                "INFO", "speech_recognized",
                {
                    "text": result["text"],
                    "confidence": result["confidence"],
                    "alternatives_count": len(result["alternatives"]),
                },
            )
        else:
            print("[req] Речь не распознана.")
            log_client.send_log("WARNING", "speech_not_recognized")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_client.send_log("INFO", "req_stopped")
        print("\n[req] остановлен")
