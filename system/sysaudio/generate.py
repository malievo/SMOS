#!/usr/bin/env python3
"""
generate.py — сборка клипов системного аудио SMOS.

НЕ рантайм-компонент. Это build-time инструмент: запускается вручную,
когда меняется список или формулировка системных фраз (см.
sysaudio_design.md, раздел «generate.py — опционально, на потом»). smos.py
его не трогает, в рантайме SMOS piper не нужен.

Что делает:
1. Проверяет, что установлен piper-tts. Нет — печатает, как поставить, и
   выходит; сам ничего не ставит.
2. Скачивает голосовую модель Piper в models/ (один раз, ~63 МБ). Папка
   models/ — кэш, в .gitignore: в репозитории лежат только готовые WAV.
3. На каждый ключ из PHRASES синтезирует clips/<LANG>/<ключ>.wav голосом
   VOICE, плюс генерирует lead_tone.wav — короткий тон-префикс «говорит
   система» (не TTS, чистая синусоида на stdlib).
4. Перезаписывает clips.txt — опись: чем, каким голосом и с каким текстом
   собран каждый клип (чтобы через полгода попасть в тот же голос).

Голос выбран 2026-09-05: dmitri — локальный офлайновый нейросетевой голос
Piper, заметно отличается от женского gtts, которым говорит system/audio/.
Кандидатов сравнивали в voice_samples/ (папку можно удалить).

    python3 system/sysaudio/generate.py            # собрать всё
    python3 system/sysaudio/generate.py --list     # показать фразы и выйти
"""

import json
import math
import struct
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

LANG = "ru"
VOICE = "ru_RU-dmitri-medium"  # Piper, huggingface.co/rhasspy/piper-voices
_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium"

# Источник истины для того, ЧТО говорит каждый клип. Ключ = имя файла:
# clips/<LANG>/<ключ>.wav. Рантайм (sysaudio.play_clip) зовёт клип по
# этому же ключу. Поменял формулировку — правишь здесь и запускаешь
# generate.py заново.
PHRASES = {
    "system_started":   "Система запущена.",
    "system_stopping":  "Выключение системы.",
    "startup_failed":   "Запуск не удался.",
    "update_available": "Доступно обновление. Обновить сейчас?",
    "update_started":   "Обновление начато.",
    "update_applying":  "Устанавливаю обновление.",
    "update_cancelled": "Обновление отменено.",
    "update_postponed": "Обновление отложено.",
    "update_failed":    "Обновление не удалось.",
    "no_network":       "Нет подключения к интернету.",
    "mic_unavailable":  "Микрофон недоступен.",
}

CLIPS_DIR = SCRIPT_DIR / "clips" / LANG
MODELS_DIR = SCRIPT_DIR / "models"
MANIFEST_FILE = SCRIPT_DIR / "clips.txt"
LEAD_TONE_FILE = CLIPS_DIR / "lead_tone.wav"


def _require_piper() -> None:
    try:
        import piper  # noqa: F401
    except ImportError:
        sys.exit(
            "[generate] нужен piper-tts:\n"
            "    pip install --user piper-tts\n"
            "(build-time зависимость — в рантайме SMOS не нужна)"
        )


def ensure_model() -> Path:
    """Скачивает .onnx + .onnx.json в models/ (один раз). models/ — кэш,
    в .gitignore; коммитятся только готовые clips/."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    onnx = MODELS_DIR / f"{VOICE}.onnx"
    conf = MODELS_DIR / f"{VOICE}.onnx.json"
    for path, url in ((onnx, f"{_HF_BASE}/{VOICE}.onnx"),
                      (conf, f"{_HF_BASE}/{VOICE}.onnx.json")):
        if path.exists() and path.stat().st_size > 1000:
            continue
        print(f"[generate] скачиваю {path.name} …")
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                path.write_bytes(r.read())
        except Exception as e:  # noqa: BLE001 — сеть/HTTP/диск, любой сбой -> стоп
            sys.exit(f"[generate] не удалось скачать {url}: {e}")
    return onnx


def model_sample_rate(onnx: Path) -> int:
    """Частота дискретизации модели — чтобы тон-префикс совпадал с
    клипами Piper (обычно 22050 для *-medium)."""
    try:
        conf = onnx.parent / (onnx.name + ".json")
        return int(json.loads(conf.read_text(encoding="utf-8"))["audio"]["sample_rate"])
    except Exception:  # noqa: BLE001
        return 22050


def synth(onnx: Path, text: str, out: Path) -> None:
    """Один вызов Piper: текст на stdin, WAV в out."""
    out.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [sys.executable, "-m", "piper", "-m", str(onnx), "-f", str(out)],
        input=text + "\n", text=True, capture_output=True,
    )
    if res.returncode != 0 or not out.exists():
        sys.exit(f"[generate] piper упал на {out.name}:\n{res.stderr.strip()}")


class GenerateError(Exception):
    """piper не установлен, модель не скачалась или синтез упал —
    для вызывающих, которым нужен не sys.exit, а обычное исключение
    (sysaudio.py --generate)."""


def synthesize(text: str, out: Path) -> None:
    """Публичная точка синтеза: гарантирует piper + модель, синтезирует
    text в out тем же голосом VOICE. В отличие от CLI (который печатает и
    выходит) — при любой проблеме бросает GenerateError."""
    try:
        _require_piper()
        onnx = ensure_model()
        synth(onnx, text, out)
    except SystemExit as e:
        raise GenerateError(str(e)) from None


def make_lead_tone(out: Path, sample_rate: int) -> None:
    """Короткий тон-префикс «говорит система»: две восходящие ноты
    (ля -> ми, кварта+), ~0.16 c, с плавным нарастанием/затуханием на
    краях каждой ноты, чтобы не щёлкало. Не TTS — чистая синусоида на
    stdlib. Формат как у клипов Piper (моно, 16 бит, та же частота),
    чтобы можно было склеивать встык. Заглушка — при желании заменить
    своим звуком той же длины и частоты дискретизации."""
    notes = [(880.0, 0.07), (1320.0, 0.09)]
    amp = 0.22
    fade = max(1, int(0.006 * sample_rate))
    frames = bytearray()
    for freq, dur in notes:
        n = int(dur * sample_rate)
        for i in range(n):
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            else:
                env = 1.0
            sample = amp * env * math.sin(2 * math.pi * freq * i / sample_rate)
            frames += struct.pack("<h", int(sample * 32767))
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))


def write_manifest(sample_rate: int) -> None:
    """Перезаписывает clips.txt из PHRASES — опись не расходится с тем,
    что реально сгенерировано."""
    key_w = max(len(k) for k in PHRASES)
    lines = [
        "Клипы системного аудио SMOS — как сгенерированы",
        "=" * 46,
        "",
        "Пересобрать:  python3 system/sysaudio/generate.py",
        "Список/тексты — в PHRASES внутри generate.py (источник истины).",
        "",
        "Движок : Piper — нейросетевой TTS, локально, офлайн. pip install piper-tts",
        f"Модель : {VOICE}",
        "         huggingface.co/rhasspy/piper-voices  ru/ru_RU/dmitri/medium/",
        "         кэш в system/sysaudio/models/ (в .gitignore, не исходник)",
        f"Формат : WAV, {sample_rate} Гц, моно, 16 бит (как отдаёт Piper)",
        "Голос  : мужской, нейтральный. Намеренно НЕ голос ассистента",
        "         (system/audio/ -> gtts, женский) — чтобы на слух отличать",
        "         «говорит система» от «отвечает ассистент».",
        "Выбран : 2026-09-05, сравнив кандидатов в voice_samples/.",
        "",
        f"clips/{LANG}/<ключ>.wav:",
        "",
    ]
    for key, text in PHRASES.items():
        lines.append(f"  {key.ljust(key_w)}  «{text}»")
    lines += [
        "",
        "  lead_tone.wav  тон-префикс «говорит система» (две ноты, ~0.16 c), НЕ TTS.",
        "                 Генерируется generate.py. Заглушка — можно заменить",
        "                 своим звуком той же длины и частоты дискретизации.",
        "",
    ]
    MANIFEST_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if "--list" in sys.argv[1:]:
        for key, text in PHRASES.items():
            print(f"{key:18} {text}")
        return

    _require_piper()
    onnx = ensure_model()
    sample_rate = model_sample_rate(onnx)

    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    for key, text in PHRASES.items():
        out = CLIPS_DIR / f"{key}.wav"
        synth(onnx, text, out)
        print(f"[generate] {out.relative_to(SCRIPT_DIR)}  «{text}»")

    make_lead_tone(LEAD_TONE_FILE, sample_rate)
    print(f"[generate] {LEAD_TONE_FILE.relative_to(SCRIPT_DIR)}  (тон-префикс)")

    write_manifest(sample_rate)
    print(f"[generate] {MANIFEST_FILE.name} обновлён")
    print(f"[generate] готово: {len(PHRASES)} фраз + тон -> {CLIPS_DIR}")


if __name__ == "__main__":
    main()
