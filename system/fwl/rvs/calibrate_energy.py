"""
calibrate_energy.py — разовый калибровщик recording.energy_threshold для SMOS FWL (rvs)

Зачем: recording.energy_threshold в config.json — фиксированный порог
громкости, который wake.py использует, чтобы отличить тишину от речи (и
понять, что фраза закончилась — см. wake.py, состояние "recording").
Значение по умолчанию (300) подобрано "на глаз" и не привязано к
твоему реальному микрофону и комнате — в тихой комнате оно может быть
завышено (кусок тихой речи в конце фразы обрежется раньше времени), а в
шумной — заниженным (фоновый шум будет постоянно приниматься за начало
новой фразы).

Что делает скрипт:
1. Просит помолчать несколько секунд и измеряет уровень фонового шума
   комнаты — тем же способом (RMS чанка), которым сам wake.py решает,
   тишина сейчас или нет.
2. Просит сказать что-нибудь обычным голосом и измеряет уровень речи —
   для сравнения, чтобы наглядно видеть зазор между тишиной и речью, а
   не гадать по одному числу.
3. Предлагает конкретное значение energy_threshold — с запасом над
   измеренным шумом, но заметно ниже измеренной речи, и предупреждает,
   если этот зазор подозрительно маленький (например, если в комнате
   шумно или ты говорил слишком тихо при записи).

Значение НЕ прописывается в config.json автоматически — скрипт только
показывает рекомендацию, дальше сам решаешь, вставлять ли её
(recording -> energy_threshold).

Установка зависимостей: те же, что у wake.py (pyaudio, numpy) — если
wake.py уже запускается, для этого скрипта ничего доустанавливать не
нужно.

Запуск:
    python calibrate_energy.py
"""

import sys
import time
from pathlib import Path

import numpy as np
import pyaudio

# --- Пути (скрипт сам находит себя, не зависит от места запуска) ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти config.py рядом с собой

import config  # noqa: E402  (импорт после правки sys.path — так и задумано)

CFG = config.load(SCRIPT_DIR)

SAMPLE_RATE = CFG["audio"]["sample_rate"]
CHANNELS = CFG["audio"]["channels"]
CHUNK_SIZE = CFG["audio"]["chunk_size"]

_PYAUDIO_FORMATS_BY_BIT_DEPTH = {
    8: pyaudio.paInt8,
    16: pyaudio.paInt16,
    24: pyaudio.paInt24,
    32: pyaudio.paInt32,
}
_bit_depth = CFG["audio"]["bit_depth"]
if _bit_depth not in _PYAUDIO_FORMATS_BY_BIT_DEPTH:
    print(f"[calibrate] Неподдерживаемый audio.bit_depth={_bit_depth} в config.json, использую 16.")
    _bit_depth = 16
FORMAT = _PYAUDIO_FORMATS_BY_BIT_DEPTH[_bit_depth]

CURRENT_THRESHOLD = CFG["recording"]["energy_threshold"]

SILENCE_SECONDS = 5
SPEECH_SECONDS = 3


def _rms(raw_chunk: bytes) -> float:
    """Та же метрика громкости, что использует wake.py — среднеквадратичная
    амплитуда чанка."""
    samples = np.frombuffer(raw_chunk, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


def _record_rms_series(stream, duration_sec: float) -> list:
    """Пишет duration_sec секунд с микрофона и возвращает список RMS
    каждого прочитанного чанка (не сам звук — он тут не нужен)."""
    n_chunks = max(1, round(duration_sec * SAMPLE_RATE / CHUNK_SIZE))
    values = []
    for _ in range(n_chunks):
        raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        values.append(_rms(raw))
    return values


def _percentile(values: list, p: float) -> float:
    """Простой перцентиль без numpy.percentile — оставлено вручную для
    прозрачности расчёта (видно ровно что происходит)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[idx]


def suggest_threshold(noise_values: list) -> float:
    """Предлагает порог с запасом над измеренным фоновым шумом.

    Берём максимум из:
      - 95-й перцентиль шума * 3 (обычный уровень шума с хорошим запасом)
      - пиковый шум * 1.5 (чтобы редкий, но громкий всплеск — шаги,
        скрип стула — тоже не путался с началом речи)
    и не даём порогу упасть ниже 50 (предохранитель на случай, если в
    комнате настолько тихо, что RMS шума почти нулевой — иначе порог
    получился бы нереалистично низким)."""
    if not noise_values:
        return float(CURRENT_THRESHOLD)
    p95 = _percentile(noise_values, 95)
    peak = max(noise_values)
    return max(p95 * 3, peak * 1.5, 50.0)


def main() -> None:
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    try:
        print("[calibrate] Сейчас измерим уровень тишины в комнате.")
        print(f"[calibrate] Помолчи {SILENCE_SECONDS} секунд — начинаю через 2...")
        time.sleep(2)
        print("[calibrate] Записываю тишину...")
        noise_values = _record_rms_series(stream, SILENCE_SECONDS)
        print("[calibrate] Готово.")

        print()
        print(f"[calibrate] Теперь скажи что-нибудь обычным голосом ({SPEECH_SECONDS} сек) — начинаю через 2...")
        time.sleep(2)
        print("[calibrate] Записываю речь...")
        speech_values = _record_rms_series(stream, SPEECH_SECONDS)
        print("[calibrate] Готово.")
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()

    noise_mean = sum(noise_values) / len(noise_values)
    noise_peak = max(noise_values)
    noise_p95 = _percentile(noise_values, 95)

    speech_mean = sum(speech_values) / len(speech_values)
    speech_min = min(speech_values)
    speech_p10_raw = _percentile(speech_values, 10)  # сырой перцентиль — см. комментарий ниже

    # ВАЖНО: сравнивать порог с "самым тихим чанком за всю запись речи"
    # (speech_p10_raw) — плохая идея. Внутри ЛЮБОЙ нормальной речи есть
    # естественные микропаузы между словами (смычные согласные, короткие
    # вдохи) — они почти всегда проваливаются в уровень шума, даже если
    # человек говорит совершенно нормально и не тихо. Если сравнивать
    # порог с этими паузами, предупреждение будет ложно срабатывать
    # почти всегда, независимо от реальной громкости голоса.
    #
    # Вместо этого сначала отсекаем чанки, которые сами похожи на шум
    # (RMS <= 95-й перцентиль шума — то есть, скорее всего, это пауза
    # внутри речи, а не сказанное слово), и уже среди ОСТАВШИХСЯ,
    # заведомо "голосовых" чанков берём тихий перцентиль — это и есть
    # честная оценка "как звучит тихая часть реально сказанного".
    voiced_speech_values = [v for v in speech_values if v > noise_p95]
    if voiced_speech_values:
        speech_quiet_level = _percentile(voiced_speech_values, 10)
    else:
        # Совсем крайний случай — вся запись речи не громче шума (человек
        # либо промолчал, либо действительно говорил очень тихо).
        speech_quiet_level = speech_mean

    suggested = suggest_threshold(noise_values)

    print()
    print("=" * 60)
    print(f"Фоновый шум:  среднее={noise_mean:.0f}  95-й перцентиль={noise_p95:.0f}  пик={noise_peak:.0f}")
    print(f"Речь:         среднее={speech_mean:.0f}  минимум={speech_min:.0f}"
          f"  (сырой 10-й перцентиль={speech_p10_raw:.0f} — включает паузы между словами, не показатель)")
    print(f"Речь (только голосовые чанки, {len(voiced_speech_values)}/{len(speech_values)}):"
          f"  тихий уровень={speech_quiet_level:.0f}")
    print(f"Текущий порог в config.json (recording.energy_threshold): {CURRENT_THRESHOLD}")
    print(f"Рекомендуемый порог: {suggested:.0f}")
    print("=" * 60)

    if suggested >= speech_quiet_level:
        print("[calibrate] ВНИМАНИЕ: рекомендуемый порог близко к тихой части голоса или выше.")
        print("            Либо в комнате шумно, либо во время записи ты говорил слишком тихо.")
        print("            Попробуй перезапустить калибровку, говоря чуть громче и чётче.")
    else:
        margin = speech_quiet_level - suggested
        print(f"[calibrate] Запас между порогом и тихой частью голоса: {margin:.0f} — выглядит нормально.")

    print()
    print("Чтобы применить значение: открой config.json -> recording -> energy_threshold")
    print(f"и поставь {suggested:.0f} (сейчас там {CURRENT_THRESHOLD}).")


if __name__ == "__main__":
    main()
