#!/usr/bin/env python3
"""
analytics.py — аналитический слой поверх сырых логов SMOS.

Читает logs/raw/<module>/events.jsonl (их пишет logs/listener) и НИЧЕГО
больше — с кодом модулей не связан, как и задумано (см. PROTOCOL.md,
project-заметка про логи). Отвечает на вопрос, который логи копили всё
это время: где система теряет команды, что тормозит, что ломается, чего
пользователь просит.

Подкоманды:
    analytics.py report [--since ISO | --last 24h] [--json]
        Полный отчёт за окно: воронка (докуда доходят реплики и на чём
        отваливаются), разбор задержки по стадиям, аномалии, поведение.
    analytics.py trace <trace_id>
        Таймлайн одной реплики: строка на событие, Δt от предыдущего.
    analytics.py traces [--last 24h] [-n 20]
        Последние реплики: докуда дошли, чем закончились, сколько заняли.
    analytics.py watch
        Демон: раз в poll_interval_sec перечитывает свежее окно, шлёт
        найденные аномалии обратно в шину логов (module="analytics") и
        печатает компактную сводку. Это зерно будущего watcher'а —
        в smos.py PROCESSES по умолчанию НЕ добавлен (только чтение,
        включается вручную).

trace_id по всей цепочке появился отдельным изменением
(logs/PROTOCOL.md). События старше него trace_id не имеют — воронка и
задержка считаются только по тем репликам, где он есть; аномалии и
поведение — по всем событиям окна.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import pipeline  # noqa: E402
from log_client import send_log  # noqa: E402
from store import TraceStore  # noqa: E402

CFG = config.load(SCRIPT_DIR)

LOGS_DIR = SCRIPT_DIR.parent               # папка logs/
RAW_DIR = LOGS_DIR / CFG["paths"]["raw_dir"]

REACHED = pipeline.reached_lookup()
FAILED = pipeline.failed_lookup()
LAST_STAGE_IDX = len(pipeline.STAGES) - 1


# ─────────────────────────── утилиты ────────────────────────────────

def now_aware() -> datetime:
    return datetime.now().astimezone()


def parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def parse_since(spec: str | None, default_hours: float) -> datetime:
    """--since ISO  |  --last 90m/6h/2d  |  ничего -> config default."""
    if not spec:
        return now_aware() - timedelta(hours=default_hours)
    s = spec.strip().lower()
    if s and s[-1] in "smhd" and s[:-1].replace(".", "", 1).isdigit():
        n = float(s[:-1])
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[s[-1]]
        return now_aware() - timedelta(seconds=n * mult)
    dt = parse_ts(spec)
    if dt is None:
        raise SystemExit(f"[analytics] не понял --since {spec!r} (нужно ISO-время или вида 24h/90m/2d)")
    return dt


def pct(values: list[float], p: float):
    if not values:
        return None
    v = sorted(values)
    idx = min(len(v) - 1, max(0, int(round((p / 100) * (len(v) - 1)))))
    return v[idx]


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "  —  "
    if sec < 10:
        return f"{sec:4.1f}s"
    if sec < 600:
        return f"{sec:4.0f}s"
    return f"{sec / 60:4.0f}m"


def bar(n: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return ""
    filled = int(round(width * n / total))
    return "█" * filled + "·" * (width - filled)


# ─────────────────────────── загрузка ───────────────────────────────

def load_events(since: datetime) -> list[dict]:
    """Все события из logs/raw/*/events.jsonl не старше since, по времени."""
    if not RAW_DIR.exists():
        raise SystemExit(f"[analytics] нет папки с логами: {RAW_DIR}")
    events: list[dict] = []
    for events_file in sorted(RAW_DIR.glob("*/events.jsonl")):
        try:
            lines = events_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            dt = parse_ts(ev.get("ts", ""))
            if dt is None or dt < since:
                continue
            ev["_dt"] = dt
            events.append(ev)
    events.sort(key=lambda e: e["_dt"])
    return events


def by_trace(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        tid = ev.get("trace_id")
        if tid:
            out[tid].append(ev)
    return out


# ─────────────────────────── разбор реплики ─────────────────────────

class TraceView:
    """Что случилось с одной репликой (по её событиям)."""

    def __init__(self, trace_id: str, events: list[dict]):
        self.trace_id = trace_id
        self.events = events
        self.stage_ts: dict[str, datetime] = {}
        self.furthest = -1
        self.failure: tuple[str, int] | None = None   # (причина, индекс стадии)
        self.label: str | None = None
        self.goal: str | None = None
        self.outcome: str | None = None               # DELIVERED | PARTIAL | NEVER
        self.silent = False

        for ev in events:
            key = (ev.get("module"), ev.get("message"))
            data = ev.get("data") or {}

            si = REACHED.get(key)
            if si is not None:
                sk = pipeline.STAGE_KEYS[si]
                self.stage_ts.setdefault(sk, ev["_dt"])
                self.furthest = max(self.furthest, si)

            fi = FAILED.get(key)
            if fi is not None:
                idx, reason = fi
                # держим провал самой поздней достигнутой стадии
                if self.failure is None or idx >= self.failure[1]:
                    self.failure = (reason, idx)

            if key == ("classifier", "phrase_classified"):
                self.label = data.get("label")
            elif key == ("swl", "intent_extracted"):
                self.goal = data.get("goal")
            elif key == ("core", "goal_dispatched"):
                self.goal = self.goal or data.get("goal")
            elif key == ("core", "completion_silent"):
                self.silent = True
            elif key == ("cnps", "delivery"):
                self.outcome = data.get("outcome")

    @property
    def furthest_key(self) -> str:
        return pipeline.STAGE_KEYS[self.furthest] if self.furthest >= 0 else "—"

    @property
    def spoken_ok(self) -> bool:
        return self.outcome in ("DELIVERED", "PARTIAL")

    @property
    def is_chat(self) -> bool:
        return self.label == "chat"

    @property
    def status(self) -> str:
        if self.spoken_ok:
            return "ok" if self.outcome == "DELIVERED" else "partial"
        if self.is_chat:
            return "chat"
        if self.silent and self.furthest >= pipeline.STAGE_INDEX["executed"]:
            return "silent-ok"
        if self.failure is not None:
            return "failed"
        return "dropped"

    def total_latency(self):
        """Полное время: услышана → озвучен ответ."""
        a = self.stage_ts.get("heard")
        b = self.stage_ts.get("spoken")
        return (b - a).total_seconds() if a and b else None

    def exec_latency(self):
        """Время выполнения БЕЗ озвучки: услышана → готов ответ
        (phrase_emitted). Отбрасывает последний участок phrased→spoken
        (синтез речи + воспроизведение CNPS). Если ответ не формулировался
        (silent / оборвалось раньше) — услышана → самая поздняя
        достигнутая стадия, кроме spoken."""
        a = self.stage_ts.get("heard")
        if not a:
            return None
        ends = [ts for k, ts in self.stage_ts.items() if k != "spoken"]
        if not ends:
            return None
        b = max(ends)
        return (b - a).total_seconds() if b >= a else None

    def gap_latencies(self) -> dict:
        out = {}
        for a, b in pipeline.GAPS:
            ta, tb = self.stage_ts.get(a), self.stage_ts.get(b)
            if ta and tb and tb >= ta:
                out[f"{a}->{b}"] = round((tb - ta).total_seconds(), 3)
        tot = self.total_latency()
        out["heard->spoken"] = round(tot, 3) if tot is not None else None
        ex = self.exec_latency()
        out["heard->phrased"] = round(ex, 3) if ex is not None else None
        return out

    def to_record(self) -> dict:
        """Собранная история одной реплики — для постоянного хранилища
        (store.py) и для 'analytics.py trace'."""
        first = self.stage_ts.get("heard") or (self.events[0]["_dt"] if self.events else now_aware())
        last = self.events[-1]["_dt"] if self.events else first
        return {
            "trace_id": self.trace_id,
            "started_at": first.isoformat(timespec="seconds"),
            "ended_at": last.isoformat(timespec="seconds"),
            "furthest": self.furthest_key,
            "status": self.status,
            "label": self.label,
            "goal": self.goal,
            "outcome": self.outcome,
            "silent": self.silent,
            "failure": self.failure[0] if self.failure else None,
            "total_sec": round(self.total_latency(), 3) if self.total_latency() is not None else None,
            "exec_sec": round(self.exec_latency(), 3) if self.exec_latency() is not None else None,
            "latency_sec": self.gap_latencies(),
            "events": [
                {"ts": e["_dt"].isoformat(timespec="seconds"),
                 "module": e.get("module"), "message": e.get("message"),
                 "level": e.get("level"), "data": e.get("data") or {}}
                for e in self.events
            ],
        }


# ─────────────────────────── воронка ────────────────────────────────

def funnel(traces: list[TraceView]) -> dict:
    n = len(traces)
    reached_at_least = [0] * len(pipeline.STAGES)
    for t in traces:
        for i in range(len(pipeline.STAGES)):
            if t.furthest >= i:
                reached_at_least[i] += 1

    # причины отвала в каждом межстадийном промежутке (по репликам,
    # застрявшим ровно на стадии i)
    drop_reasons: dict[int, Counter] = defaultdict(Counter)
    chat_exit = 0
    for t in traces:
        if t.is_chat and not t.spoken_ok:
            chat_exit += 1
            continue
        if t.spoken_ok or t.furthest >= LAST_STAGE_IDX:
            continue
        if t.silent and t.furthest >= pipeline.STAGE_INDEX["executed"]:
            continue
        i = t.furthest if t.furthest >= 0 else 0
        reason = t.failure[0] if t.failure else "оборвалась без явной ошибки"
        drop_reasons[i][reason] += 1

    return {
        "n": n,
        "reached": reached_at_least,
        "drop_reasons": {pipeline.STAGE_KEYS[i]: dict(c) for i, c in drop_reasons.items()},
        "chat_exit": chat_exit,
        "spoken_ok": sum(1 for t in traces if t.spoken_ok),
        "spoken_never": sum(1 for t in traces if t.outcome == "NEVER"),
        "silent_ok": sum(1 for t in traces
                         if t.silent and t.furthest >= pipeline.STAGE_INDEX["executed"] and not t.spoken_ok),
    }


def latency(traces: list[TraceView]) -> dict:
    gaps: dict[str, list[float]] = defaultdict(list)
    for t in traces:
        for a, b in pipeline.GAPS:
            ta, tb = t.stage_ts.get(a), t.stage_ts.get(b)
            if ta and tb and tb >= ta:
                gaps[f"{a}->{b}"].append((tb - ta).total_seconds())
        tot = t.total_latency()
        if tot is not None and tot >= 0:
            gaps["heard->spoken"].append(tot)
        ex = t.exec_latency()
        if ex is not None and ex >= 0:
            gaps["heard->phrased"].append(ex)
    return {
        k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": max(v)}
        for k, v in gaps.items()
    }


# ─────────────────────────── аномалии ───────────────────────────────

def anomalies(events: list[dict], since: datetime) -> list[dict]:
    a = CFG["anomaly"]
    span_h = max(0.01, (now_aware() - since).total_seconds() / 3600)
    out: list[dict] = []

    # ошибки по модулям
    err = Counter()
    err_samples: dict[str, str] = {}
    for ev in events:
        if ev.get("level") in ("ERROR", "CRITICAL"):
            m = ev.get("module", "?")
            err[m] += 1
            err_samples.setdefault(m, ev.get("message", ""))
    for m, c in err.most_common():
        per_h = c / span_h
        lvl = "WARNING" if per_h >= a["error_spike_per_hour"] else "INFO"
        out.append({"key": f"errors:{m}", "level": lvl,
                    "title": f"{m}: {c} ошибок за окно ({per_h:.1f}/час)",
                    "detail": f"первая: {err_samples.get(m, '')}"})

    # перезапуски системы (хлопанье). Один реальный запуск = одно
    # smos/all_system_started; если smos.py не использовался — падаем на
    # core_started (тоже один на запуск ядра). НЕ считаем cnps/logs/...
    # _started, иначе один нормальный старт выглядит как пачка.
    start_key = ("smos", "all_system_started")
    starts = [ev["_dt"] for ev in events if (ev.get("module"), ev.get("message")) == start_key]
    if not starts:
        starts = [ev["_dt"] for ev in events if (ev.get("module"), ev.get("message")) == ("core", "core_started")]
    if len(starts) >= 2:
        win = timedelta(seconds=a["restart_flap_window_sec"])
        flaps = max(
            sum(1 for s in starts if b - win <= s <= b)
            for b in starts
        )
        if flaps >= a["restart_flap_count"]:
            out.append({"key": "restart_flap", "level": "WARNING",
                        "title": f"перезапуски: до {flaps} за {a['restart_flap_window_sec']}с — система хлопает",
                        "detail": f"всего стартовых событий за окно: {len(starts)}"})

    # доля неудачного STT
    ok = sum(1 for ev in events if (ev.get("module"), ev.get("message")) == ("rvs", "speech_recognized"))
    bad = sum(1 for ev in events if (ev.get("module"), ev.get("message"))
              in (("rvs", "speech_not_recognized"), ("rvs", "stt_request_error")))
    if ok + bad >= 5:
        rate = bad / (ok + bad)
        if rate >= a["stt_failure_rate_warn"]:
            out.append({"key": "stt_failure_rate", "level": "WARNING",
                        "title": f"STT: {rate:.0%} попыток без результата ({bad}/{ok + bad})",
                        "detail": "сеть, микрофон или порог энергии wake.py"})

    # CNPS ушёл на запасной движок синтеза (прокси здоровья сети/пакетов)
    fb = [ev for ev in events if (ev.get("module"), ev.get("message")) == ("cnps", "synth_fallback")]
    if fb:
        pairs = Counter((ev.get("data") or {}).get("asked", "?") + "→" + (ev.get("data") or {}).get("used", "?")
                        for ev in fb)
        out.append({"key": "synth_fallback", "level": "WARNING" if len(fb) >= 3 else "INFO",
                    "title": f"CNPS: {len(fb)} раз запасной движок синтеза",
                    "detail": ", ".join(f"{k}×{v}" for k, v in pairs.most_common())})

    # зависшие задачи: task_started без task_done/task_failed
    started: dict[str, datetime] = {}
    finished: set[str] = set()
    for ev in events:
        key = (ev.get("module"), ev.get("message"))
        tid = (ev.get("data") or {}).get("task_id")
        if not tid:
            continue
        if key == ("core", "task_started"):
            started[tid] = ev["_dt"]
        elif key in (("core", "task_done"), ("core", "task_failed")):
            finished.add(tid)
    stale = [(tid, ts) for tid, ts in started.items()
             if tid not in finished and (now_aware() - ts).total_seconds() > a["hang_timeout_sec"]]
    for tid, ts in stale:
        out.append({"key": f"hang:{tid}", "level": "WARNING",
                    "title": f"задача {tid} не завершилась за {a['hang_timeout_sec']}с",
                    "detail": f"старт в {ts.isoformat(timespec='seconds')}"})

    # отклонённые заявки
    rej = Counter()
    for ev in events:
        if ev.get("message") in ("goal_rejected", "task_rejected", "result_rejected"):
            rej[f"{ev.get('module')}:{ev.get('message')}"] += 1
    for k, c in rej.items():
        out.append({"key": f"rejected:{k}", "level": "INFO",
                    "title": f"{k}: {c} отклонённых заявок за окно", "detail": ""})

    # залипший mute.flag (микрофон мог оглохнуть навсегда)
    root = config.project_root(SCRIPT_DIR)
    if root:
        mf = root / CFG["paths"]["mute_flag_from_root"]
        try:
            raw = mf.read_text(encoding="utf-8")
            mtime = mf.stat().st_mtime
            age = time.time() - mtime
            active = False
            try:
                d = json.loads(raw)
                if isinstance(d, dict) and isinstance(d.get("until"), (int, float)):
                    active = time.time() < d["until"]
            except ValueError:
                active = age <= 1.0
            if active and age > a["mute_flag_stuck_sec"]:
                out.append({"key": "mute_stuck", "level": "WARNING",
                            "title": f"mute.flag активен и не обновлялся {age:.0f}с",
                            "detail": "CNPS мог упасть в середине озвучки — wake.py заглушён"})
        except OSError:
            pass  # нет файла — нормально

    return out


# ─────────────────────────── поведение ──────────────────────────────

def behavior(events: list[dict], traces: list[TraceView]) -> dict:
    goals = Counter(t.goal for t in traces if t.goal)
    labels = Counter(t.label for t in traces if t.label)
    hours = Counter(t.stage_ts["heard"].hour for t in traces if "heard" in t.stage_ts)

    no_match = [
        (ev.get("data") or {}).get("text", "")
        for ev in events
        if (ev.get("module"), ev.get("message")) == ("swl", "no_intent_match")
    ]
    phrase_layer = Counter(
        (ev.get("data") or {}).get("source", "?")
        for ev in events
        if (ev.get("module"), ev.get("message")) == ("outputstructurizer", "phrase_emitted")
    )
    clf_source = Counter(
        (ev.get("data") or {}).get("source", "?")
        for ev in events
        if (ev.get("module"), ev.get("message")) == ("classifier", "phrase_classified")
    )
    return {
        "goals": goals.most_common(),
        "labels": dict(labels),
        "hours": dict(sorted(hours.items())),
        "no_intent_match": no_match,
        "phrase_layer": dict(phrase_layer),
        "classifier_source": dict(clf_source),
    }


# ─────────────────────────── печать отчёта ──────────────────────────

def print_report(since: datetime, events: list[dict], as_json: bool) -> None:
    traces = [TraceView(tid, evs) for tid, evs in by_trace(events).items()]
    traces.sort(key=lambda t: t.stage_ts.get("heard") or now_aware())

    fn = funnel(traces)
    lat = latency(traces)
    an = anomalies(events, since)
    bh = behavior(events, traces)

    if as_json:
        print(json.dumps({
            "since": since.isoformat(timespec="seconds"),
            "events": len(events),
            "traces": len(traces),
            "funnel": {
                "n": fn["n"],
                "reached": dict(zip(pipeline.STAGE_KEYS, fn["reached"])),
                "drop_reasons": fn["drop_reasons"],
                "chat_exit": fn["chat_exit"],
                "spoken_ok": fn["spoken_ok"], "spoken_never": fn["spoken_never"],
                "silent_ok": fn["silent_ok"],
            },
            "latency": lat,
            "anomalies": an,
            "behavior": bh,
        }, ensure_ascii=False, indent=2))
        return

    span_h = (now_aware() - since).total_seconds() / 3600
    print(f"\n═══ SMOS analytics ─ окно {span_h:.1f}ч (с {since.isoformat(timespec='seconds')}) ═══")
    print(f"событий: {len(events)}   реплик с trace_id: {len(traces)}")

    # ── воронка
    print("\n── ВОРОНКА (докуда доходит реплика) " + "─" * 30)
    n0 = fn["reached"][0] or 1
    for i, st in enumerate(pipeline.STAGES):
        c = fn["reached"][i]
        line = f"  {st.key:<11} {c:>4}  {bar(c, n0)}  {100 * c / n0:5.1f}%"
        print(line)
        if i < LAST_STAGE_IDX:
            drop = fn["reached"][i] - fn["reached"][i + 1]
            reasons = fn["drop_reasons"].get(st.key, {})
            extra = ""
            if st.key == "classified" and fn["chat_exit"]:
                extra = f"  ({fn['chat_exit']} → chat, легитимный выход)"
            if drop > 0 or reasons:
                rs = ", ".join(f"{k}×{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
                print(f"       ↓ −{drop}{extra}" + (f"   [{rs}]" if rs else ""))
            elif extra:
                print(f"       ↓{extra}")
    print(f"\n  озвучено успешно: {fn['spoken_ok']}   NEVER: {fn['spoken_never']}   "
          f"без озвучки намеренно (silent): {fn['silent_ok']}")

    # ── задержка
    print("\n── ЗАДЕРЖКА по стадиям (сек, p50 / p95 / max, n) " + "─" * 15)
    slow = CFG["latency"]["slow_gap_sec"]
    for gk in pipeline.GAP_KEYS:
        d = lat.get(gk)
        if not d or not d["n"]:
            continue
        mark = "  ← медленно" if (slow.get(gk) is not None and d["p95"] is not None
                                  and d["p95"] >= slow[gk]) else ""
        print(f"  {gk:<22} {fmt_dur(d['p50'])} / {fmt_dur(d['p95'])} / {fmt_dur(d['max'])}   n={d['n']}{mark}")

    # ── аномалии
    print("\n── АНОМАЛИИ / ЗДОРОВЬЕ " + "─" * 40)
    if not an:
        print("  чисто")
    for x in an:
        tag = "⚠ " if x["level"] == "WARNING" else "· "
        print(f"  {tag}{x['title']}" + (f"   — {x['detail']}" if x["detail"] else ""))

    # ── поведение
    print("\n── ПОВЕДЕНИЕ " + "─" * 50)
    if bh["goals"]:
        print("  чаще всего просят:")
        gmax = bh["goals"][0][1]
        for g, c in bh["goals"][:10]:
            print(f"    {g:<24} {c:>3}  {bar(c, gmax, 20)}")
    if bh["labels"]:
        print(f"  команда/чат: {bh['labels']}")
    if bh["classifier_source"]:
        print(f"  классификатор: {bh['classifier_source']}  (llm_api → local = прогресс дистилляции)")
    if bh["phrase_layer"]:
        print(f"  слой формулировки ответа: {bh['phrase_layer']}  (template/llm/fallback)")
    if bh["hours"]:
        hmax = max(bh["hours"].values())
        print("  по часам суток:")
        for h, c in bh["hours"].items():
            print(f"    {h:02d}:00  {c:>3}  {bar(c, hmax, 20)}")
    if bh["no_intent_match"]:
        print(f"  SWL не нашёл цель ({len(bh['no_intent_match'])}) — кандидаты в новые модули:")
        for txt in bh["no_intent_match"][:15]:
            print(f"    · {txt!r}")
    print()


# ─────────────────────────── trace / traces ────────────────────────

def _render_trace_record(rec: dict) -> None:
    """Печатает историю одной реплики из record-словаря (одинаковый вид
    и у store, и у собранного на лету TraceView.to_record())."""
    print(f"\n═══ trace {rec['trace_id']} ═══")
    line = f"докуда дошла: {rec.get('furthest', '—')}   статус: {rec.get('status', '—')}"
    if rec.get("goal"):
        line += f"   цель: {rec['goal']}"
    if rec.get("outcome"):
        line += f"   outcome: {rec['outcome']}"
    if rec.get("failure"):
        line += f"   провал: {rec['failure']}"
    print(line)
    tot = (rec.get("latency_sec") or {}).get("heard->spoken")
    if tot is not None:
        print(f"всего микрофон→озвучка: {fmt_dur(tot)}")
    gaps = {k: v for k, v in (rec.get("latency_sec") or {}).items()
            if k not in ("heard->spoken", "heard->phrased") and v}
    if gaps:
        print("по участкам: " + "  ".join(f"{k} {v:g}s" for k, v in gaps.items()))
    print()
    prev = None
    for e in rec.get("events", []):
        dt = parse_ts(e.get("ts", "")) or prev or now_aware()
        delta = f"+{(dt - prev).total_seconds():5.1f}s" if prev else "   —   "
        prev = dt
        data = e.get("data") or {}
        keep = {k: data[k] for k in
                ("text", "label", "goal", "outcome", "error", "reason", "source", "engine", "wakeword")
                if k in data}
        print(f"  {dt.strftime('%H:%M:%S')} {delta}  {str(e.get('module')):<16} {str(e.get('message')):<26} "
              f"{json.dumps(keep, ensure_ascii=False) if keep else ''}")

    # итог в конце: время выполнения БЕЗ синтеза речи (услышана → готов ответ)
    ex = rec.get("exec_sec")
    if ex is None:
        ex = (rec.get("latency_sec") or {}).get("heard->phrased")
    if ex is not None:
        tail = f"   (полное с озвучкой {fmt_dur(tot).strip()})" if tot is not None else ""
        print(f"\n── ИТОГ  выполнение без озвучки: {fmt_dur(ex).strip()}{tail}")
    print()


def print_trace(trace_id: str, since: datetime) -> None:
    # 1) постоянное хранилище (мгновенно, если watch его писал)
    store = TraceStore(CFG, SCRIPT_DIR)
    rec = store.load_one(trace_id) if store.enabled else None
    if rec:
        _render_trace_record(rec)
        print("(из хранилища logs/analytics/traces/)")
        return
    # 2) иначе — собрать на лету из сырых логов за окно
    events = [ev for ev in load_events(since) if ev.get("trace_id") == trace_id]
    if not events:
        print(f"[analytics] реплика {trace_id!r} не найдена (нет в хранилище и в окне --last)")
        return
    _render_trace_record(TraceView(trace_id, events).to_record())
    print("(собрано из сырых логов)")


def _print_traces_table(rows: list[dict]) -> None:
    print(f"  {'начало':<19} {'trace_id':<26} {'докуда':<11} {'статус':<9} {'цель':<20} "
          f"{'без озв.':>8} {'всего':>8}")
    for r in rows:
        started = (r.get("started_at") or "")[:19] or "—"
        print(f"  {started:<19} {str(r.get('trace_id', '—')):<26} "
              f"{str(r.get('furthest', '—')):<11} {str(r.get('status', '—')):<9} "
              f"{str(r.get('goal') or '—'):<20} {fmt_dur(r.get('exec_sec')):>8} {fmt_dur(r.get('total_sec')):>8}")


def print_traces(since: datetime, limit: int, raw: bool) -> None:
    store = TraceStore(CFG, SCRIPT_DIR)
    if not raw and store.enabled and store.index_path.exists():
        rows = [r for r in store.read_index()
                if (parse_ts(r.get("started_at", "")) or now_aware()) >= since]
        rows = rows[-limit:]
        print(f"\n═══ последние {len(rows)} реплик (из хранилища) ═══")
        _print_traces_table(rows)
        print()
        return
    traces = [TraceView(tid, evs) for tid, evs in by_trace(load_events(since)).items()]
    traces.sort(key=lambda t: t.stage_ts.get("heard") or now_aware())
    rows = [t.to_record() for t in traces[-limit:]]  # to_record кладёт total_sec / exec_sec
    print(f"\n═══ последние {len(rows)} реплик (из сырых логов) ═══")
    _print_traces_table(rows)
    print()


# ─────────────────────────── watch ─────────────────────────────────

def run_watch() -> None:
    w = CFG["watch"]
    poll = float(w["poll_interval_sec"])
    win_min = float(w["report_window_min"])
    emitted: dict[str, datetime] = {}

    store = TraceStore(CFG, SCRIPT_DIR)
    store.prune(log=lambda lvl, msg, data: send_log(lvl, msg, data))

    print(f"[analytics] watch: окно {win_min:.0f}мин, опрос каждые {poll:.0f}с. "
          f"хранилище команд: {'вкл (' + str(store.root) + ')' if store.enabled else 'выкл'}. Ctrl+C — выход.")
    send_log("INFO", "watch_started", {"window_min": win_min, "poll_sec": poll, "store": store.enabled})
    try:
        while True:
            since = now_aware() - timedelta(minutes=win_min)
            events = load_events(since)
            trace_views = [TraceView(tid, evs) for tid, evs in by_trace(events).items()]
            traces = trace_views
            fn = funnel(traces)
            an = anomalies(events, since)

            # ── постоянное хранилище истории по каждой команде
            if store.enabled:
                for tv in trace_views:
                    rec = tv.to_record()
                    store.write(rec)
                    last = tv.events[-1]["_dt"] if tv.events else since
                    closed = (tv.outcome is not None
                              or (now_aware() - last).total_seconds() > store.idle_close_sec)
                    if closed:
                        store.index_once(rec)

            # аномалии -> в шину логов (повторно не чаще ширины окна)
            if w["emit_anomaly_events"]:
                fresh = now_aware() - timedelta(minutes=win_min)
                for x in an:
                    if emitted.get(x["key"], datetime.min.replace(tzinfo=fresh.tzinfo)) > fresh:
                        continue
                    emitted[x["key"]] = now_aware()
                    send_log(x["level"], f"anomaly_{x['key'].split(':')[0]}",
                             {"key": x["key"], "title": x["title"], "detail": x["detail"]})

            if w["emit_summary_events"]:
                send_log("INFO", "window_summary", {
                    "window_min": win_min, "events": len(events), "traces": len(traces),
                    "spoken_ok": fn["spoken_ok"], "spoken_never": fn["spoken_never"],
                    "anomalies": len(an),
                })

            warn = sum(1 for x in an if x["level"] == "WARNING")
            stored = f" сохранено={len(store._indexed)}" if store.enabled else ""
            print(f"[analytics] {now_aware().strftime('%H:%M:%S')}  события={len(events)} "
                  f"реплики={len(traces)} озвучено={fn['spoken_ok']} NEVER={fn['spoken_never']} "
                  f"аномалии={len(an)} (⚠{warn}){stored}")
            for x in an:
                if x["level"] == "WARNING":
                    print(f"           ⚠ {x['title']}")
            time.sleep(poll)
    except KeyboardInterrupt:
        send_log("INFO", "watch_stopped")
        print("\n[analytics] watch остановлен")


# ─────────────────────────── CLI ───────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="analytics.py", description="аналитический слой поверх логов SMOS")
    sub = ap.add_subparsers(dest="cmd")

    p_rep = sub.add_parser("report", help="полный отчёт за окно")
    p_rep.add_argument("--since", help="ISO-время начала окна")
    p_rep.add_argument("--last", help="длина окна: 24h / 90m / 2d")
    p_rep.add_argument("--json", action="store_true", help="машиночитаемый вывод")

    p_tr = sub.add_parser("trace", help="таймлайн одной реплики")
    p_tr.add_argument("trace_id")
    p_tr.add_argument("--last", default="7d", help="как глубоко искать (по умолчанию 7d)")

    p_trs = sub.add_parser("traces", help="список последних реплик (из хранилища, если есть)")
    p_trs.add_argument("--last", help="ограничить по времени начала: 24h / 7d")
    p_trs.add_argument("-n", type=int, default=20, help="сколько показать")
    p_trs.add_argument("--raw", action="store_true", help="не хранилище, а свежий разбор сырых логов")

    sub.add_parser("watch", help="демон: хранилище истории по командам + аномалии в шину логов")

    args = ap.parse_args()
    default_hours = CFG["window"]["default_hours"]

    if args.cmd in (None, "report"):
        spec = getattr(args, "last", None) or getattr(args, "since", None)
        since = parse_since(spec, default_hours)
        print_report(since, load_events(since), as_json=getattr(args, "json", False))
    elif args.cmd == "trace":
        print_trace(args.trace_id, parse_since(args.last, default_hours))
    elif args.cmd == "traces":
        # список — это про историю, поэтому по умолчанию окно шире отчёта
        since = parse_since(args.last, CFG["store"]["keep_days"] * 24)
        print_traces(since, args.n, raw=args.raw)
    elif args.cmd == "watch":
        run_watch()


if __name__ == "__main__":
    main()
