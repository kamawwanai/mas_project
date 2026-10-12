#!/usr/bin/env python3
"""CLI for day-one trip scenario checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


# Stable UTF-8 output is especially important for JSON redirected on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.validator import validate_scenario  # noqa: E402


STATUS_RU = {
    "passed": "пройдено",
    "failed": "нарушение",
    "incomplete": "недостаточно данных",
    "skipped": "пропущено",
    "input_error": "ошибка входных данных",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Проверки первого дня для ручного плана поездки.")
    parser.add_argument("--scenario", required=True, help="ID сценария и имя JSON без расширения, например dev_01")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="корень набора данных (по умолчанию ./data)",
    )
    parser.add_argument("--json", action="store_true", help="вывести только машиночитаемый JSON")
    return parser


def _problem_line(problem: dict[str, object]) -> str:
    location = ""
    if problem.get("file"):
        location = str(problem["file"])
    if problem.get("path"):
        location = f"{location}:{problem['path']}" if location else str(problem["path"])
    suffix = f" [{location}]" if location else ""
    return f"{problem['code']}: {problem['message']}{suffix}"


def format_human(report: dict[str, object]) -> str:
    lines = [
        "Проверки первого дня",
        f"Сценарий: {report['scenario_id'] or 'не определён'}",
        f"Снимок: {report['snapshot_id'] or 'не определён'}",
        f"Итог: {STATUS_RU[report['overall_status']]} (код {report['exit_code']})",
        "",
        "Загруженные объекты:",
    ]
    for kind, count in report["loaded_counts"].items():
        lines.append(f"  {kind}: {count if count is not None else 'не загружен'}")
    lines.extend(
        [
            f"  events: {report['event_count'] if report['event_count'] is not None else 'не загружен'}",
            f"  корректных полных интервалов: {report['valid_complete_interval_count']}",
        ]
    )

    input_errors = report["input_errors"]
    if input_errors:
        lines.extend(["", "Ошибки входных данных:"])
        lines.extend(f"  - {_problem_line(item)}" for item in input_errors)

    lines.extend(["", "Результаты проверок:"])
    for check in report["checks"]:
        lines.append(f"  [{STATUS_RU[check['status']]}] {check['name']}: {check['message']}")
        for problem in check["violations"]:
            lines.append(f"    - {_problem_line(problem)}")
        if check["incomplete_data"]:
            for problem in check["incomplete_data"]:
                lines.append(f"    ? {_problem_line(problem)}")

    lines.extend(["", "Ещё не реализовано:"])
    lines.extend(f"  - {item}" for item in report["not_implemented"])
    lines.extend(
        [
            "",
            "Полная выполнимость поездки не оценивалась.",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_scenario(str(args.data_dir), args.scenario)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_human(payload))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
