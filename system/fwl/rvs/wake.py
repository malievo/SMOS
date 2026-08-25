"""
wake.py — детекция активационного слова И запись фразы (Wake) для SMOS FWL

ИСТОРИЯ (2026-08-19): раньше wake.py только детектил активационное слово
и передавал req.py "довесок" (prebuffer) — небольшой кусок аудио ДО
момента активации, а саму фразу req.py записывал СВОИМ, отдельно
открытым потоком микрофона. Довесок и живую запись затем склеивали
(сначала "встык", потом с затуханием на стыке). На практике это
регулярно портило распознавание: два независимо открытых потока на один
физический микрофон (в логах были видны ошибки ALSA dmix/dsnoop —
устройство не умеет нормально делиться между двумя процессами), плюс
сам факт двух РАЗНЫХ записей, которые нужно было склеивать — источник
щелчков и потерянных кусков речи на границе.

НОВАЯ СХЕМА: wake.py — ЕДИНСТВЕННЫЙ владелец микрофона в системе. Он не
просто ловит активационное слово, а сам же дожидается конца фразы (по
тишине) и сохраняет ЦЕЛЬНУЮ, ни с чем не склеенную запись. req.py эту
запись только читает и отправляет в Google STT — микрофон он больше не
трогает вообще (см. req.py за подробностями).

Логика:
- Скрипт сам находит папку, в которой лежит.
- Все настройки читает из config.json рядом с собой (через config.py).
  Если config.json нет или он битый — используются значения по
  умолчанию, скрипт не падает (подробности — в config.py).
- Два состояния:
    1) "listening" — постоянно слушает микрофон и прогоняет звук через
       локальную модель openWakeWord (БЕЗ отправки куда-либо в
       интернет), плюс держит кольцевой буфер последних
       wake_word.prebuffer_seconds секунд аудио (довесок ДО слова).
    2) "recording" — как только модель уверенно услышала активационное
       слово (или сработало продолжение диалога, см. ниже), переходит
       в запись: копит все чанки подряд (начиная с довеска из
       кольцевого буфера), пока не обнаружит recording.pause_threshold_sec
       секунд подряд тишины (или не упрётся в
       recording.max_utterance_seconds — предохранитель на случай, если
       тишина в комнате никогда не наступит). После этого сохраняет всё
       накопленное ОДНИМ файлом в flags/utterance.wav и возвращается в
       "listening".
- "Тишина"/"речь" определяется простым порогом громкости (RMS чанка
  против recording.energy_threshold) — то же самое, что раньше делала
  библиотека speech_recognition внутри req.py, только теперь сделано
  вручную прямо здесь, потому что это тот же самый непрерывный поток
  звука, а не отдельная запись.
- Файл flags/utterance.wav пишется атомарно (сначала во временный файл,
  потом переименование) — чтобы req.py никогда не мог прочитать
  недописанный WAV, даже если проверит файл ровно в момент записи.
- Дебаунс: пока произносится само активационное слово, модель обычно
  держит score выше порога не один звуковой чанк, а несколько подряд
  (слово длится дольше одного чанка ~80мс). Повторное срабатывание в
  пределах wake_word.detection_cooldown_sec игнорируется — иначе одно
  произнесённое слово могло бы начать запись по несколько раз подряд.
- Продолжение диалога без повторения слова активации: если req.py
  недавно (см. flags/activity.flag) успешно распознал фразу, следующие
  wake_word.continuation_window_sec секунд ЛЮБАЯ речь (даже без
  активационного слова) тоже считается началом новой фразы и уходит в
  запись — так система не заставляет заново говорить "Алекса" посреди
  диалога. Явное произнесение активационного слова работает всегда,
  независимо от этого окна.

ВАЖНО: пока что этот скрипт рассчитан на то, что лежит в ТОЙ ЖЕ
папке, что и req.py — они используют общий config.json и общую папку
flags/. (Позже, когда будешь оформлять по-нормальному, стоит вынести
flags в общую папку модуля Ear, как обсуждали.)

Установка зависимостей:
    pip install openwakeword pyaudio numpy

Модель:
    Пока в config.json (wake_word.model_path) не указана своя обученная
    модель под своё слово из Wakeword Training Center
    (openwakeword.com/train), скрипт по умолчанию использует ВСЕ
    встроенные предобученные модели ("hey_jarvis", "alexa" и др.) —
    просто чтобы проверить, что вся цепочка (детект -> запись -> Req
    распознаёт) вообще работает.

    Когда обучишь и скачаешь свою модель (.onnx или .tflite) — положи
    файл рядом со скриптом и укажи путь в config.json -> wake_word.model_path.
"""

import collections
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pyaudio
from openwakeword.model import Model

# --- Пути (скрипт сам находит себя, не зависит от места запуска) ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти config.py рядом с собой

import config  # noqa: E402  (импорт после правки sys.path — так и задумано)
import log_client  # noqa: E402

CFG = config.load(SCRIPT_DIR)

FLAGS_DIR = SCRIPT_DIR / CFG["paths"]["flags_dir"]
ACTIVITY_FILE = FLAGS_DIR / CFG["paths"]["activity_file"]  # общий с req.py сигнал "было успешное распознавание"
UTTERANCE_FILE = FLAGS_DIR / CFG["paths"]["utterance_file"]  # готовая цельная запись фразы, для req.py

# --- Настройки (все — из config.json, см. config.py -> DEFAULTS за описанием) ---
_model_path_str = CFG["wake_word"]["model_path"]
if _model_path_str:
    MODEL_PATH = Path(_model_path_str)
    if not MODEL_PATH.is_absolute():
        MODEL_PATH = SCRIPT_DIR / MODEL_PATH
else:
    MODEL_PATH = None  # None -> загружаются ВСЕ встроенные модели ("hey jarvis", "alexa" и др.) для теста

DETECTION_THRESHOLD = CFG["wake_word"]["detection_threshold"]
# Дебаунс: минимальный интервал между двумя срабатываниями детекции — чтобы
# одно произнесённое слово (несколько чанков подряд со score > threshold)
# не запускало запись по несколько раз. См. докстринг модуля.
DETECTION_COOLDOWN = CFG["wake_word"]["detection_cooldown_sec"]
# Сколько секунд после успешного распознавания разрешаем начинать запись
# без повторного произнесения активационного слова.
CONTINUATION_WINDOW = CFG["wake_word"]["continuation_window_sec"]

CHUNK_SIZE = CFG["audio"]["chunk_size"]
SAMPLE_RATE = CFG["audio"]["sample_rate"]
CHANNELS = CFG["audio"]["channels"]
SAMPLE_WIDTH = CFG["audio"]["bit_depth"] // 8  # байт на сэмпл
CHUNK_DURATION_SEC = CHUNK_SIZE / SAMPLE_RATE

# pyaudio понимает битность только через свои константы формата —
# сопоставляем bit_depth из конфига с ними. Поддерживаются стандартные
# варианты; для распознавания речи нужен и проверен только 16.
_PYAUDIO_FORMATS_BY_BIT_DEPTH = {
    8: pyaudio.paInt8,
    16: pyaudio.paInt16,
    24: pyaudio.paInt24,
    32: pyaudio.paInt32,
}
_bit_depth = CFG["audio"]["bit_depth"]
if _bit_depth not in _PYAUDIO_FORMATS_BY_BIT_DEPTH:
    print(f"[wake] Неподдерживаемый audio.bit_depth={_bit_depth} в config.json, использую 16.")
    _bit_depth = 16
FORMAT = _PYAUDIO_FORMATS_BY_BIT_DEPTH[_bit_depth]

# Сколько секунд аудио держим "про запас" в кольцевом буфере — должно с
# запасом перекрывать задержку детекции модели + время, за которое человек
# успевает произнести следующее слово тем же выдохом. Если в отладочных
# записях (debug_audio/last_recording.wav) начало фразы всё ещё обрезано —
# увеличь wake_word.prebuffer_seconds в config.json.
PREBUFFER_SECONDS = CFG["wake_word"]["prebuffer_seconds"]
PREBUFFER_CHUNKS = max(1, round(PREBUFFER_SECONDS * SAMPLE_RATE / CHUNK_SIZE))

# --- Настройки записи фразы (когда она закончилась) ---
ENERGY_THRESHOLD = CFG["recording"]["energy_threshold"]
PAUSE_THRESHOLD_SEC = CFG["recording"]["pause_threshold_sec"]
MAX_UTTERANCE_SECONDS = CFG["recording"]["max_utterance_seconds"]


def get_activity_mtime() -> float:
    """Возвращает mtime flags/activity.flag или 0.0, если файла ещё нет."""
    try:
        return ACTIVITY_FILE.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _rms(raw_chunk: bytes) -> float:
    """Среднеквадратичная амплитуда чанка — простая метрика "громкости"
    для определения начала/конца фразы (VAD), без внешних библиотек.
    Тот же смысл, что раньше был у energy_threshold внутри
    speech_recognition, просто теперь считается вручную."""
    samples = np.frombuffer(raw_chunk, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


def save_utterance(chunks) -> None:
    """Сохраняет накопленные чанки одной фразы (довесок ДО слова + сама
    фраза + немного тишины после неё, естественно захваченной, пока мы
    ждали pause_threshold_sec) одним WAV-файлом в flags/utterance.wav.

    Пишет АТОМАРНО: сначала во временный файл рядом, потом переименование
    (Path.replace — атомарно на одной файловой системе). Так req.py
    никогда не увидит недописанный/битый WAV, даже если проверит файл
    ровно в момент записи."""
    FLAGS_DIR.mkdir(exist_ok=True)
    tmp_path = UTTERANCE_FILE.parent / (UTTERANCE_FILE.name + ".tmp")
    with wave.open(str(tmp_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(chunks))
    tmp_path.replace(UTTERANCE_FILE)


def main() -> None:
    if MODEL_PATH:
        model = Model(wakeword_model_paths=[str(MODEL_PATH)])
    else:
        model = Model()  # без аргумента -> загружаются ВСЕ встроенные предобученные модели (для теста)

    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print("[wake] Запущен. Слушаю активационное слово (офлайн, без интернета)...")
    log_client.send_log("INFO", "wake_started")

    state = "listening"  # "listening" | "recording"
    ring_buffer = collections.deque(maxlen=PREBUFFER_CHUNKS)  # кольцевой буфер последних PREBUFFER_SECONDS сек
    utterance_chunks = []
    silence_run_sec = 0.0
    utterance_started_at = 0.0
    last_trigger_time = 0.0  # для дебаунса детекции слова
    continuation_until = 0.0  # до какого момента разрешено начать запись без слова активации
    last_activity_mtime = get_activity_mtime()  # чтобы не среагировать на старый файл при старте

    try:
        while True:
            raw_audio = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            now = time.time()

            if state == "listening":
                ring_buffer.append(raw_audio)
                audio_chunk = np.frombuffer(raw_audio, dtype=np.int16)
                predictions = model.predict(audio_chunk)

                heard_word = None
                heard_score = None
                for wakeword, score in predictions.items():
                    if score > DETECTION_THRESHOLD and (now - last_trigger_time) >= DETECTION_COOLDOWN:
                        heard_word = wakeword
                        heard_score = float(score)  # openWakeWord отдаёт numpy.float32 — не JSON-сериализуемо как есть
                        break

                start_recording = False
                if heard_word is not None:
                    last_trigger_time = now
                    print(f"[wake] Услышал '{heard_word}' — начинаю запись фразы")
                    log_client.send_log(
                        "INFO", "wake_word_detected",
                        {"wakeword": heard_word, "score": heard_score},
                    )
                    start_recording = True
                elif now < continuation_until and _rms(raw_audio) > ENERGY_THRESHOLD:
                    # Продолжение диалога: слово не звучало, но недавно было
                    # успешное распознавание, и сейчас снова кто-то говорит.
                    print("[wake] Продолжение диалога — начинаю запись фразы")
                    log_client.send_log("INFO", "continuation_recording_started")
                    start_recording = True

                if start_recording:
                    state = "recording"
                    utterance_chunks = list(ring_buffer)  # довесок — то, что уже накоплено ДО этого момента
                    silence_run_sec = 0.0
                    utterance_started_at = now

            elif state == "recording":
                utterance_chunks.append(raw_audio)

                if _rms(raw_audio) < ENERGY_THRESHOLD:
                    silence_run_sec += CHUNK_DURATION_SEC
                else:
                    silence_run_sec = 0.0

                too_long = (now - utterance_started_at) >= MAX_UTTERANCE_SECONDS
                phrase_ended = silence_run_sec >= PAUSE_THRESHOLD_SEC or too_long

                if phrase_ended:
                    reason = "max_length" if too_long else "silence"
                    if too_long:
                        print(f"[wake] Превышена максимальная длина фразы ({MAX_UTTERANCE_SECONDS}с) — заканчиваю запись")
                    else:
                        print("[wake] Тишина — фраза закончена, сохраняю запись")
                    save_utterance(utterance_chunks)
                    log_client.send_log(
                        "INFO", "utterance_saved",
                        {"reason": reason, "duration_sec": round(now - utterance_started_at, 2)},
                    )
                    utterance_chunks = []
                    ring_buffer.clear()  # копим довесок заново с чистого листа
                    state = "listening"

            # req.py что-то успешно распознал -> продлеваем окно продолжения
            # диалога (разрешаем начинать запись без нового слова активации)
            activity_mtime = get_activity_mtime()
            if activity_mtime > last_activity_mtime:
                last_activity_mtime = activity_mtime
                continuation_until = now + CONTINUATION_WINDOW
                print("[wake] Req успешно распознал фразу — окно продолжения диалога продлено")
                log_client.send_log("DEBUG", "continuation_window_extended")

    except KeyboardInterrupt:
        print("[wake] Остановлен.")
        log_client.send_log("INFO", "wake_stopped")
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()


if __name__ == "__main__":
    main()
