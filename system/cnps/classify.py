"""
classify.py — аналитический слой CNPS: какой класс важности у заявки.

Заявке нельзя доверять решение о важности (кривой или спешно написанный
модуль пришлёт болтовню с максимальным приоритетом). Поэтому класс
определяет CNPS — по источнику (`source` в манифесте), а запрошенный
класс зажимает по потолку источника. Подробно — cnps_design.md, раздел
«Аналитический слой».

Единственная публичная функция — resolve_class(manifest, cfg) ->
(class_name, class_params, info). Побочных эффектов и ввода-вывода нет.
"""

from __future__ import annotations

import config as _config

CLASS_ORDER = _config.CLASS_ORDER


def class_rank(name: str) -> int:
    """Ранг класса: 0 — самый важный (CRITICAL), больше — менее важный.
    Неизвестное имя -> ранг AMBIENT (последний)."""
    try:
        return CLASS_ORDER.index(name)
    except ValueError:
        return len(CLASS_ORDER) - 1


def _valid_class(name) -> str | None:
    return name if isinstance(name, str) and name in CLASS_ORDER else None


def _clamp_to_ceiling(requested: str, ceiling: str) -> str:
    """Нельзя быть ВАЖНЕЕ потолка. Ранг: больше = менее важно, значит
    итог = класс с max(рангов) = наименее важный из двух."""
    return requested if class_rank(requested) >= class_rank(ceiling) else ceiling


def resolve_class(manifest: dict, cfg: dict) -> tuple[str, dict, dict]:
    """Возвращает (имя класса, копию параметров класса из cfg['classes'],
    словарь-объяснение для лога).

    Логика:
      1. source из манифеста (строка). Нет / не строка -> "".
      2. Потолок = cfg['source_ceiling'].get(source). Если source не в
         cfg['trusted_direct_sources'] (и список непустой) ИЛИ источник
         неизвестен -> потолок и итог = cfg['unknown_source_class'].
      3. Запрошенный класс: manifest['class'] (если валиден), иначе
         маппинг manifest['privilege_level'] через
         cfg['privilege_level_class'], иначе дефолт источника
         (cfg['source_default_class']), иначе unknown_source_class.
      4. Итог = зажим запрошенного по потолку.
      5. Параметры класса: cfg['classes'][итог] + необязательные
         подсказки манифеста ttl_soft_sec / ttl_hard_sec (просто
         переопределяют, это hint — см. дизайн).
    """
    source = manifest.get("source")
    source = source if isinstance(source, str) and source else ""

    trusted = cfg.get("trusted_direct_sources") or []
    unknown_cls = _valid_class(cfg.get("unknown_source_class")) or "AMBIENT"

    known = source in cfg.get("source_ceiling", {}) or source in cfg.get("source_default_class", {})
    untrusted = bool(trusted) and source not in trusted

    if not source or not known or untrusted:
        ceiling = unknown_cls
        reason = "no_source" if not source else ("untrusted_source" if untrusted else "unknown_source")
    else:
        ceiling = _valid_class(cfg.get("source_ceiling", {}).get(source)) or unknown_cls
        reason = "source_ceiling"

    # что просит заявка
    requested = _valid_class(manifest.get("class"))
    requested_from = "manifest_class"
    if requested is None:
        pl = manifest.get("privilege_level")
        if pl is not None:
            requested = _valid_class(cfg.get("privilege_level_class", {}).get(str(pl)))
            requested_from = "privilege_level"
    if requested is None:
        requested = _valid_class(cfg.get("source_default_class", {}).get(source))
        requested_from = "source_default"
    if requested is None:
        requested = unknown_cls
        requested_from = "fallback"

    final = _clamp_to_ceiling(requested, ceiling)
    clamped = final != requested

    params = dict(cfg.get("classes", {}).get(final, {}))
    for hint in ("ttl_soft_sec", "ttl_hard_sec"):
        val = manifest.get(hint)
        if isinstance(val, (int, float)) and val > 0:
            params[hint] = float(val)

    info = {
        "source": source or None,
        "ceiling": ceiling,
        "requested": requested,
        "requested_from": requested_from,
        "final": final,
        "clamped": clamped,
        "reason": reason,
    }
    return final, params, info
