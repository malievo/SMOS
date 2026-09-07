"""
synth.py — синтез речи для CNPS: по предложениям, с кэшем, несколько движков.

Почему по предложениям (см. cnps_design.md, «Синтез по предложениям»):
- первый звук идёт раньше;
- чистые точки реза для «продолжи» (с начала прерванного предложения);
- дешёвый повтор — синтезированное предложение лежит в кэше по хешу
  (движок + голос + текст).

Движки (какой применить к заявке — решает CNPS, см. resolve_engine):
- "gtts"    — Google TTS, ЖЕНСКИЙ голос, онлайн: синтез в MP3, кэш
              cache/<hash>.mp3, играется mp3-плеером. Голос ассистента
              (ответы outputstructurizer, болталка). Нужен `pip install
              gtts` + сеть + mp3-плеер (gst-play-1.0 / mpg123 / ffplay).
- "piper"   — нейросетевой TTS, МУЖСКОЙ (ru_RU-dmitri-medium), локально/
              офлайн. Тёплый PiperVoice.load один раз, кэш
              cache/<hash>.wav. Голос системных сообщений; и офлайн-запас
              для gtts.
- "spd-say" — speech-dispatcher. WAV не даёт: играется прямой командой
              (без кэша, без «первого звука раньше»). Последний запас.

Цепочка запаса: [движок заявки] + synth.fallback_engine (строка или
список). gtts офлайн -> piper -> spd-say. Ни один сбой не бросает
наружу: prepare_sentence() возвращает ("none", None), CNPS печатает
текст.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path

ENGINES = ("gtts", "piper", "spd-say")

# Границы предложений: точка/!/?/… (возможно с кавычкой/скобкой) + пробелы.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])[\"»)\]]?\s+")


def split_sentences(text: str) -> list[str]:
    """Делит текст на предложения. Нет знаков конца — весь текст одним
    элементом. Пустые куски отбрасываются."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text)]
    parts = [p for p in parts if p]
    return parts or [text]


class Synth:
    """Обёртка вокруг движков синтеза. Один экземпляр на весь сеанс CNPS."""

    def __init__(self, cfg: dict, script_dir: Path, log=None):
        s = cfg["synth"]
        self.default_engine = s.get("engine", "piper")
        self.engine_by_source = dict(s.get("engine_by_source", {}))
        fb = s.get("fallback_engine", ["piper", "spd-say"])
        self.fallbacks = [fb] if isinstance(fb, str) else list(fb)

        self.piper_voice = s.get("piper_voice", "")
        self.piper_timeout = s.get("piper_timeout_sec", 30)
        self.model_dir = (script_dir / s.get("model_dir", "../sysaudio/models")).resolve()

        self.spd_say_command = list(s.get("spd_say_command", ["spd-say", "-w", "-l", "ru"]))

        self.gtts_cfg = s.get("gtts", {})
        self.gtts_lang = self.gtts_cfg.get("lang", "ru")
        self.gtts_tld = self.gtts_cfg.get("tld", "com")
        self.gtts_slow = bool(self.gtts_cfg.get("slow", False))

        self.cache_dir = (script_dir / cfg["paths"]["cache_dir"]).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._log = log or (lambda *a, **k: None)

        self._piper_pkg: bool | None = None
        self._gtts_pkg: bool | None = None
        self._voice = None
        self._voice_failed = False
        self._lock = threading.Lock()
        self._warned_none = False

    # ---- выбор движка под заявку -------------------------------------

    def resolve_engine(self, manifest_voice, source: str) -> str:
        """Движок для заявки: явное поле манифеста (`voice`/`engine`) ->
        synth.engine_by_source[source] -> synth.engine (дефолт)."""
        if isinstance(manifest_voice, str) and manifest_voice in ENGINES:
            return manifest_voice
        by_src = self.engine_by_source.get(source)
        if by_src in ENGINES:
            return by_src
        return self.default_engine if self.default_engine in ENGINES else "piper"

    def _chain(self, engine: str) -> list[str]:
        chain = [engine]
        for f in self.fallbacks:
            if f in ENGINES and f not in chain:
                chain.append(f)
        return chain

    # ---- диагностика для стартового вывода -------------------------

    def engine_status(self) -> str:
        return ", ".join(f"{e}: {self._ready(e)}" for e in ENGINES)

    def _ready(self, engine: str) -> str:
        if engine == "piper":
            if not self._have_piper_pkg():
                return "нет пакета piper-tts"
            onnx = self.model_dir / f"{self.piper_voice}.onnx"
            return "готов" if onnx.is_file() else f"нет модели {onnx.name}"
        if engine == "gtts":
            if not self._have_gtts_pkg():
                return "нет пакета gtts"
            p = self._mp3_player()
            return "готов (нужна сеть)" if p else "нет mp3-плеера"
        if engine == "spd-say":
            cmd = self.spd_say_command
            return "готов" if cmd and shutil.which(cmd[0]) else f"нет команды {cmd[0] if cmd else '(пусто)'}"
        return "неизвестный движок"

    def _have_piper_pkg(self) -> bool:
        if self._piper_pkg is None:
            try:
                import piper  # noqa: F401
                self._piper_pkg = True
            except ImportError:
                self._piper_pkg = False
        return self._piper_pkg

    def _have_gtts_pkg(self) -> bool:
        if self._gtts_pkg is None:
            try:
                import gtts  # noqa: F401
                self._gtts_pkg = True
            except ImportError:
                self._gtts_pkg = False
        return self._gtts_pkg

    def mp3_player(self) -> list[str] | None:
        """Первая mp3-команда из synth.gtts.players с программой в PATH."""
        for cand in self.gtts_cfg.get("players", [["gst-play-1.0", "--quiet"]]):
            if cand and shutil.which(cand[0]):
                return list(cand)
        return None

    _mp3_player = mp3_player  # внутреннее имя, для _ready()

    # ---- синтез ----------------------------------------------------

    def _cache_path(self, sentence: str, engine: str, ext: str) -> Path:
        voice = self.piper_voice if engine == "piper" else (f"{self.gtts_lang}-{self.gtts_tld}" if engine == "gtts" else "-")
        key = hashlib.sha1(f"{engine}|{voice}|{sentence}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.{ext}"

    def _get_voice(self):
        if self._voice is not None or self._voice_failed:
            return self._voice
        try:
            from piper import PiperVoice
            onnx = self.model_dir / f"{self.piper_voice}.onnx"
            self._voice = PiperVoice.load(str(onnx))
        except Exception as e:  # noqa: BLE001
            self._voice_failed = True
            self._log("WARNING", "piper_warm_load_failed", {"error": str(e)})
        return self._voice

    def _piper_wav(self, sentence: str) -> Path | None:
        if not self._have_piper_pkg() or not (self.model_dir / f"{self.piper_voice}.onnx").is_file():
            return None
        out = self._cache_path(sentence, "piper", "wav")
        if out.is_file() and out.stat().st_size > 44:
            return out
        with self._lock:
            if out.is_file() and out.stat().st_size > 44:
                return out
            tmp = out.with_suffix(".wav.tmp")
            voice = self._get_voice()
            if voice is not None:
                try:
                    with wave.open(str(tmp), "wb") as wf:
                        voice.synthesize_wav(sentence, wf)
                    if tmp.is_file() and tmp.stat().st_size > 44:
                        tmp.replace(out)
                        return out
                except Exception as e:  # noqa: BLE001
                    self._log("WARNING", "synth_failed", {"engine": "piper-api", "error": str(e)})
                tmp.unlink(missing_ok=True)
            # запас: python -m piper подпроцессом
            onnx = self.model_dir / f"{self.piper_voice}.onnx"
            try:
                res = subprocess.run(
                    [sys.executable, "-m", "piper", "-m", str(onnx), "-f", str(tmp)],
                    input=sentence + "\n", text=True, capture_output=True, timeout=self.piper_timeout,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                self._log("WARNING", "synth_failed", {"engine": "piper-cli", "error": str(e)})
                tmp.unlink(missing_ok=True)
                return None
            if res.returncode != 0 or not tmp.is_file():
                self._log("WARNING", "synth_failed", {"engine": "piper-cli", "stderr": (res.stderr or "")[:200]})
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
            return out

    def _gtts_mp3(self, sentence: str) -> Path | None:
        if not self._have_gtts_pkg() or self._mp3_player() is None:
            return None
        out = self._cache_path(sentence, "gtts", "mp3")
        if out.is_file() and out.stat().st_size > 100:
            return out
        with self._lock:
            if out.is_file() and out.stat().st_size > 100:
                return out
            from gtts import gTTS
            tmp = out.with_suffix(".mp3.tmp")
            try:
                gTTS(text=sentence, lang=self.gtts_lang, tld=self.gtts_tld, slow=self.gtts_slow).save(str(tmp))
            except Exception as e:  # noqa: BLE001 — нет сети / отказ Google / диск
                self._log("WARNING", "synth_failed", {"engine": "gtts", "error": str(e)})
                tmp.unlink(missing_ok=True)
                return None
            if not tmp.is_file() or tmp.stat().st_size <= 100:
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
            return out

    def _spd_cmd(self, sentence: str) -> list[str]:
        cmd = self.spd_say_command
        if not cmd or not shutil.which(cmd[0]):
            return []
        return [*cmd, sentence]

    def _one(self, sentence: str, engine: str):
        if engine == "piper":
            p = self._piper_wav(sentence)
            return ("wav", str(p)) if p else ("none", None)
        if engine == "gtts":
            p = self._gtts_mp3(sentence)
            return ("mp3", str(p)) if p else ("none", None)
        if engine == "spd-say":
            c = self._spd_cmd(sentence)
            return ("cmd", c) if c else ("none", None)
        return ("none", None)

    def prepare_sentence(self, sentence: str, engine: str):
        """Готовит источник звука для предложения движком engine (с
        откатом по цепочке). Возвращает ("wav", путь) | ("mp3", путь) |
        ("cmd", [команда]) | ("none", None). Может ходить в сеть (gtts)
        и синтезировать (piper) — вызывать БЕЗ lock демона."""
        sentence = (sentence or "").strip()
        if not sentence:
            return ("none", None)
        tried = []
        for eng in self._chain(engine):
            tried.append(eng)
            res = self._one(sentence, eng)
            if res[0] != "none":
                if eng != engine:
                    self._log("INFO", "synth_fallback", {"asked": engine, "used": eng})
                return res
        if not self._warned_none:
            self._warned_none = True
            self._log("WARNING", "no_synth_engine", {"tried": tried})
        print(f"[cnps] (озвучка недоступна) {sentence}")
        return ("none", None)
