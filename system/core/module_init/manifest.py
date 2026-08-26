"""
manifest.py — чтение и валидация манифеста одного модуля SMOS.

Формат манифеста и все решения по нему — см. manifest_design.md рядом
с этим файлом. Каждая папка модуля (в system/modules/ или в modules/ в
корне проекта) должна содержать manifest.json такого вида:

{
  "name": "weather",
  "description": "...",
  "entrypoint": {"command": ["python3", "main.py"]},
  "actions": [
    {"command": "get_weather", "description": "...", "needs": ["city"], "produces": ["weather_forecast"]}
  ]
}

Модуль без манифеста или с манифестом, не прошедшим валидацию, не
должен ронять сканирование остальных модулей — поэтому load() бросает
ManifestError с понятной причиной, а решение, что делать дальше
(пропустить и залогировать), принимает вызывающий код (registry.py).
"""

import json
from pathlib import Path

MANIFEST_FILENAME = "manifest.json"

REQUIRED_MODULE_FIELDS = ("name", "entrypoint", "actions")
REQUIRED_ACTION_FIELDS = ("command", "needs", "produces")


class ManifestError(Exception):
    """Манифест отсутствует, битый JSON, или не прошёл валидацию структуры."""


def load(module_dir: Path) -> dict:
    """Читает и валидирует manifest.json из папки модуля module_dir.
    Бросает ManifestError при любой проблеме, ничего не возвращает
    молча наполовину валидным."""
    manifest_file = module_dir / MANIFEST_FILENAME

    if not manifest_file.exists():
        raise ManifestError(f"нет {MANIFEST_FILENAME} в {module_dir}")

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ManifestError(f"не удалось прочитать {manifest_file}: {e}") from e

    if not isinstance(data, dict):
        raise ManifestError(f"{manifest_file}: манифест должен быть JSON-объектом")

    _validate(data, manifest_file)
    return data


def _validate(data: dict, source: Path) -> None:
    for field in REQUIRED_MODULE_FIELDS:
        if field not in data:
            raise ManifestError(f"{source}: не хватает поля {field!r}")

    entrypoint = data["entrypoint"]
    if not isinstance(entrypoint, dict) or "command" not in entrypoint:
        raise ManifestError(f"{source}: entrypoint должен быть объектом с полем 'command'")
    if not isinstance(entrypoint["command"], list) or not entrypoint["command"]:
        raise ManifestError(f"{source}: entrypoint.command должен быть непустым списком")

    actions = data["actions"]
    if not isinstance(actions, list) or not actions:
        raise ManifestError(f"{source}: actions должен быть непустым списком")

    for i, action in enumerate(actions):
        for field in REQUIRED_ACTION_FIELDS:
            if field not in action:
                raise ManifestError(f"{source}: actions[{i}] — не хватает поля {field!r}")
        if not isinstance(action["needs"], list) or not isinstance(action["produces"], list):
            raise ManifestError(f"{source}: actions[{i}] — needs/produces должны быть списками строк")
