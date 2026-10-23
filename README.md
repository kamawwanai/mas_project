# Поездка: бюджет и маршрут

Локальная инфраструктура для сравнения fixed workflow, single-agent и MAS с
координатором. API выключен в `configs/llm.toml`. Синтетический benchmark:
**17–18 октября 2026 года**, два взрослых, одна ночь. Готовность проверяется
командой `check_benchmark.py`; она не подтверждает исторические предложения.

Исходные `data/snapshot/*.json`, `data/scenarios/dev/dev_01.json` и ручной маршрут
`data/reference_plans/dev_01.json` сохранены побайтово. Строгая проверка исходного
dev_01 по-прежнему возвращает incomplete (код 3). В экспериментальной копии все
активные условия явно заданы как `synthetic_assumption`.

## Локальная проверка

Python 3.11+. Тесты, CSV/JSON и SVG не требуют внешних пакетов.

```powershell
python -B scripts/test_offline.py
python -B scripts/prepare_snapshot.py --check
python -B scripts/check_benchmark.py
python -B scripts/check_repository.py
python -B scripts/run_experiments.py --dry-run
python -B scripts/check_scenario.py --scenario dev_01 --scope full --data-dir data/benchmark --plan evaluation/benchmark/reference_plans/dev_01.json --mode benchmark_assumptions --json
```

Та же проверка с `--mode strict` сохраняет требования к реальным источникам.
Синтетические цены не становятся доказательством реальных расходов.

## Воспроизведение без API

```powershell
python -B scripts/build_synthetic_benchmark.py
python -B scripts/prepare_snapshot.py --check
python -B scripts/run_benchmark_mock.py --output reports/local/benchmark-mock
python -B scripts/run_benchmark_mock.py --output reports/local/benchmark-mock --resume
```

Генератор сверяет SHA256 оригиналов и дизайна, использует фиксированные правила
без случайности и сети. Он перезаписывает только производные benchmark-артефакты
и отдельные файлы `evaluation/benchmark/`. Для resume нужны неизменные входы и код;
после изменений следует выбрать новый каталог результатов.

Mock-цикл использует заранее заданные ответы ScriptedClient и эталоны. Он
проверяет проводку архитектур, перепланирование, независимую оценку, трассы,
CSV/JSON, SVG и возобновление. **Это не измерение качества LLM.**

## Компоненты

| Файлы | Назначение |
| --- | --- |
| `src/schemas.py`, `src/catalog.py` | Модели и загрузка |
| `src/checks/trip.py` | Полный маршрут, бюджет, расписание, отдых, ходьба, багаж и запись |
| `src/assumptions.py` | Явный контекст синтетических условий, связанный хешами с реестром |
| `src/synthetic_benchmark.py` | Подготовка, эталоны и gate всех 16 сценариев |
| `src/architectures/` | Три способа управления общей инструментальной средой |
| `src/tools.py` | Поиск, расчёт, проверка, ограниченные вопросы |
| `src/selection.py`, `src/evaluator.py` | Парето-отбор, различимость, полезность и сертификаты |
| `src/runtime.py`, `src/llm.py` | Общие лимиты и выключенный API-адаптер |
| `src/batch.py`, `src/reporting.py` | Контроль целостности, возобновление, отчёты |
| `data/benchmark/design.json`, `assumptions.json` | Публичные условия и реестр допущений |
| `evaluation/benchmark/` | Приватные профили, эталоны, альтернативы, сертификаты |

Планировщики получают публичный запрос, результаты общих инструментов и ответы
на заданные вопросы. Они не получают профили, веса, эталоны или конструктор
эталонов. MAS требует фактической проверки точного кандидата специалистом verification.

## Будущие эксперименты

`configs/experiment.toml` явно выбирает `benchmark_assumptions`. LLM остаётся
выключенной. Реальный запуск требует отдельного разрешения, точной модели,
проверенного прайсинга и денежных лимитов. Отчёты различают режим клиента
`test`/`real` и среды `strict`/`benchmark_assumptions`; смешивание запрещено.
Синтетическая готовность не означает готовность реальных туристических данных.

Подробнее: [среда](docs/synthetic_benchmark.md),
[протокол](docs/experiment_protocol.md), [OpenRouter](docs/openrouter.md),
[происхождение](docs/data_audit.md).
