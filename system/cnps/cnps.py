#!/usr/bin/env python3
"""
cnps.py — CNPS, Centralized Notification/Playback System (v1).

Единственный процесс SMOS, владеющий колонками. Заменяет заглушку
system/audio/ v1. Полный разбор — system/cnps/cnps_design.md; здесь —
только то, что нужно, чтобы читать код.

Что делает демон:
- Приёмник поллит tasks/ (заявки <id>.json, атомарные temp+rename).
  Заявка минимальна: {id, kind, source, text|wav_path|clip_key}.
- Аналитический слой (classify.py): класс важности определяет CNPS по
  `source`, запрошенный класс зажимает по потолку источника — заявке
  доверять нельзя.
- Синтез (synth.py) по предложениям, с кэшем WAV по хешу текста.
- Одна звуковая дорожка за раз. Более важная заявка ВЫТЕСНЯЕТ текущую
  (kill проигрывателя, текущая -> «отложена»). Микшера/приглушения в v1
  нет.
- Система времени жизни: два порога. soft — прерванная сама доиграет,
  пока не вышел; hard — дальше только по явной просьбе. Истёкшее уходит
  в буфер Recap (repeat/continue).
- Управление по UDP :47120 (stop/pause/resume/skip/repeat/continue/
  louder/quieter) — мимо очереди. Штатный отправитель — модуль
  system/modules/mod_playback (по команде пользователя через ядро).
- Пока играет — держит flags/mute.flag (глушение микрофона, анти-петля
  v1.1.1), режим «пульс».
- Квитанции о доставке (DELIVERED / PARTIAL / NEVER) в шину логов и в
  state.json.

НЕ в v1 (см. дизайн): голосовой barge-in (это v2 — стоп-споттер в
wake.py), приглушение фоном, кроссфейд.

Треды: intake (поллит tasks/), conductor (выбор + проигрывание по
предложениям), control (UDP), housekeeping (state.json + пульс mute.flag).
Общее состояние под self.lock.

Запуск:
    python3 system/cnps/cnps.py

Проверка вручную:
    echo '{"id":"t1","kind":"speech","source":"outputstructurizer","text":"Проверка связи. Это второе предложение."}' > system/cnps/tasks/t1.json
    echo '{"op":"stop"}' | nc -u -w0 127.0.0.1 47120
"""

import collections
import json
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import classify  # noqa: E402
from classify import class_rank  # noqa: E402
from synth import Synth, split_sentences  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# Заявка в работе
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    kind: str                       # speech | file | clip
    source: str
    cls: str                        # класс важности (CRITICAL..AMBIENT)
    params: dict                    # параметры класса (ttl_*, voice_interruptible, preemptible)
    created_at: float
    sentences: list[str]            # предложения (speech) ИЛИ [путь_к_wav] (file/clip)
    is_file: bool = False
    goal: str | None = None
    intent: str | None = None
    reply_to: str | None = None
    trace_id: str = ""              # сквозной id реплики (см. logs/PROTOCOL.md) — CNPS его финальное звено
    full_text: str = ""
    engine: str = "piper"           # движок синтеза для speech (gtts|piper|spd-say)

    sentence_idx: int = 0           # следующее предложение к проигрыванию
    state: str = "queued"           # queued | playing | deferred | recap
    started_at: float | None = None
    bypass_ttl: bool = False        # True для повторов (repeat/continue) — TTL их не косит
    recap_outcome: str | None = None

    expires_soft: float = 0.0
    expires_hard: float = 0.0

    def set_expiry(self) -> None:
        self.expires_soft = self.created_at + float(self.params.get("ttl_soft_sec", 60))
        self.expires_hard = self.created_at + float(self.params.get("ttl_hard_sec", 900))

    def replay_clone(self, from_start: bool) -> "Task":
        t = Task(
            id=f"{self.id}~r{uuid.uuid4().hex[:4]}",
            kind=self.kind, source=self.source, cls=self.cls, params=dict(self.params),
            created_at=time.time(), sentences=list(self.sentences), is_file=self.is_file,
            goal=self.goal, intent=self.intent, reply_to=self.reply_to, trace_id=self.trace_id,
            full_text=self.full_text, engine=self.engine,
        )
        t.sentence_idx = 0 if from_start else min(self.sentence_idx, max(0, len(self.sentences) - 1))
        t.bypass_ttl = True
        t.set_expiry()
        return t


# --------------------------------------------------------------------------
# Демон
# --------------------------------------------------------------------------

class CNPS:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.check_interval = float(cfg.get("check_interval_sec", 0.3))
        self.play_timeout = float(cfg.get("play_timeout_sec", 30))
        self.players = [list(c) for c in cfg.get("players", []) if c]
        self.volume = float(cfg.get("volume", 1.0))
        self.volume_step = float(cfg.get("volume_step", 0.15))

        self.tasks_dir = (SCRIPT_DIR / cfg["paths"]["tasks_dir"]).resolve()
        self.rejected_dir = self.tasks_dir / cfg["paths"]["rejected_subdir"]
        self.state_file = (SCRIPT_DIR / cfg["paths"]["state_file"]).resolve()
        self.sysaudio_clips = (SCRIPT_DIR / cfg["paths"]["sysaudio_clips_dir"] / cfg.get("sysaudio_lang", "ru")).resolve()

        self.control_host = cfg.get("control_udp_host", "127.0.0.1")
        self.control_port = int(cfg.get("control_udp_port", 47120))

        self.recap = collections.deque(maxlen=int(cfg.get("recap_size", 10)))
        self.synth = Synth(cfg, SCRIPT_DIR, log=self._log)

        root = config.project_root(SCRIPT_DIR)
        self.mute_file = (root / cfg["mute"]["flag_path"]) if root else None
        self.mute_margin = float(cfg["mute"]["until_margin_sec"])
        self.mute_refresh = float(cfg["mute"]["refresh_interval_sec"])
        self._mute_active = False

        self.lock = threading.RLock()
        self.tasks: list[Task] = []
        self.current: Task | None = None
        self.player_proc: subprocess.Popen | None = None
        self._kill_reason: str | None = None    # None|preempt|pause|stop|skip
        self.paused = False
        self._seen_ids: set[str] = set()
        self._warned_no_player = False
        self.running = True

    # ---- логирование --------------------------------------------------

    def _log(self, level: str, message: str, data: dict | None = None,
             trace_id: str | None = None) -> None:
        send_log(level, message, data, trace_id=trace_id)

    # ---- приём заявок (intake thread) --------------------------------

    def _reject(self, path: Path, reason: str) -> None:
        self.rejected_dir.mkdir(parents=True, exist_ok=True)
        dest = self.rejected_dir / f"{path.stem}_{uuid.uuid4().hex[:8]}.json"
        try:
            path.replace(dest)
        except OSError:
            path.unlink(missing_ok=True)
        self._log("WARNING", "task_rejected", {"file": path.name, "reason": reason})
        print(f"[cnps] отклонил {path.name}: {reason}")

    def _created_at(self, manifest: dict, path: Path) -> float:
        v = manifest.get("created_at")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v).timestamp()
            except ValueError:
                pass
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()

    def _build_task(self, manifest: dict, path: Path) -> Task | None:
        kind = manifest.get("kind")
        if not isinstance(kind, str) or not kind:
            kind = "speech" if isinstance(manifest.get("text"), str) else ""
        if kind not in ("speech", "file", "clip"):
            self._reject(path, f"неизвестный kind {kind!r}")
            return None

        cls, params, info = classify.resolve_class(manifest, self.cfg)
        tid = manifest.get("id") or manifest.get("task_id") or _new_id()
        # Движок синтеза: поле манифеста voice/engine -> engine_by_source ->
        # общий дефолт. Для file/clip синтеза нет, но поле безвредно.
        engine = self.synth.resolve_engine(
            manifest.get("voice") or manifest.get("engine"), info["source"] or "")
        common = dict(
            id=str(tid), kind=kind, source=info["source"] or "", cls=cls, params=params,
            created_at=self._created_at(manifest, path), engine=engine,
            goal=manifest.get("goal"),
            intent=manifest.get("intent") or manifest.get("task_id"),
            reply_to=manifest.get("reply_to"),
            trace_id=manifest.get("trace_id") or "",
        )

        if kind == "speech":
            text = manifest.get("text")
            if not (isinstance(text, str) and text.strip()):
                self._reject(path, "нет непустого поля text")
                return None
            sents = split_sentences(text)
            task = Task(sentences=sents, is_file=False, full_text=" ".join(sents), **common)
        elif kind == "file":
            wp = manifest.get("wav_path")
            if not (isinstance(wp, str) and wp):
                self._reject(path, "kind=file без wav_path")
                return None
            wav = Path(wp)
            if not wav.is_absolute():
                root = config.project_root(SCRIPT_DIR)
                wav = (root / wp) if root else (SCRIPT_DIR / wp)
            if not wav.is_file():
                self._reject(path, f"wav_path не найден: {wav}")
                return None
            task = Task(sentences=[str(wav)], is_file=True, full_text=f"(файл {wav.name})", **common)
        else:  # clip
            key = manifest.get("clip_key")
            if not (isinstance(key, str) and key):
                self._reject(path, "kind=clip без clip_key")
                return None
            clip = self.sysaudio_clips / f"{key}.wav"
            if not clip.is_file():
                self._reject(path, f"клип не найден: {clip}")
                return None
            task = Task(sentences=[str(clip)], is_file=True, full_text=f"(клип {key})", **common)

        task.set_expiry()
        self._log("INFO", "task_accepted", {
            "id": task.id, "kind": kind, "class": cls, "source": task.source or None,
            "engine": engine, "sentences": len(task.sentences), "clamped": info["clamped"],
            "requested": info["requested"], "ceiling": info["ceiling"],
        }, trace_id=task.trace_id or None)
        return task

    def _control_from_manifest(self, manifest: dict) -> dict:
        return {k: manifest[k] for k in ("op", "amount", "target", "source") if k in manifest}

    def _scan_tasks(self) -> None:
        if not self.tasks_dir.exists():
            return
        for path in sorted(p for p in self.tasks_dir.glob("*.json") if p.is_file()):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                self._reject(path, f"битый JSON ({e})")
                continue
            if not isinstance(manifest, dict):
                self._reject(path, "не JSON-объект")
                continue

            if manifest.get("kind") == "control" or ("op" in manifest and "kind" not in manifest and "text" not in manifest):
                frame = self._control_from_manifest(manifest)
                path.unlink(missing_ok=True)
                if frame.get("op"):
                    with self.lock:
                        self._apply_control(frame, origin="file")
                else:
                    self._log("WARNING", "control_task_no_op", {"file": path.name})
                continue

            task = self._build_task(manifest, path)
            path.unlink(missing_ok=True)
            if task is None:
                continue
            with self.lock:
                if task.id in self._seen_ids:
                    self._log("DEBUG", "duplicate_task_skipped", {"id": task.id})
                    continue
                self._seen_ids.add(task.id)
                self.tasks.append(task)
                outranks = (
                    self.current is not None
                    and class_rank(task.cls) < class_rank(self.current.cls)
                    and self.current.params.get("preemptible", True)
                )
                if outranks:
                    self._kill_player("preempt")  # заставить conductor уйти на границу сейчас

    def _intake_loop(self) -> None:
        while self.running:
            try:
                self._scan_tasks()
            except Exception as e:  # noqa: BLE001 — приёмник не должен падать
                self._log("ERROR", "intake_error", {"error": str(e)})
            time.sleep(self.check_interval)

    # ---- выбор и проигрывание (conductor thread) --------------------

    def _pick_candidate(self) -> Task | None:
        now = time.time()
        eligible = []
        for t in self.tasks:
            if t.state == "playing":
                continue
            if not t.bypass_ttl and now > t.expires_hard:
                continue
            if t.state == "deferred" and not t.bypass_ttl and now > t.expires_soft:
                continue
            eligible.append(t)
        if not eligible:
            return None
        eligible.sort(key=lambda t: (class_rank(t.cls), t.created_at))
        return eligible[0]

    def _ttl_sweep(self) -> None:
        now = time.time()
        for t in list(self.tasks):
            if t.state == "playing" or t.bypass_ttl:
                continue
            if now > t.expires_hard:
                outcome = "NEVER" if (t.sentence_idx == 0 and t.state == "queued") else "PARTIAL"
                self._finish(t, outcome, swept=True)
            elif t.state == "deferred" and now > t.expires_soft:
                self._finish(t, "PARTIAL", swept=True)

    def _activate(self, task: Task) -> None:
        self.current = task
        task.state = "playing"
        if task.started_at is None:
            task.started_at = time.time()

    def _select(self) -> None:
        """Решает, что должно играть. Вызывается conductor'ом под lock в
        начале каждой итерации."""
        self._ttl_sweep()
        cand = self._pick_candidate()
        cur = self.current
        if cur is None:
            if cand is not None:
                self._activate(cand)
            return
        if cand is None or cand is cur:
            return
        if class_rank(cand.cls) < class_rank(cur.cls) and cur.params.get("preemptible", True):
            cur.state = "deferred"   # sentence_idx уже указывает на точку возобновления
            self._log("INFO", "preempted", {"held": cur.id, "by": cand.id, "at_sentence": cur.sentence_idx},
                      trace_id=cur.trace_id or None)
            self._activate(cand)

    def _finish(self, task: Task, outcome: str, swept: bool = False) -> None:
        if task in self.tasks:
            self.tasks.remove(task)
        task.state = "recap"
        task.recap_outcome = outcome
        self.recap.append(task)
        if self.current is task:
            self.current = None
        self._log("INFO", "delivery", {
            "id": task.id, "outcome": outcome, "class": task.cls,
            "goal": task.goal, "intent": task.intent,
            "played_sentences": task.sentence_idx, "total": len(task.sentences),
            "swept": swept,
        }, trace_id=task.trace_id or None)
        print(f"[cnps] {outcome} {task.id} ({task.cls}) {task.sentence_idx}/{len(task.sentences)}")

    def _prepare(self, task: Task, idx: int):
        """Готовит источник звука для предложения idx. Может звать синтез
        (Piper) и сеть (gtts) — поэтому вызывается БЕЗ lock. Возвращает
        ("wav", путь) | ("mp3", путь) | ("cmd", [команда]) | ("none", None)."""
        if task.is_file:
            p = task.sentences[idx]
            return ("mp3" if p.lower().endswith(".mp3") else "wav", p)
        return self.synth.prepare_sentence(task.sentences[idx], task.engine)

    def _wav_player(self, volume: float) -> list[str] | None:
        for cand in self.players:
            if cand and shutil.which(cand[0]):
                if cand[0] == "paplay" and volume < 0.999:
                    vol = max(0, min(65536, int(volume * 65536)))
                    return [*cand, f"--volume={vol}"]
                return list(cand)
        return None

    def _spawn(self, kind: str, payload) -> subprocess.Popen | None:
        if kind == "wav":
            player = self._wav_player(self.volume)
            if player is None:
                if not self._warned_no_player:
                    self._warned_no_player = True
                    self._log("WARNING", "no_wav_player", {"tried": [p[0] for p in self.players]})
                return None
            cmd = [*player, payload]
        elif kind == "mp3":
            player = self.synth.mp3_player()
            if player is None:
                if not self._warned_no_player:
                    self._warned_no_player = True
                    self._log("WARNING", "no_mp3_player", {})
                return None
            cmd = [*player, payload]
        else:
            cmd = payload
        try:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._log("WARNING", "player_spawn_failed", {"error": str(e)})
            return None

    def _conductor_loop(self) -> None:
        while self.running:
            with self.lock:
                self._select()
                task = self.current
                paused = self.paused
                enabled = self.enabled

            if task is None:
                time.sleep(self.check_interval)
                continue
            if paused:
                with self.lock:
                    self._clear_mute()   # на паузе звука нет — микрофон можно вернуть
                time.sleep(0.1)
                continue
            if not enabled:
                with self.lock:
                    self._log("INFO", "playback_disabled", {"id": task.id, "text": task.full_text[:120]})
                    self._finish(task, "DELIVERED")
                print(f"[cnps] (выключено) {task.full_text}")
                continue

            idx = task.sentence_idx
            if idx >= len(task.sentences):
                with self.lock:
                    self._finish(task, "DELIVERED")
                continue

            kind, payload = self._prepare(task, idx)
            if kind == "none":
                with self.lock:
                    if task is self.current and idx == task.sentence_idx:
                        task.sentence_idx += 1
                        if task.sentence_idx >= len(task.sentences):
                            self._finish(task, "DELIVERED")
                continue

            with self.lock:
                # Пока шёл _prepare (синтез/сеть — вне lock, секунды),
                # мог прийти запрос на прерывание: intake бросил
                # _kill_player("preempt") для более важной заявки, но
                # проигрывателя ещё не было — kill пришёлся в пустоту, а
                # _kill_reason остался. Не запускаем это предложение:
                # чистим флаг и уходим на новую итерацию, где _select
                # разведёт приоритеты. (Аналогично для stop/pause/skip,
                # прилетевших во время _prepare.)
                if self._kill_reason is not None:
                    self._kill_reason = None
                    proc = None
                else:
                    ok = (task is self.current and not self.paused
                          and idx == task.sentence_idx and self.enabled)
                    proc = self._spawn(kind, payload) if ok else None
                    if proc is not None:
                        self.player_proc = proc
                        self._refresh_mute()
            if proc is None:
                time.sleep(0.02)
                continue

            start = time.time()
            last_mute = start
            while proc.poll() is None:
                time.sleep(0.05)
                nowt = time.time()
                if nowt - last_mute >= self.mute_refresh:
                    with self.lock:
                        if self.player_proc is proc:
                            self._refresh_mute()
                    last_mute = nowt
                if nowt - start > self.play_timeout:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    self._log("WARNING", "play_timeout", {"id": task.id, "sentence": idx})
                    break
            rc = proc.poll()

            with self.lock:
                if self.player_proc is proc:
                    self.player_proc = None
                reason = self._kill_reason
                self._kill_reason = None
                if task is self.current and not self.paused:
                    if reason is None:
                        if rc not in (0, None):
                            self._log("WARNING", "play_nonzero_exit", {"id": task.id, "rc": rc, "sentence": idx})
                        task.sentence_idx += 1
                        if task.sentence_idx >= len(task.sentences):
                            self._finish(task, "DELIVERED")
                    elif reason == "skip":
                        task.sentence_idx += 1
                        if task.sentence_idx >= len(task.sentences):
                            self._finish(task, "DELIVERED")
                    # preempt / pause / stop -> sentence_idx не трогаем

    # ---- управление (control thread + заявки kind=control) ---------

    def _kill_player(self, reason: str) -> None:
        """Останавливает текущий проигрыватель. Вызывать под self.lock.
        reason: preempt|pause|stop|skip — conductor по нему решает,
        двигать ли sentence_idx."""
        self._kill_reason = reason
        p = self.player_proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _enqueue_replay(self, from_start: bool) -> None:
        """repeat -> проиграть заново с начала; continue -> с начала
        прерванного предложения. Источник: сперва отложенная заявка,
        ещё живущая в self.tasks (continue), иначе — из Recap."""
        if not from_start:
            for t in reversed(self.tasks):
                if t.state == "deferred" and 0 < t.sentence_idx < len(t.sentences):
                    t.bypass_ttl = True
                    self._log("INFO", "replay", {"mode": "continue", "reuse": t.id, "from_sentence": t.sentence_idx})
                    return

        src = None
        for t in reversed(self.recap):
            if not t.sentences:
                continue
            if from_start or (0 < t.sentence_idx < len(t.sentences)):
                src = t
                break
        if src is None:  # для continue без «частичного» — берём последнее и с начала
            for t in reversed(self.recap):
                if t.sentences:
                    src = t
                    break
            from_start = True
        if src is None:
            self._log("INFO", "replay_empty", {"mode": "start" if from_start else "continue"})
            print("[cnps] нечего повторять")
            return

        clone = src.replay_clone(from_start=from_start)
        self._seen_ids.add(clone.id)
        self.tasks.append(clone)
        self._log("INFO", "replay", {
            "mode": "start" if from_start else "continue",
            "from": src.id, "as": clone.id, "from_sentence": clone.sentence_idx,
        })

    def _apply_control(self, frame: dict, origin: str) -> None:
        """Применяет управляющий фрейм. Вызывать под self.lock."""
        op = frame.get("op")
        cur = self.current

        if op in ("stop", "pause", "skip") and cur is not None \
                and not cur.params.get("voice_interruptible", True):
            self._log("INFO", "control_ignored", {"op": op, "class": cur.cls, "origin": origin})
            return

        if op == "stop":
            if cur is not None:
                self._kill_player("stop")
                self._finish(cur, "PARTIAL")
        elif op == "pause":
            if cur is not None and not self.paused:
                self.paused = True
                self._kill_player("pause")
        elif op == "resume":
            self.paused = False
        elif op == "skip":
            if cur is not None:
                self._kill_player("skip")
        elif op == "repeat":
            self._enqueue_replay(from_start=True)
        elif op == "continue":
            self._enqueue_replay(from_start=False)
        elif op in ("louder", "quieter", "set_volume"):
            amt = frame.get("amount")
            if op == "set_volume":
                self.volume = max(0.0, min(1.0, float(amt) if isinstance(amt, (int, float)) else self.volume))
            else:
                step = float(amt) if isinstance(amt, (int, float)) else self.volume_step
                self.volume = max(0.0, min(1.0, self.volume + (step if op == "louder" else -step)))
        else:
            self._log("WARNING", "control_unknown_op", {"op": op, "origin": origin})
            return

        self._log("INFO", "control", {"op": op, "origin": origin, "volume": round(self.volume, 2),
                                      "paused": self.paused})

    def _control_loop(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.control_host, self.control_port))
            sock.settimeout(0.5)
        except OSError as e:
            self._log("ERROR", "control_bind_failed", {"port": self.control_port, "error": str(e)})
            print(f"[cnps] UDP-канал управления НЕ поднялся ({self.control_port}: {e}) — "
                  f"воспроизведение работает, управление недоступно")
            return
        print(f"[cnps] управление на udp://{self.control_host}:{self.control_port}")
        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                frame = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._log("WARNING", "control_bad_frame", {"from": addr[0]})
                continue
            if not isinstance(frame, dict) or not frame.get("op"):
                self._log("WARNING", "control_no_op", {"from": addr[0]})
                continue
            with self.lock:
                self._apply_control(frame, origin=f"udp:{addr[0]}")
        try:
            sock.close()
        except OSError:
            pass

    # ---- housekeeping: state.json + пульс mute.flag ---------------

    def _refresh_mute(self) -> None:
        if self.mute_file is None:
            return
        payload = {"until": time.time() + self.mute_margin, "by": "cnps"}
        try:
            self.mute_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.mute_file.with_name(self.mute_file.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.mute_file)
            self._mute_active = True
        except OSError as e:
            self._log("WARNING", "mute_flag_write_failed", {"error": str(e)})

    def _clear_mute(self) -> None:
        if self.mute_file is None or not self._mute_active:
            return
        try:
            self.mute_file.unlink(missing_ok=True)
        except OSError:
            pass
        self._mute_active = False

    def _state_snapshot(self) -> dict:
        def brief(t: Task) -> dict:
            return {"id": t.id, "class": t.cls, "source": t.source or None,
                    "sentence_idx": t.sentence_idx, "sentences": len(t.sentences)}

        cur = self.current
        return {
            "updated_at": now_iso(),
            "enabled": self.enabled,
            "paused": self.paused,
            "volume": round(self.volume, 2),
            "playing": ({**brief(cur), "goal": cur.goal, "intent": cur.intent,
                         "text": cur.full_text[:200], "started_at": cur.started_at} if cur else None),
            "queued": [brief(t) for t in self.tasks if t.state == "queued"],
            "deferred": [brief(t) for t in self.tasks if t.state == "deferred"],
            "recap": [{"id": t.id, "outcome": t.recap_outcome, "goal": t.goal,
                       "text": t.full_text[:120]} for t in list(self.recap)],
            "counts": {"tasks": len(self.tasks), "recap": len(self.recap)},
        }

    def _write_state(self, snap: dict) -> None:
        try:
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as e:
            self._log("WARNING", "state_write_failed", {"error": str(e)})

    def _housekeeping_loop(self) -> None:
        while self.running:
            with self.lock:
                snap = self._state_snapshot()
                idle = self.player_proc is None and (self.current is None or self.paused)
            self._write_state(snap)
            if idle:
                self._clear_mute()
            time.sleep(self.check_interval)

    # ---- жизненный цикл -------------------------------------------

    def start(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        print(f"[cnps] запущен. Заявки: {self.tasks_dir}")
        print(f"[cnps] синтез: {self.synth.engine_status()}")
        print(f"[cnps] движок по источнику: {self.synth.engine_by_source or '{}'}, дефолт {self.synth.default_engine}")
        wp = self._wav_player(1.0)
        mp = self.synth.mp3_player()
        print(f"[cnps] проигрыватель WAV: {' '.join(wp) if wp else 'НЕ НАЙДЕН'} | MP3: {' '.join(mp) if mp else 'НЕ НАЙДЕН'}")
        if self.mute_file is not None:
            print(f"[cnps] на время воспроизведения глушу микрофон: {self.mute_file}")
        else:
            print("[cnps] корень проекта не найден — глушение микрофона ОТКЛЮЧЕНО")
        if not self.enabled:
            print("[cnps] enabled=false — заявки принимаю и учитываю, но не озвучиваю")

        self._log("INFO", "cnps_started", {
            "enabled": self.enabled, "synth_default": self.synth.default_engine,
            "engine_by_source": self.synth.engine_by_source,
            "control_port": self.control_port,
            "mute_flag": str(self.mute_file) if self.mute_file else None,
        })

        threads = [
            threading.Thread(target=self._intake_loop, name="cnps-intake", daemon=True),
            threading.Thread(target=self._conductor_loop, name="cnps-conductor", daemon=True),
            threading.Thread(target=self._control_loop, name="cnps-control", daemon=True),
            threading.Thread(target=self._housekeeping_loop, name="cnps-housekeeping", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while self.running:
                time.sleep(0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        with self.lock:
            self._kill_player("stop")
            self._clear_mute()
        self._log("INFO", "cnps_stopped")
        print("\n[cnps] остановлен")


def main() -> None:
    cnps = CNPS(CFG)

    def _sigterm(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)
    try:
        cnps.start()
    except KeyboardInterrupt:
        cnps.stop()


if __name__ == "__main__":
    main()
