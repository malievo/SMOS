"""
store.py — постоянное хранилище истории по каждой команде.

`watch` (analytics.py) на каждом опросе группирует свежие события по
trace_id и отдаёт сюда собранную запись одной реплики. Здесь она
пишется одним файлом:

    logs/analytics/traces/<YYYY-MM-DD>/<trace_id>.json

плюс, когда реплика закрыта (дошла до озвучки или замолчала на
idle_close_sec), — одна строка в `traces/index.jsonl` (быстрый список
всей истории, без пересканирования raw-логов).

Только запись в свою папку — raw-логи модулей store не трогает.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path


class TraceStore:
    def __init__(self, cfg: dict, base_dir: Path):
        s = cfg["store"]
        self.enabled = bool(s.get("enabled", True))
        self.root = base_dir / s.get("dir", "traces")
        self.index_path = self.root / s.get("index_file", "index.jsonl")
        self.idle_close_sec = float(s.get("idle_close_sec", 90))
        self.keep_days = int(s.get("keep_days", 30))
        self.state_file = base_dir / cfg["paths"].get("state_file", "watch_state.json")
        self._indexed: set[str] = set()
        self._load_state()

    # ---- состояние (какие трейсы уже в index) ----------------------

    def _load_state(self) -> None:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._indexed = set(d.get("indexed", []))
        except (OSError, ValueError):
            self._indexed = set()

    def _save_state(self) -> None:
        try:
            keep = list(self._indexed)[-5000:]  # не растим бесконечно
            self.state_file.write_text(
                json.dumps({"indexed": keep}, ensure_ascii=False), encoding="utf-8")
            self._indexed = set(keep)
        except OSError:
            pass

    # ---- запись --------------------------------------------------

    def _file_for(self, rec: dict) -> Path:
        day = (rec.get("started_at") or "")[:10] or "unknown"
        d = self.root / day
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{rec['trace_id']}.json"

    def write(self, rec: dict) -> None:
        """Перезаписывает файл реплики (идемпотентно — вызывается каждый
        опрос, пока реплика в окне)."""
        if not self.enabled:
            return
        try:
            p = self._file_for(rec)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass

    def index_once(self, rec: dict) -> None:
        """Дописывает строку в index.jsonl — ровно один раз на trace_id
        (когда реплика закрыта). Файл реплики продолжит обновляться, если
        прилетит опоздавшее событие, но в index он уже не попадёт второй
        раз."""
        if not self.enabled:
            return
        tid = rec["trace_id"]
        if tid in self._indexed:
            return
        self._indexed.add(tid)
        row = {k: rec.get(k) for k in
               ("trace_id", "started_at", "ended_at", "furthest", "status",
                "goal", "label", "outcome")}
        lat = rec.get("latency_sec") or {}
        row["exec_sec"] = rec.get("exec_sec", lat.get("heard->phrased"))
        row["total_sec"] = rec.get("total_sec", lat.get("heard->spoken"))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self._save_state()

    # ---- чтение -------------------------------------------------

    def load_one(self, trace_id: str) -> dict | None:
        if not self.root.exists():
            return None
        for hit in self.root.glob(f"*/{trace_id}.json"):
            try:
                return json.loads(hit.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def read_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        rows = []
        try:
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    # ---- уборка -----------------------------------------------

    def prune(self, log=None) -> None:
        """Удаляет папки-дни старше keep_days. index.jsonl не трогает."""
        if not self.enabled or not self.root.exists():
            return
        cutoff = (datetime.now().astimezone() - timedelta(days=self.keep_days)).date().isoformat()
        removed = 0
        for day_dir in list(self.root.iterdir()):
            if not day_dir.is_dir() or day_dir.name >= cutoff:
                continue
            for f in day_dir.glob("*.json"):
                f.unlink(missing_ok=True)
                removed += 1
            try:
                day_dir.rmdir()
            except OSError:
                pass
        if removed and log:
            log("INFO", "trace_store_pruned", {"removed": removed, "keep_days": self.keep_days})
