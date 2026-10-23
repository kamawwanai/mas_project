# Локальный показ без API

1. `python -B scripts/prepare_snapshot.py --check` — проверка воспроизводимости.
2. `python -B scripts/check_benchmark.py` — gate всех 4 dev и 12 test.
3. Полный эталон dev_01:

```powershell
python -B scripts/check_scenario.py --scenario dev_01 --scope full --data-dir data/benchmark --plan evaluation/benchmark/reference_plans/dev_01.json --mode benchmark_assumptions --json
```

Ожидаются 45 событий, `exit_code=0`, стоимость 2 534 500 копеек и явный режим
benchmark_assumptions. Синтетическая цена не подтверждает историческую стоимость.
Повтор той же проверки с `--mode strict` должен вернуть incomplete, код 3.

4. `python -B scripts/run_benchmark_mock.py --output reports/local/demo-benchmark`.
5. Повтор с `--resume` проверяет возобновление без повторного исполнения.

В каталоге report будут CSV/JSON, SVG и выбранные трассы. Все результаты
oracle-scripted, test / synthetic; не представляют качество реальной LLM.
Отдельный `run_local_cycle.py` проверяет компактные фикстуры, включая ошибку
и сохранение прерванной серии. Для нового показа после изменений кода выберите
новый каталог результатов. API для показа не нужен и остаётся отключённым.
