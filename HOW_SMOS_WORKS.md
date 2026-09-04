# Как работает SMOS — полное описание

Справочник по всей системе в текущем состоянии. Не спецификация и не
журнал решений (для решений — файлы `*_design.md` рядом с кодом), а
подробный разбор «что где лежит и что за чем происходит». Особое
внимание — ядру: как модули хранятся, вызываются, завершаются.

Написано по факту кода. Более короткий narrative по ядру —
`system/core/how_core_works.md` (от 2026-08-26, до очереди результатов);
этот файл его перекрывает по охвату.

---

## Оглавление

1. [Одним взглядом](#1-одним-взглядом)
2. [Сквозные принципы](#2-сквозные-принципы)
3. [Раскладка репозитория](#3-раскладка-репозитория)
4. [Логи (`logs/`)](#4-логи-logs)
5. [FWL — первый рабочий цикл](#5-fwl--первый-рабочий-цикл)
6. [SWL — второй рабочий цикл](#6-swl--второй-рабочий-цикл)
7. [ЯДРО — подробно](#7-ядро--подробно)
8. [Выходная сторона: `outputstructurizer` и `audio`](#8-выходная-сторона-outputstructurizer-и-audio)
9. [Запуск: `smos.py`](#9-запуск-smospy)
10. [Полный сквозной пример](#10-полный-сквозной-пример)
11. [Все рантайм-папки](#11-все-рантайм-папки)
12. [Состояние: что есть, чего нет](#12-состояние-что-есть-чего-нет)
13. [Куда смотреть за деталями](#13-куда-смотреть-за-деталями)

---

## 1. Одним взглядом

SMOS — голосовая среда: **маленькое ядро + много независимых модулей**.
Восемь постоянно работающих процессов, общаются **через файлы** (папки
плюс поллинг), никакой общей памяти.

```
микрофон
  │
  ▼  wake.py ─ детект слова активации (openWakeWord, локально) + запись фразы
     └─► system/fwl/rvs/flags/utterance.wav
  │
  ▼  req.py ─ читает utterance.wav, шлёт в Google STT
     └─► system/fwl/rvs/output/recognized.json   {"text": "..."}
  │
  ▼  classifier.py ─ «команда» или «разговор»
     └─► system/fwl/classifier/output/classified.json   {"text": "...", "label": "command"|"chat"}
  │  (label == "command")
  ▼  swl.py ─ фраза → структурированная цель (GigaChat на bootstrap-этапе)
     └─► system/core/goals/<ts>_<hex>.json   {"goal": "...", "state": {...}, "source_text": "..."}
  │
  ▼  core.py ─ ДЕМОН ЯДРА: очередь целей → задачи
     ├─ module_init: скан модулей → граф needs/produces
     ├─ planner:     achieve() — GOAP backward chaining
     └─ task_runner: поток на задачу, статус в файле
        │  на каждый шаг: subprocess  main.py <command> '<json>'
        │                 env  SMOS_MODULE_DATA=user/module_data/<name>
        ▼
     system/modules/*  и  user/modules/*   (модуль печатает один JSON, завершается)
        │
        ├─► system/core/tasks/<task_id>/state.json      (долговременная запись задачи)
        └─► system/outputstructurizer/queue/<task_id>.json   (результат — дальше на озвучку)
  │
  ▼  outputstructurizer.py ─ result/error → человеческая фраза
     │  (готовый шаблон phrases.json → GigaChat → сырой fallback)
     └─► system/audio/tasks/<task_id>.json   {"text": "...", ...}
  │
  ▼  audio.py ─ фраза → синтез речи (Google TTS gtts / spd-say)
     └─► произнесено

  Все восемь процессов → UDP 127.0.0.1:47110 → logs/listener → logs/raw/<module>/events.jsonl
  smos.py — поднимает и гасит всех разом.
```

Три условных блока:

- **FWL** (First Working Loop) — «услышать и понять, что за фраза»:
  `wake` → `req` → `classifier`. Линейный конвейер на одну фразу.
- **SWL** (Second Working Loop) — «превратить команду в цель»: `swl`.
  Тоже линейный, одна фраза.
- **CORE** (ядро) + **выход** — не конвейер, а службы: в ядро прилетают
  цели от разных команд; ядро их планирует и исполняет; результат
  уходит на формулировку и озвучку.

---

## 2. Сквозные принципы

Соблюдаются во всех процессах — если помнить их, читать любой скрипт
проще.

**Общение файлами, не памятью.** Процессы не импортируют друг друга и
не держат общих структур. Один пишет файл в папку, другой эту папку
поллит. Так же общаются части внутри ядра (поток задачи ↔ остальное —
через `state.json`).

**Атомарная запись.** Всё, что кто-то может читать на лету, пишется
через временный файл + `rename` (`Path.replace`): `x.json.tmp` →
`x.json`. Читатель никогда не видит недописанное. Исключение —
`req.py`, пишет `recognized.json` не атомарно; читатели (`classifier`)
это знают и при `JSONDecodeError` просто пробуют на следующем цикле.

**Поллинг + дедуп.** Демоны в цикле (`check_interval_sec`, обычно
0.2–0.5 c) смотрят свою входную папку/файл. Отслеживание нового:
- по **mtime** входного файла (`classifier`, `swl` — их вход это один
  перезаписываемый файл);
- по **имени файла** в папке-очереди (`core`, `outputstructurizer`,
  `audio` — вход это много файлов; имя начинается с метки времени →
  `sorted()` даёт порядок поступления);
- **что было до старта — пропускаем.** `classifier`/`swl` запоминают
  mtime входа на старте и не переобрабатывают лежавшее; `core` просто
  разберёт очередь (она копится по делу); `outputstructurizer` считает
  лежавшее в очереди на старте протухшим и убирает в `queue/stale/` (не
  произносит — старый ответ бесполезен); `audio` очередь на старте не
  чистит (фильтр протухших стоит выше).

**Каждый скрипт сам находит себя.** `SCRIPT_DIR = Path(__file__).resolve().parent`,
все пути — от него. Не зависит от того, откуда запущен.

**Конфиги — в `user/configs/`, значения по умолчанию — в коде.** У
каждого процесса свой `config.py` с `DEFAULTS` и функцией `load()`.
`load()` идёт от `SCRIPT_DIR` вверх до файла-маркера `smos.root`
(корень проекта), читает `user/configs/<имя>.json`, рекурсивно
накладывает поверх `DEFAULTS`. Файла нет / корень не найден / битый
JSON — печатает предупреждение и работает на `DEFAULTS`. Любой конфиг
можно удалить целиком или заполнить частично.
Файлы: `core.json`, `swl.json`, `classifier.json`, `rvs.json`,
`logs.json`, `outputstructurizer.json`, `audio.json`.

**Секрет — один на всю систему.** `user/.env` в корне,
`GIGACHAT_CREDENTIALS=...`. Его читают `classifier`, `swl`,
`outputstructurizer` (у каждого `config.user_env_file()` находит путь
через тот же маркер `smos.root`). В `.gitignore`.

**Логи — fire-and-forget.** У каждого процесса своя копия
`log_client.py` (~15 строк, отличается только `MODULE_NAME`). `send_log`
кидает один UDP-пакет с JSON на `127.0.0.1:47110` и не ждёт ответа.
Демон логов не запущен — пакет теряется, отправитель не замечает.
Логирование не может уронить того, кто логирует.

**GigaChat — временные «леса».** Облачную LLM зовут три места
(`classifier`, `swl`, `outputstructurizer`), и все — на bootstrap-этапе:
параллельно копится `dataset.jsonl`, чтобы потом обучить локальную
модель и облако отключить (у классификатора этот цикл уже реализован
целиком, см. §5). В самом ядре LLM нет — планировщик это чистый
graph-поиск.

**`user/` vs `system/`.** `system/` — код, поставляемый с репозиторием.
`user/` — личное (в `.gitignore`, кроме `configs/`, где лежат значения
по умолчанию под гитом): личные модули `user/modules/`, данные модулей
`user/module_data/`, секрет `user/.env`. Разделение — про происхождение
кода, не про обработку: ядро сканирует `system/modules/` и
`user/modules/` одинаково.

---

## 3. Раскладка репозитория

```
smos.py                     единая точка входа (preflight + запуск/остановка всех)
smos.root                   пустой файл-маркер корня
README.md                   обзор
IDEAS.md                    список отложенных идей
HOW_SMOS_WORKS.md           этот файл

logs/
  PROTOCOL.md               как модулю слать события
  listener/
    listener.py             демон логов (UDP :47110 → logs/raw/<module>/events.jsonl)
    config.py               DEFAULTS + load()
  raw/<module>/events.jsonl  сырой архив событий (рантайм, .gitignore)

system/
  fwl/                      первый рабочий цикл
    rvs/
      wake.py               владелец микрофона: wake-word + запись фразы целиком
      req.py                utterance.wav → Google STT → recognized.json
      config.py             (общий конфиг wake+req: user/configs/rvs.json)
      calibrate_energy.py   утилита подбора порога тишины
      flags/                utterance.wav, activity.flag (рантайм)
      output/               recognized.json (рантайм)
      debug_audio/          последняя запись для отладки (рантайм)
    classifier/
      classifier.py         recognized.json → «команда»/«разговор» → classified.json
      ai_provider.py        обёртка над GigaChat (bootstrap)
      local_provider.py     локальная модель (эмбеддинги rubert-tiny2 + логрег)
      train_local_model.py  обучение/дообучение локальной модели
      config.py
      *_design.md            дизайн классификатора и цикла дистилляции
      output/               classified.json, dataset.jsonl, shadow_state.json,
                            local_model.joblib(.bak) (рантайм)
      flags/                promoted.flag, offline_only.flag (рантайм)

  swl/
    swl.py                  classified.json (label==command) → цель в очередь ядра
    catalog.py              каталог целей из манифестов модулей
    intent_provider.py      обёртка над GigaChat: фраза + каталог → {goal, params}
    config.py
    swl_design.md            дизайн SWL
    IDEA_swap_swl_and_classifier.md
    output/                 dataset.jsonl (рантайм)

  core/                     ДЕМОН ЯДРА
    core.py                 главный цикл: очередь goals/ → задачи
    config.py
    core_design.md           модель выполнения, память задачи, планировщик, сессии
    how_core_works.md        narrative (частично устарел)
    log_client.py            общий для всех подпапок ядра (MODULE_NAME="core")
    module_init/
      registry.py            scan() модулей + build_registry() графа + snapshot
      manifest.py            чтение и валидация одного manifest.json
      manifest_design.md     формат манифеста, протокол вызова, хранение данных, зависание
      output/               modules.json, graph.json — снимок реестра (рантайм)
    planner/
      planner.py             achieve() (GOAP backward chaining) + call_module()
    task_runner/
      task_runner.py         create_task() поток-на-задачу, _run_task(), _emit_completion()
    goals/                  очередь целей от SWL (рантайм); rejected/ внутри
    tasks/<task_id>/state.json  запись каждой задачи (рантайм)

  outputstructurizer/       результат задачи → человеческая фраза
    outputstructurizer.py   демон: queue/ → фраза → заявка в audio/tasks/
    phrasing.py             три слоя выбора фразы (чистые функции)
    phrase_provider.py      обёртка над GigaChat-формулировщиком
    phrases.json            таблица готовых фраз (контент, под гитом)
    config.py
    outputstructurizer_design.md
    queue/                  вход, от task_runner (рантайм); rejected/, stale/ внутри
    output/                 dataset.jsonl — примеры от GigaChat (рантайм)

  audio/                    фраза → синтез речи, v1
    audio.py                демон: tasks/ → speak() (gtts / spd-say)
    config.py
    audio_design.md          полный аудио-демон (очередь приоритетов, музыка) — не построен
    IDEA_responder_v2_chat_branch.md
    tasks/                  вход, от outputstructurizer (рантайм); rejected/ внутри

  modules/                  модули, поставляемые с репозиторием
    calc/ datetime/ sysinfo/ notes/ journal/     ← настоящие (имя mod_*)
    clock/ echo/ greeting/ location/ weather/     ← тестовые фикстуры

  launcher/run/state.json   pid'ы процессов, запущенных smos.py (рантайм)

user/                       личное (в .gitignore, кроме configs/)
  configs/*.json            настройки процессов (в репо — значения по умолчанию)
  .env                      GIGACHAT_CREDENTIALS
  modules/                  личные модули пользователя
  module_data/<name>/       личная папка данных каждого модуля
```

---

## 4. Логи (`logs/`)

**Транспорт.** UDP на `127.0.0.1:47110`. Отправитель — функция
`send_log(level, message, data=None)` из локальной копии `log_client.py`
(есть у каждого процесса и у каждого модуля, который хочет логировать).
Формат пакета:

```json
{"module": "core", "level": "INFO", "message": "goal_dispatched", "data": {...}}
```

`level` ∈ `DEBUG/INFO/WARNING/ERROR/CRITICAL`. `message` — короткий
машинный код события в `snake_case`, не текст для людей. `ts` отправитель
не ставит.

**Демон** `logs/listener/listener.py`: биндит UDP-сокет, на каждый пакет
проставляет `ts` (своё локальное время), проверяет обязательные
`module`/`message`, дописывает строку в
`logs/raw/<module>/events.jsonl` (по файлу на модуль, append-only,
JSONL). Битый пакет — предупреждение в консоль, пакет отброшен, приём
продолжается. Демон никогда не читает и не удаляет уже записанное.

Аналитического слоя поверх `logs/raw/` пока нет.

---

## 5. FWL — первый рабочий цикл

### 5.1 `wake.py` — микрофон, слово активации, запись фразы

Единственный владелец микрофона в системе. Держит один открытый поток
PyAudio.

- Состояние **`listening`**: непрерывно читает чанки, гоняет через
  локальную модель `openWakeWord` (офлайн, без сети), держит кольцевой
  буфер последних `prebuffer_seconds` секунд (довесок ДО слова).
- Услышал слово (`score > detection_threshold`, с дебаунсом
  `detection_cooldown_sec`) → состояние **`recording`**: копит чанки,
  начиная с довеска, пока не наберётся `pause_threshold_sec` секунд
  тишины подряд (RMS чанка < `energy_threshold`) или не упрётся в
  `max_utterance_seconds`.
- Сохраняет всё накопленное **одним** WAV-файлом,
  **атомарно** → `system/fwl/rvs/flags/utterance.wav`. Возвращается в
  `listening`.
- **Продолжение диалога без слова активации:** если `req.py` недавно
  успешно распознал фразу (следит за mtime `flags/activity.flag`),
  следующие `continuation_window_sec` секунд ЛЮБАЯ речь тоже уходит в
  запись.

Конфиг: `user/configs/rvs.json` (общий с `req.py`). Пока не указана своя
модель слова (`wake_word.model_path`), грузятся все встроенные модели
openWakeWord (`hey_jarvis`, `alexa`, …) — для проверки цепочки.

### 5.2 `req.py` — речь в текст

Микрофон не трогает. Поллит `flags/utterance.wav`:

- Файл появился → читает и **сразу удаляет** его, отправляет в Google
  STT (через `SpeechRecognition`), результат (текст + confidence +
  альтернативы + язык + время) пишет в
  `system/fwl/rvs/output/recognized.json` (не атомарно).
- При каждом успешном распознавании трогает `flags/activity.flag`
  (обновляет mtime) — сигнал для `wake.py` продлить окно продолжения
  диалога.
- Не распознал — `WARNING speech_not_recognized`, файл ответа не
  трогает.

### 5.3 `classifier.py` — «команда» или «разговор»

Первая точка в системе с реальным ветвящимся решением. Следит за
`recognized.json` по mtime (на старте запоминает текущий mtime, лежавшее
не переобрабатывает). На новую фразу определяет `label` ∈
`command` | `chat`, пишет **атомарно** в
`system/fwl/classifier/output/classified.json`:

```json
{"text": "какая погода в питере", "label": "command", "timestamp": "..."}
```

Кто даёт метку — зависит от режима (определяется по флагам и файлам, без
состояния в памяти):

| Режим | Условие | Кто отвечает |
|---|---|---|
| BOOTSTRAP | нет `output/local_model.joblib` | GigaChat (`ai_provider.ai_classify`) |
| SHADOW | модель есть, `flags/promoted.flag` нет | GigaChat; локальная (`local_provider`) считает параллельно, только сравнение |
| VALIDATING | `promoted.flag` есть, `offline_only.flag` нет | локальная; GigaChat параллельно ограниченное число раз — проверка |
| OFFLINE | `flags/offline_only.flag` есть | только локальная, GigaChat больше не зовётся — финал |

Пока зовётся GigaChat — каждая пара (фраза, метка) пишется в
`output/dataset.jsonl`. Когда набирается `retrain_batch_size` новых
примеров — `train_local_model.py` обучает/дообучает локальную модель
(эмбеддинги `cointegrated/rubert-tiny2` + логистическая регрессия).
Когда согласие локальной и облачной стабильно ≥ `agreement_threshold`
на ≥ `min_comparisons_for_promotion` сравнениях — создаётся
`promoted.flag` (локальная становится основной), после проверочного
окна `post_promotion_validation_comparisons` — `offline_only.flag`
навсегда. Счётчики — в `output/shadow_state.json`. Все пороги — в
`user/configs/classifier.json`.

`label == "chat"` сейчас никто не читает — разговорная ветка не
построена. Дальше по цепочке идёт только `command`.

---

## 6. SWL — второй рабочий цикл

Один процесс `swl.py` + два вспомогательных файла. По форме — как
`classifier.py` (поллинг одного входного файла).

### `swl.py`

Следит за `classified.json` по mtime (на старте запоминает mtime, лежавшее
не переобрабатывает). На новую запись:

- `label != "command"` → `DEBUG skipped_non_command`, всё.
- `label == "command"` → `handle_command(text)`:
  1. `catalog.build()` — каталог целей (см. ниже).
  2. `intent_provider.extract(text, catalog)` → `(goal, params)`.
  3. `append_to_dataset(text, goal, params)` — строка в
     `system/swl/output/dataset.jsonl` (`{text, goal, params, source: "llm_api"}`),
     **включая** `goal == None` (отрицательные примеры тоже нужны).
  4. `goal is None` → `INFO no_intent_match`, ничего не отправляем.
     Что делать с нераспознанной командой по-хорошему («не могу»,
     переспросить, отдать в chat) — открытый вопрос.
  5. `goal` найден → `write_goal(goal, params, text)`: **атомарно** пишет
     файл в `system/core/goals/`, имя
     `<YYYYMMDD_HHMMSS>_<8hex>.json`, содержимое:
     ```json
     {"goal": "weather_forecast", "state": {"city": "Санкт-Петербург"},
      "origin": "swl", "source_text": "какая погода в питере", "ts": "..."}
     ```

### `catalog.py`

`build(force=False)` — сканирует манифесты модулей через
`module_init.registry.scan()` (тот же сканер, что у ядра), собирает
плоский список: на каждый `produces`-ключ каждого действия —
`{"goal": <ключ>, "description": <из манифеста>, "needs": [...]}`. Один
ключ производят несколько действий — берётся первый. Результат
кэшируется (состав модулей при работе не меняется).

### `intent_provider.py`

Тонкая обёртка над GigaChat — единственное место SWL, знающее про
конкретного провайдера (близнец `classifier/ai_provider.py`).
`extract(text, catalog)`: системный промт + фраза + каталог (JSON) →
GigaChat (`temperature=0`) → строгий JSON
`{"goal": "<ключ>|null", "params": {...}}`. Проверяет, что `goal` —
из каталога (или `null`); неожиданный ответ → `ValueError` (ловится в
`swl.handle_command`, идёт в лог `intent_extraction_failed`, цель не
отправляется).

Позже заменяется локальной моделью по тому же циклу, что классификатор.
`catalog.py` при этом не меняется — он и сейчас провайдеро-независим.

---

## 7. ЯДРО — подробно

### 7.1 Что такое «ядро»

Не один скрипт. Четыре части в `system/core/`:

| Часть | Что это | Роль |
|---|---|---|
| `core.py` | **демон**, единственный долгоживущий процесс ядра | главный цикл: берёт цели из очереди `goals/`, раздаёт в задачи, никогда не ждёт |
| `module_init/` (`registry.py` + `manifest.py`) | набор функций, зовётся **один раз на старте** `core.py` | найти модули, проверить манифесты, построить граф `needs/produces`, отвести папки данных, сохранить снимок |
| `planner/planner.py` | чистые функции, зовутся из потока задачи | `achieve()` — разрешение цели рекурсией по графу; `call_module()` — **сам запуск модуля как процесса** |
| `task_runner/task_runner.py` | функции + поток на задачу | `create_task()` заводит поток и сразу возвращает id; `_run_task()` ведёт задачу; `_emit_completion()` отдаёт результат дальше |

Всё общается вызовами функций и файлами. В памяти `core.py` держит
ровно одну вещь — `graph` (построен на старте, дальше только читается).
Реестра активных задач в памяти **нет** — это сразу вопросы
потокобезопасности; вместо него у каждой задачи свой `state.json` на
диске.

### 7.2 Как модули ХРАНЯТСЯ

**Модуль = папка.** Либо `system/modules/<x>/` (идёт с репозиторием),
либо `user/modules/<x>/` (личный). Ядро сканирует обе одинаково —
разница только в происхождении кода.

Имя папки роли не играет. **Идентичность модуля — поле `name` в его
`manifest.json`** (например папка `system/modules/journal/`, а модуль
`mod_journal`). По `name`:
- он значится в графе и в логах (поле `module`);
- к нему привязана папка данных `user/module_data/<name>/`.

**Содержимое папки модуля:**
- `manifest.json` — обязателен;
- точка входа (например `main.py`) — что указано в `entrypoint.command`;
- по желанию своя копия `log_client.py`.

**Формат `manifest.json`:**

```json
{
  "name": "mod_calc",
  "description": "Калькулятор арифметических выражений ...",
  "entrypoint": {"command": ["python3", "main.py"]},
  "actions": [
    {
      "command": "evaluate",
      "description": "Вычисляет выражение",
      "needs": ["expression"],
      "produces": ["calc_result"],
      "cost": 1
    }
  ]
}
```

- **`name`** — уникальный id.
- **`description`** — человекочитаемый текст. Планировщику не нужен (он
  работает только с `needs`/`produces`), но `catalog.py` в SWL отдаёт
  его GigaChat, чтобы тот выбрал цель.
- **`entrypoint.command`** — список: как запустить процесс модуля.
  Поверх него ядро добавит имя команды и JSON-параметры.
- **`actions`** — список действий. Одна папка/процесс может объявлять
  несколько действий (у `greeting` — `say_hello` и `say_bye`). Каждое
  действие — отдельный узел графа.
  - **`needs`** — плоский список строк-ключей, которые нужны на входе.
  - **`produces`** — плоский список строк-ключей, которые действие даёт
    на выходе.
  - **`cost`** — необязательное число, по умолчанию 1. Заведено на
    будущее (выбор между конкурирующими путями), сейчас **не
    используется**.

**Валидация** (`manifest.py:load()` → `_validate()`): обязательные поля
модуля `name`/`entrypoint`/`actions`; `entrypoint.command` — непустой
список; `actions` — непустой список; у каждого действия `command`/
`needs`/`produces`; `needs`/`produces` — списки; `cost`, если есть, —
число. Любое нарушение → `ManifestError`. Битый манифест **не роняет
скан** — `registry._scan_dir` ловит `ManifestError`, шлёт
`WARNING manifest_invalid`, печатает `[module_init] пропускаю <папка>`
и идёт дальше.

**Данные модуля хранятся отдельно от кода.** Папка
`user/module_data/<name>/`:
- **создаётся при скане** (`registry._scan_dir` делает
  `mkdir(parents=True, exist_ok=True)` на каждый валидный манифест —
  модуль обнаружен, место сразу отведено, даже если его ни разу не
  вызовут);
- ключ — `name` из манифеста, не путь и не место установки. Удалил
  папку кода модуля → `user/module_data/<name>/` в другом дереве, цела.
  Добавил снова модуль с тем же `name` → он получит ту же папку и
  откроет свои старые файлы. Переименовал модуль → осиротил данные;
- путь абсолютный, кладётся в граф как `data_dir` и в снимок
  (`modules.json` как `_data_dir`, `graph.json` как `data_dir`);
- модулю сообщается **на каждом вызове** переменной окружения
  `SMOS_MODULE_DATA` (см. §7.5). Модуль путь не вычисляет.
- что и как внутри — дело модуля (JSON, JSONL, SQLite, подпапки).
  Система только выделяет место. `mod_notes` кладёт `notes.jsonl`,
  `mod_journal` — `journal.jsonl`.

### 7.3 Как ядро СТАРТУЕТ (`module_init`)

`core.py:main()` первым делом зовёт `build_graph()`:

```python
modules = module_registry.scan()
graph   = module_registry.build_registry(modules)
module_registry.save(modules, graph)
```

**`scan()`** → `_scan_dir(SYSTEM_MODULES_DIR) + _scan_dir(USER_MODULES_DIR)`.
`_scan_dir` обходит подпапки (`sorted`), на каждой:
1. `manifest.load(папка)` — читает и валидирует `manifest.json`.
   `ManifestError` → лог + пропуск.
2. `mkdir user/module_data/<name>/`.
3. добавляет в dict манифеста служебные `_module_dir` (абсолютный путь
   к коду) и `_data_dir` (абсолютный путь к данным).
Возвращает список dict'ов. Лог `INFO modules_scanned {count}`.

**`build_registry(modules)`** — разворачивает манифесты в **плоский
реестр по КОМАНДЕ действия** (не по модулю):

```json
{
  "get_datetime":  {"module": "mod_datetime", "module_dir": "...", "data_dir": "...",
                    "entrypoint": ["python3","main.py"], "needs": [],
                    "produces": ["current_datetime"], "cost": 1},
  "get_weather":   {"module": "weather", "needs": ["city"],
                    "produces": ["weather_forecast"], ...}
}
```

Две команды с одинаковым именем в разных модулях → `WARNING duplicate_action`,
побеждает первая по порядку скана. Этот `graph` — то, в чём планировщик
ищет путь.

**`save(modules, graph)`** — атомарно пишет
`module_init/output/modules.json` (сырые манифесты + `_module_dir` +
`_data_dir`) и `output/graph.json` (реестр). Это **не источник истины** —
им остаётся свежий скан при каждом старте ядра. Снимок нужен, чтобы
посмотреть глазами и чтобы другие части системы (SWL `catalog.py` могла
бы) читали без запуска Python.

**Без hot-reload.** Новый модуль виден только после перезапуска
`core.py`. Осознанное решение.

Дальше `INFO core_started {actions: [...]}` и главный цикл.

### 7.4 Как ядро ПРИНИМАЕТ цель (главный цикл `core.py`)

```python
while True:
    for goal_file in pending_goal_files():   # sorted(GOALS_DIR.glob("*.json"))
        process_goal_file(goal_file, graph)
    time.sleep(CHECK_INTERVAL_SEC)            # 0.3 c по умолчанию
```

`pending_goal_files()` — `*.json` в `system/core/goals/`, старейшие
первыми (имя от SWL начинается с метки времени). Подпапка `rejected/` и
временные `*.json.tmp` в `glob("*.json")` не попадают.

`process_goal_file(goal_file, graph)`:
1. Прочитать и распарсить JSON. `JSONDecodeError`/`OSError` →
   `_reject`. (SWL пишет атомарно, значит битый файл — действительно
   битая цель, а не гонка с записью.)
2. Не dict → `_reject`.
3. `goal` — обязателен, непустая строка. Иначе → `_reject`.
4. `state` — необязателен; `None` → `{}`; если есть и не dict →
   `_reject`. Это то, что SWL уже вытащил из фразы (`{"city": "..."}`).
5. `source_text` — необязателен, любой (исходная фраза).
6. `task_id = task_runner.create_task(goal, state, graph, source_text)`
   — **возвращает мгновенно**.
7. `goal_file.unlink()` — цель разобрана.
8. `INFO goal_dispatched {goal, task_id, source_text, known_keys}`.

`_reject(goal_file, reason)` — переносит файл в
`system/core/goals/rejected/<stem>_<8hex>.json` (не удаляет — можно
посмотреть, что пришло не так), `WARNING goal_rejected`.

**Главный цикл никогда не ждёт задачу.** Он только раздаёт. После
`create_task()` `core.py` про задачу забывает — дальше она сама по
себе, общение с ней только через `tasks/<id>/state.json`.

### 7.5 Как модуль ВЫЗЫВАЕТСЯ (`planner.call_module`)

Это происходит **внутри потока задачи**, из `achieve()`. Единственное
место, где ядро запускает чужой код.

```python
def call_module(action: dict, command: str, params: dict) -> dict:
    data_dir = Path(action["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)          # страховка; обычно уже создана при скане
    env = {**os.environ, "SMOS_MODULE_DATA": str(data_dir)}

    cmd = [*action["entrypoint"], command, json.dumps(params, ensure_ascii=False)]
    # напр.: ["python3", "main.py", "get_weather", '{"city": "Москва"}']

    result = subprocess.run(
        cmd,
        cwd=action["module_dir"],       # рабочая папка = папка кода модуля
        env=env,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SEC,     # 5 секунд
    )
```

Разбор:

- **argv-массив, не строка для шелла.** ОС создаёт процесс напрямую с
  этим массивом аргументов. Шелл не участвует — кавычки, пробелы,
  кириллица внутри JSON не требуют экранирования.
- **`cwd` = папка кода модуля** (`module_dir`). Поэтому относительные
  пути внутри модуля отсчитываются от неё. Но для СВОИХ данных модуль
  должен брать `SMOS_MODULE_DATA`, а не `cwd`.
- **`env`** = окружение ядра + `SMOS_MODULE_DATA` (абсолютный путь к
  `user/module_data/<name>/`). Не-Python модуль читает `$SMOS_MODULE_DATA`
  так же.
- **`timeout=5` — жёсткий.** Не уложился → `subprocess.TimeoutExpired`.
  (Умное определение зависания по heartbeat спроектировано в
  `manifest_design.md`, не построено.)
- **`subprocess.run` блокирующий.** Поток задачи стоит и ждёт модуль.
  Но только **этот** поток — главный цикл ядра и другие задачи-потоки
  работают: Python отпускает GIL, пока ждёт дочерний процесс, поэтому
  параллелизм между задачами настоящий.

**Что делает модуль** (см. `system/modules/*/main.py`):

```python
command = sys.argv[1]                 # "get_weather"
params  = json.loads(sys.argv[2])     # {"city": "Москва"}
# ... своя логика ...
print(json.dumps({"status": "ok", "data": {"weather_forecast": {...}}}, ensure_ascii=False))
```

Печатает в stdout **ровно один** JSON-объект и завершается. Отладочный
вывод — только через свой `log_client` (UDP), не в stdout.

**Как `call_module` разбирает исход:**

| Ситуация | Что делает `call_module` | Возврат наружу |
|---|---|---|
| `TimeoutExpired` (не уложился в 5 c) | `ERROR module_timeout` | `{"status":"error","error":"таймаут"}` |
| `returncode != 0` (модуль упал, трейсбек в stderr) | `ERROR module_crashed` + stderr | `{"status":"error","error":"процесс завершился с кодом N"}` |
| stdout не парсится как JSON | `ERROR module_bad_output` + stdout | `{"status":"error","error":"не удалось разобрать ответ модуля"}` |
| stdout — валидный JSON | — | этот JSON как есть |

Ни один из отказов **не роняет** ни поток задачи, ни ядро. Падение
модуля — просто неудачный шаг одной задачи.

### 7.6 Три формы ответа модуля

Модуль печатает один из трёх объектов:

```json
{"status": "ok",      "data": {"<produces-ключ>": <значение>, ...}}
{"status": "missing", "missing": ["<ключ>", ...]}
{"status": "error",   "error": "<текст>"}
```

- **`ok`** — успех. `achieve()` делает `state.update(response["data"])`.
- **`missing`** — «мне нужен параметр, которого я не объявлял в
  `needs`». Планировщик рекурсивно добывает каждый недостающий ключ и
  **повторяет вызов** модуля с `{**params, **state}`. (Штатные `needs`
  добываются заранее, до первого вызова; `missing` — подстраховка сверх
  манифеста. На практике тестовые модули так не делают, но код есть.)
- **`error`** — модуль попытался и не смог. `achieve()` бросает
  `PlanningError(error)` → задача завершится со `status: error`.

Пример из `mod_calc`: команду вызвали без `expression` →
`{"status":"missing","missing":["expression"]}`; выражение с делением на
ноль → `{"status":"error","error":"..."}`; всё хорошо →
`{"status":"ok","data":{"calc_result":{"expression":"2+2","value":4}}}`.

### 7.7 Как ядро СТРОИТ ПЛАН (`planner.achieve` — backward chaining)

```python
def achieve(target_key, state, graph, _resolving=None):
    if target_key in state:                       # (1)
        return state[target_key]

    _resolving = _resolving or set()              # (2) защита от циклов
    if target_key in _resolving:
        raise PlanningError(f"цикл при разрешении {target_key!r}")
    _resolving.add(target_key)

    found = find_action_that_produces(target_key, graph)   # (3)
    if found is None:
        raise PlanningError(f"никто не производит {target_key!r}")
    command, action = found

    params = {}
    for need in action["needs"]:                  # (4) рекурсия по needs, по очереди
        params[need] = achieve(need, state, graph, _resolving)

    response = call_module(action, command, params)        # (5)

    if response.get("status") == "missing":       # (6)
        for key in response.get("missing", []):
            state[key] = achieve(key, state, graph, _resolving)
        response = call_module(action, command, {**params, **state})

    _resolving.discard(target_key)

    if response.get("status") == "ok":            # (7)
        state.update(response["data"])
        return state[target_key]

    raise PlanningError(response.get("error", "неизвестная ошибка модуля"))
```

Пошагово:

1. **Ключ уже в `state`** → вернуть, ничего не вызывать. Это то самое
   «короткое замыкание», из-за которого предвытащенные SWL параметры
   (`{"city": "Питер"}`) пропускают шаги.
2. **Защита от циклов.** `_resolving` — множество ключей, которые прямо
   сейчас разрешаются вверх по рекурсии. Повторное попадание (манифесты
   образовали круг A→B→A) — ошибка, а не зависание.
3. **Кто производит ключ.** `find_action_that_produces` — первый
   action, у которого `target_key` в `produces`. Несколько кандидатов —
   берётся первый по порядку в графе (выбор «поумнее» — открытый
   вопрос). Никто — `PlanningError`.
4. **Обеспечить `needs`.** Для каждого — рекурсивный `achieve`. По
   очереди, не параллельно (сознательно: частичные отказы и слияние
   результатов не стоят выигрыша, пока цепочки короткие).
5. **Вызвать модуль** (см. §7.5).
6. **Если модуль попросил больше** (`missing`) — добыть и повторить.
7. **Успех** → влить `data` в `state`, вернуть значение ключа. Иначе —
   `PlanningError`.

**Сквозной пример: цель `journal_written`, пустой `state`.**

```
achieve("journal_written")
  нет в state → производит write_snapshot (mod_journal), needs = [current_datetime, system_stats]
  achieve("current_datetime")
      нет в state → производит get_datetime (mod_datetime), needs = []
      call_module → {"status":"ok","data":{"current_datetime": {...}}}
      state["current_datetime"] = {...} ; return
  achieve("system_stats")
      нет в state → производит get_system_stats (mod_sysinfo), needs = []
      call_module → {"status":"ok","data":{"system_stats": {...}}}
      state["system_stats"] = {...} ; return
  call_module(write_snapshot, {"current_datetime": {...}, "system_stats": {...}})
      модуль дописывает user/module_data/mod_journal/journal.jsonl
      → {"status":"ok","data":{"journal_written": {...}}}
  state["journal_written"] = {...} ; return
```

Ребра `journal → datetime, sysinfo` нигде не прописаны — планировщик
вывел их из `needs`/`produces`.

### 7.8 Как задача ХРАНИТСЯ и ЗАВЕРШАЕТСЯ (`task_runner`)

**`create_task(goal, initial_state, graph, source_text=None) -> str`:**
1. `task_id = _new_task_id()` =
   `time.strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8]`
   (например `20260828_011623_5a294eae`) — читаемо глазами по порядку,
   уникально при параллельном создании.
2. `threading.Thread(target=_run_task, args=(...), daemon=True).start()`.
   **daemon** — поток умрёт вместе с процессом.
3. `return task_id` — сразу, не дожидаясь выполнения.

**`_run_task(task_id, goal, initial_state, graph, source_text)`** — вся
жизнь одной задачи, в своём потоке:
1. `task_dir = system/core/tasks/<task_id>/`.
2. `record = {task_id, goal, state: dict(initial_state), status: "running",
   result: None, error: None}`.
3. `_save_state(task_dir, record)` — атомарно пишет
   `task_dir/state.json` (`state.json.tmp` + rename, `mkdir` папки).
4. `INFO task_started`.
5. ```python
   try:
       result = planner.achieve(goal, record["state"], graph)
       record["status"] = "done"; record["result"] = result
   except planner.PlanningError as e:
       record["status"] = "error"; record["error"] = str(e)
   ```
   `record["state"]` **мутируется на месте** внутри `achieve` — поэтому
   в финальном `state.json` лежит ВСЁ накопленное (город + прогноз +
   …), а `result` — только значение цели.
6. `_save_state(task_dir, record)` — финальная запись.
7. `_emit_completion(record, source_text)` — атомарно пишет
   `system/outputstructurizer/queue/<task_id>.json`:
   ```json
   {"task_id": "...", "goal": "...", "status": "done|error",
    "result": <значение цели|null>, "error": <текст|null>,
    "state": {<всё накопленное>}, "source_text": "<исходная фраза|null>",
    "ts": "..."}
   ```
   Пишется **и на `done`, и на `error`** (ошибку тоже надо озвучить).
   Место под поле `speech` (дословная формулировка от модуля) —
   оставлено на будущее, сейчас не заполняется.
8. Поток возвращается — задача завершена. Папку `tasks/<task_id>/`
   никто не убирает: это долговременная запись.

**`read_state(task_id)`** — читает `tasks/<id>/state.json`. Так любая
часть системы узнаёт исход задачи — не спрашивая поток. (Сейчас этим
пользуется только `task_runner.__main__`; `outputstructurizer` читает
свой файл из очереди, который положил `_emit_completion`.)

**Параллелизм.** N задач = N потоков. У каждого свой `task_dir`, свой
`record`. Общей изменяемой структуры нет, блокировок нет. `state.json`
на задачу пишется атомарно — читатель не увидит половину.

### 7.9 Чего ядро НЕ делает

- **Не ждёт задачу.** Главный цикл раздаёт и идёт дальше.
- **Не держит реестр активных задач в памяти.** Только файлы
  `tasks/<id>/state.json`.
- **Не перезагружает модули на лету.** Скан только на старте.
- **Не использует LLM.** Планировщик — чистый graph-поиск.
- **Не формулирует ответ.** Пересылает сырое в
  `outputstructurizer/queue/`; фразу делает `outputstructurizer`.
- **Не выбирает между модулями, производящими одно и то же.** Берёт
  первый. `cost` заведён, не используется.
- **Не разрешает `needs` параллельно.** Последовательный `for`. Разные
  задачи и так параллельны (потоки).
- **Не умеет умно ловить зависший модуль.** Жёсткий `timeout=5`.
  Heartbeat-подход — в `manifest_design.md`, не построен.
- **Не отслеживает долгоживущие/сессионные модули** (таймер, музыка) —
  спроектировано в `core_design.md`, не построено. `call_module` умеет
  только «запустил, дождался, прочитал».
- Убить `core.py` посреди задачи → её поток-демон умирает,
  `tasks/<id>/state.json` навсегда останется `running`.

---

## 8. Выходная сторона: `outputstructurizer` и `audio`

Два отдельных процесса (осознанно разделены: один формулирует, другой
владеет звуком).

### 8.1 `outputstructurizer.py` — результат задачи → фраза

Вход: `system/outputstructurizer/queue/` (сюда пишет
`task_runner._emit_completion`). Выход: `system/audio/tasks/`.

- **На старте:** всё, что уже лежит в `queue/`, — протухшие ответы
  (система была выключена). Переезжают в `queue/stale/`, **не
  произносятся**. Дальше реагируем только на новые файлы.
- Цикл поллит `queue/*.json` (sorted, имя = `task_id`). На каждый файл
  `process_file`:
  1. Разобрать JSON. Битый / не объект / нет `task_id`/`goal`/`status`
     (строки) → перенос в `queue/rejected/`, `WARNING result_rejected`.
  2. `task_id` уже в `seen` (обработан в этом запуске) → удалить файл,
     не произносить дважды.
  3. `text, source = phrasing.render(record, phrases, CFG)` — см. ниже.
  4. `emit_audio_task(record, text, source)` — **атомарно** пишет
     `system/audio/tasks/<task_id>.json`:
     ```json
     {"task_id": "...", "goal": "...", "status": "...", "text": "<фраза>",
      "privilege_level": 2, "source": "template|llm|fallback",
      "source_text": "<исходная фраза>", "kind": "tts", "ts": "..."}
     ```
     (`privilege_level` и `kind` — на будущее, под приоритетную очередь
     настоящего аудио-демона; v1 их не использует.)
  5. `source == "llm"` → строка в `output/dataset.jsonl` (примеры от
     GigaChat — материал для будущей локальной модели-формулировщика).
  6. `seen.add(task_id)`, удалить файл очереди.
  7. `INFO phrase_emitted`.

**`phrasing.render(record, phrases, cfg)` → `(text, source)`** — три
слоя, сверху вниз, ни один не бросает исключений наружу:

1. **Готовая фраза** (`source = "template"`). Шаблон из `phrases.json`
   по ключу: `_error` если `status == "error"`, иначе `goal`-ключ.
   Значение — строка или список строк (тогда случайный вариант, если
   `phrasing.random_variants`). Подстановка `str.format` по именам:
   `result` (значение цели), `state` (всё накопленное), `source_text`,
   `goal`, `error`. Шаблон ссылается на несуществующее поле → слой
   пропускается (`WARNING template_render_failed`).
2. **Формулировщик** (`source = "llm"`). `phrase_provider.formulate(goal,
   status, result, error, source_text)` — GigaChat (ленивый импорт:
   без пакета/ключа слои 1 и 3 всё равно работают). Системный промт:
   «ассистент выполнил команду, вот goal/result/error — дай одну
   короткую устную фразу по-русски, передай конкретику, числа не
   округляй». Любой сбой (нет ключа, сеть, пустой ответ) → на слой 3.
3. **Сырой fallback** (`source = "fallback"`). Шаблоны из конфига:
   `error_fallback_template` (`"Не удалось выполнить: {error}"`) или
   `result_fallback_template` (`"Готово: {result}"`). Гарантирует, что
   ответ уйдёт всегда, даже без интернета.

`phrases.json` (контент, не настройки, под гитом) — ключ = `goal`-ключ
из `produces` модуля. Сейчас заполнены `current_datetime`, `calc_result`,
`note_saved`, `journal_written`, `echoed_text`, `greeting_text`,
`farewell_text`, `_error`.

### 8.2 `audio.py` — фраза → синтез речи (v1)

Вход: `system/audio/tasks/`. Это **не** полный аудио-демон из
`audio_design.md` — нет приоритетной очереди, приглушения музыки,
будильника. Только: взять заявку, произнести `text`, удалить.

- Цикл поллит `tasks/*.json` (sorted). На каждый `process_file`:
  разобрать, проверить непустое строковое `text`, `speak(text)`,
  удалить. Битая заявка → `tasks/rejected/`.
- На старте очередь **не чистит** (фильтр протухших — выше, в
  `outputstructurizer`).

**`speak(text)`** — цепочка движков `[primary, fallback]`
(`user/configs/audio.json` → `tts.engine`, `tts.fallback_engine`):
- **`gtts`** — Google TTS: пакет `gTTS` синтезирует MP3 (сетевой
  вызов), внешний плеер (`tts.gtts.player`, по умолчанию
  `gst-play-1.0`) его проигрывает. Временный MP3 удаляется всегда.
- **`spd-say`** — speech-dispatcher, локально/оффлайн.
Первичный не сработал (нет пакета / сети / плеера / команды / таймаут)
→ запасной → просто печать текста. Любой сбой синтеза **никогда не
роняет** демон и не стопорит очередь. `tts.enabled = false` — только
печать.

---

## 9. Запуск: `smos.py`

Единая точка входа в корне. Раньше надо было вручную открыть консоль на
каждый процесс — теперь одна команда.

**Список процессов и порядок** (`PROCESSES` в `smos.py`):

| # | Имя | Скрипт | Зависимости (пакеты) |
|---|---|---|---|
| 1 | `logs` | `logs/listener/listener.py` | — (первым: биндит UDP-сокет) |
| 2 | `core` | `system/core/core.py` | — |
| 3 | `outputstructurizer` | `system/outputstructurizer/outputstructurizer.py` | `gigachat`, `dotenv` |
| 4 | `audio` | `system/audio/audio.py` | — (для gtts: `gtts` + MP3-плеер) |
| 5 | `swl` | `system/swl/swl.py` | `gigachat`, `dotenv` |
| 6 | `classifier` | `system/fwl/classifier/classifier.py` | `gigachat`, `dotenv`, `sentence_transformers`, `sklearn`, `joblib`, `numpy` |
| 7 | `req` | `system/fwl/rvs/req.py` | `speech_recognition` |
| 8 | `wake` | `system/fwl/rvs/wake.py` | `openwakeword`, `pyaudio`, `numpy` |

Порядок важен только для `logs` (первым, иначе стартовые логи остальных
теряются). Остальные общаются файлами, порядок не критичен.

**Команды:**

```
python smos.py                  preflight + запуск всех; общий вывод; Ctrl+C гасит всех
python smos.py start --debugview   каждый процесс в своей панели tmux
python smos.py start --only swl,core
python smos.py start --skip wake,req
python smos.py start --restart     поднимать упавший процесс заново (с backoff)
python smos.py status           кто жив, pid, uptime, режим классификатора
python smos.py stop             погасить всё, что запускал launcher (даже из другой консоли)
python smos.py restart [флаги start]
python smos.py check            только preflight
```

**Preflight** проверяет: запущен ли из venv, стоят ли нужные пакеты
(по `requires` каждого процесса), есть ли `user/configs/*`, есть ли ключ
GigaChat (если в наборе есть процесс, которому он нужен). Запускать
`.venv/bin/python smos.py`, чтобы дети унаследовали интерпретатор.

**Состояние launcher'а** — `system/launcher/run/state.json`: pid'ы,
скрипты, время старта. Поэтому `stop` работает из другой консоли и даже
если сам launcher закрыт — осиротевший `wake.py` на микрофоне всё равно
убирается.

**Проверить без микрофона:**

```bash
.venv/bin/python system/swl/swl.py "запиши снимок системы в журнал"
# фраза → GigaChat → цель → очередь ядра → планировщик → mod_datetime + mod_sysinfo
#        → mod_journal → outputstructurizer → audio (если запущены)
```

**Только выход:** положить файл прямо в очередь `outputstructurizer`
(процесс должен быть запущен ПОСЛЕ появления файла — иначе сочтёт
протухшим):

```bash
echo '{"task_id":"manual_1","goal":"calc_result","status":"done","result":{"expression":"2+2","value":4},"state":{},"source_text":"посчитай"}' \
  > system/outputstructurizer/queue/manual_1.json
```

---

## 10. Полный сквозной пример

Фраза вслух: **«запиши купить хлеб»**

| # | Процесс | Что происходит | Файл на диске |
|---|---|---|---|
| 1 | `wake` | услышал слово активации, записал фразу до тишины | `system/fwl/rvs/flags/utterance.wav` |
| 2 | `req` | прочитал и удалил wav, Google STT | `system/fwl/rvs/output/recognized.json` = `{"text": "запиши купить хлеб", ...}`; трогает `flags/activity.flag` |
| 3 | `classifier` | mtime `recognized.json` изменился → GigaChat (или локальная) → `command` | `system/fwl/classifier/output/classified.json` = `{"text": "...", "label": "command"}` |
| 4 | `swl` | mtime `classified.json` изменился, label=command → `catalog.build()` + GigaChat → `{"goal": "note_saved", "params": {"note_text": "купить хлеб"}}` | `system/core/goals/20260828_..._a1b2c3d4.json` = `{"goal": "note_saved", "state": {"note_text": "купить хлеб"}, "source_text": "запиши купить хлеб", ...}`; строка в `system/swl/output/dataset.jsonl` |
| 5 | `core` (главный цикл) | нашёл файл в `goals/`, разобрал, `create_task("note_saved", {"note_text": "купить хлеб"}, graph, "запиши купить хлеб")`, удалил файл цели | — |
| 6 | `core` (поток задачи) | `_run_task`: пишет `state.json` (`running`) → `achieve("note_saved", ...)` | `system/core/tasks/20260828_..._<hex>/state.json` |
| 7 | `planner.achieve` | `note_saved` не в state → производит `add_note` (`mod_notes`), needs `["note_text"]`; `note_text` уже в state → сразу `call_module` | — |
| 8 | `call_module` | `subprocess: python3 main.py add_note '{"note_text": "купить хлеб"}'`, cwd = `system/modules/notes/`, env `SMOS_MODULE_DATA=<abs>/user/module_data/mod_notes` | — |
| 9 | `mod_notes/main.py` | дописал строку, напечатал `{"status":"ok","data":{"note_saved":{"saved":{...},"total_notes":N,...}}}`, завершился | `user/module_data/mod_notes/notes.jsonl` |
| 10 | `planner.achieve` | `state.update(data)` → вернул `state["note_saved"]` | — |
| 11 | `_run_task` | `status="done"`, `result=<note_saved value>` → пишет `state.json` (финал) → `_emit_completion` | `system/core/tasks/.../state.json` (`done`); `system/outputstructurizer/queue/20260828_..._<hex>.json` |
| 12 | `outputstructurizer` | нашёл файл в `queue/` → `phrasing.render`: `phrases.json["note_saved"]` есть → случайный вариант, напр. «Записал.» (`source="template"`) → заявка в `audio/tasks/`; файл очереди удалён | `system/audio/tasks/20260828_..._<hex>.json` = `{"text": "Записал.", ...}` |
| 13 | `audio` | нашёл заявку → `speak("Записал.")` → `gtts` → MP3 → `gst-play-1.0` → **произнесено**; заявка удалена | — |

Плюс на каждом шаге — UDP-события в `logs/raw/<module>/events.jsonl`.

Если бы город/выражение/etc. в фразе не было — `achieve` на шаге 7
рекурсивно позвал бы модуль-производитель нужного ключа (как
`get_datetime`+`get_system_stats` для `journal_written`). Если бы модуль
вернул `error` — задача завершилась бы со `status: error`, в очередь
ушёл бы файл с `error`, `outputstructurizer` взял бы шаблон `_error` или
попросил GigaChat сформулировать «не смог: …».

---

## 11. Все рантайм-папки

Всё ниже — в `.gitignore` (папки держатся в репо через `.gitkeep`).
Генерируется при работе, не исходный код.

| Путь | Кто пишет | Что внутри |
|---|---|---|
| `logs/raw/<module>/events.jsonl` | `logs/listener` | сырой архив событий, append-only |
| `system/fwl/rvs/flags/utterance.wav` | `wake` (чит.+удал. `req`) | последняя записанная фраза |
| `system/fwl/rvs/flags/activity.flag` | `req` (чит. `wake`) | mtime = «было успешное распознавание» |
| `system/fwl/rvs/output/recognized.json` | `req` | текст последней фразы + метаданные STT |
| `system/fwl/rvs/debug_audio/` | `wake` | последняя запись для отладки порогов |
| `system/fwl/classifier/output/classified.json` | `classifier` | `{text, label, timestamp}` |
| `system/fwl/classifier/output/dataset.jsonl` | `classifier` | (фраза, метка от GigaChat) — обучающий набор |
| `system/fwl/classifier/output/shadow_state.json` | `classifier` | счётчики сравнений локальной vs облачной |
| `system/fwl/classifier/output/local_model.joblib(.bak)` | `train_local_model` | обученная локальная модель + один бэкап |
| `system/fwl/classifier/flags/promoted.flag` / `offline_only.flag` | `classifier` | маркеры режима дистилляции |
| `system/swl/output/dataset.jsonl` | `swl` | (фраза, goal, params) от GigaChat |
| `system/core/module_init/output/modules.json` / `graph.json` | `core` (на старте) | снимок реестра модулей и графа |
| `system/core/goals/*.json` | `swl` (чит.+удал. `core`) | очередь целей; `goals/rejected/` — неразобранные |
| `system/core/tasks/<task_id>/state.json` | `task_runner` | запись каждой задачи (`running`→`done`/`error`), не убирается |
| `system/outputstructurizer/queue/*.json` | `task_runner` (чит.+удал. `outputstructurizer`) | результаты задач; `rejected/`, `stale/` внутри |
| `system/outputstructurizer/output/dataset.jsonl` | `outputstructurizer` | (результат, фраза) от GigaChat-формулировщика |
| `system/audio/tasks/*.json` | `outputstructurizer` (чит.+удал. `audio`) | заявки на озвучку; `rejected/` внутри |
| `system/launcher/run/state.json` | `smos.py` | pid'ы запущенных процессов |
| `user/module_data/<name>/` | сам модуль (папку заводит `core` при скане) | личные данные модуля, формат на усмотрение модуля |

---

## 12. Состояние: что есть, чего нет

**Работает end-to-end:** голос → wake → запись → STT → классификация →
цель (SWL, GigaChat) → ядро (скан модулей, GOAP-план, поток на задачу,
многошаговые цепочки) → модуль → результат → фраза (шаблон / GigaChat /
fallback) → озвучка (gtts / spd-say). Запуск/остановка — `smos.py`.
Логи — `logs/`.

**Настоящие модули (5):** `mod_datetime`, `mod_sysinfo`, `mod_calc`,
`mod_notes`, `mod_journal`. **Тестовые фикстуры (5):** `clock`, `echo`,
`greeting`, `location`, `weather` (фиктивные данные, для проверки
сканера и планировщика).

**Спроектировано, не построено** (детали — в `*_design.md` и `IDEAS.md`):

| Что | Где дизайн |
|---|---|
| разговорная ветка (`label == "chat"`) | `project_smos_vision` (память) |
| поведение SWL при `goal: null` (нераспознанная команда) | `swl_design.md` |
| дистилляция SWL в локальную модель (сейчас только копится датасет) | `swl_design.md` |
| дистилляция формулировщика `outputstructurizer` в локальную модель | `outputstructurizer_design.md` |
| поле `speech` от модулей (дословная формулировка) — место в файле есть | `audio_design.md` |
| полный аудио-демон: приоритетная очередь, музыка, приглушение, будильник | `audio_design.md` |
| определение зависшего модуля по heartbeat (сейчас жёсткий `timeout=5`) | `manifest_design.md` |
| долгоживущие/сессионные модули (таймер, музыка) — флаг в манифесте, `system/core/sessions/` | `core_design.md` |
| флаг в манифесте «это пользовательская цель» (сейчас в каталог SWL попадают все `produces`, включая промежуточные) | `swl_design.md` |
| выбор между несколькими модулями, производящими один ключ (сейчас — первый; `cost` не используется) | `core_design.md` |
| параллельное разрешение независимых `needs` | `core_design.md` |
| аналитический слой поверх `logs/raw/` | `project_smos_logging_system` (память) |
| проактивность: напоминания, проверки-как-друг, ambient-звук | `project_smos_vision` (память) |
| механизм обновления системы | — |
| поменять местами SWL и классификатор | `swl/IDEA_swap_swl_and_classifier.md` |
| формулировка ответа через ветку chat целиком (один генератор языка) | `audio/IDEA_responder_v2_chat_branch.md` |

Нужно ли SMOS «настоящее» центральное ядро в изначально задуманном виде
(маршрутизация всех задач через ядро) — **открытый вопрос**. Текущее
ядро появилось только там, где реально понадобилась координация
(планирование многошаговых команд).

---

## 13. Куда смотреть за деталями

Дизайн-решения — в `*_design.md` рядом с кодом (журнал «почему так», не
спецификация):

| Файл | О чём |
|---|---|
| `system/core/core_design.md` | асинхронность ядра, поток на задачу, память задачи = файл, планировщик (backward chaining), отдача результата дальше, долгоживущие модули (сессии) |
| `system/core/module_init/manifest_design.md` | формат манифеста, протокол вызова модуля, хранение данных модуля, зависание модуля (heartbeat) |
| `system/core/how_core_works.md` | narrative по ядру (от 2026-08-26, до очереди результатов) |
| `system/swl/swl_design.md` | модуль vs системный процесс, GOAP без LLM, bootstrap SWL, дистилляция |
| `system/fwl/classifier/model_combination_design.md` | цикл `bootstrap → shadow → validating → offline` |
| `system/fwl/classifier/classifier_design.md`, `embedding_classifier_design.md` | подходы к классификатору |
| `system/outputstructurizer/outputstructurizer_design.md` | три слоя формулировки |
| `system/audio/audio_design.md` | полный аудио-демон, `v1: responder`, `outputstructurizer`/`audio` split |
| `logs/PROTOCOL.md` | как модулю слать события |
| `README.md` | обзор для нового читателя |
| `IDEAS.md` | список отложенных идей со ссылками |

Память ассистента (`~/.claude/.../memory/`, вне репо): `project_smos_vision`
(общее видение + уровни оффлайна), `project_smos_swl_design`,
`project_smos_classifier`, `project_smos_logging_system`,
`project_smos_launcher`, `user_smos_context`.
