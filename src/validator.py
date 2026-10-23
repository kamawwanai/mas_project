"""Day-one reference, interval, and overlap validation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .catalog import LoadedDataset, load_dataset
from .schemas import (
    CATALOG_KINDS,
    CheckResult,
    ManualPlan,
    PlanEvent,
    Problem,
    Station,
    ValidationReport,
)


NOT_IMPLEMENTED = [
    "расчёт бюджета и сверка total_cost_kopecks",
    "проверка часов работы мест и заведений",
    "проверка длительности и достаточности перемещений",
    "проверка времён поездов и доступности предложений",
    "проверка дат, вместимости и условий заселения/выселения/багажа",
    "проверка лимита ходьбы, буфера отправления и остальных ограничений",
    "оценка мягких предпочтений пользователя",
    "полная выполнимость поездки",
]


@dataclass
class _Indexes:
    occurrences: dict[str, dict[str, list[tuple[Any, str]]]]
    stations: dict[str, Station]
    station_occurrences: dict[str, list[tuple[Station, str]]]

    def unique(self, kind: str) -> dict[str, Any]:
        return {
            item_id: entries[0][0]
            for item_id, entries in self.occurrences.get(kind, {}).items()
            if len(entries) == 1
        }

    def has_unique(self, kind: str, item_id: str) -> bool:
        return len(self.occurrences.get(kind, {}).get(item_id, [])) == 1

    def has_any(self, kind: str, item_id: str) -> bool:
        return bool(self.occurrences.get(kind, {}).get(item_id))


def _status(violations: list[Problem], incomplete: list[Problem]) -> str:
    if violations:
        return "failed"
    if incomplete:
        return "incomplete"
    return "passed"


def _check(
    name: str,
    success_message: str,
    violations: list[Problem] | None = None,
    incomplete: list[Problem] | None = None,
    *,
    skipped_message: str | None = None,
) -> CheckResult:
    violations = violations or []
    incomplete = incomplete or []
    if skipped_message is not None:
        return CheckResult(name=name, status="skipped", message=skipped_message, incomplete_data=incomplete)
    status = _status(violations, incomplete)
    if status == "failed":
        message = f"Обнаружено нарушений: {len(violations)}."
    elif status == "incomplete":
        message = f"Проверена только часть данных; неизвестных/недоступных значений: {len(incomplete)}."
    else:
        message = success_message
    return CheckResult(name=name, status=status, message=message, violations=violations, incomplete_data=incomplete)


def _missing(dataset: LoadedDataset, names: list[str], code: str = "CHECK_DEPENDENCY_UNAVAILABLE") -> list[Problem]:
    result: list[Problem] = []
    for name in names:
        value = dataset.catalogs.get(name) if name in CATALOG_KINDS else getattr(dataset, name)
        if value is None:
            result.append(
                Problem(
                    code=code,
                    message=f"Проверка неполна: не загружена зависимость {name}.",
                    file=dataset.paths.get(name),
                    path="$",
                )
            )
    return result


def _build_indexes(dataset: LoadedDataset) -> _Indexes:
    occurrences: dict[str, dict[str, list[tuple[Any, str]]]] = {
        kind: defaultdict(list) for kind in CATALOG_KINDS
    }
    stations_raw: dict[str, list[tuple[Station, str]]] = defaultdict(list)
    for kind, document in dataset.catalogs.items():
        if document is None:
            continue
        for index, item in enumerate(document.items):
            occurrences[kind][item.id].append((item, f"{dataset.paths[kind]}:$.items[{index}]"))
            if kind == "transport":
                for side in ("from_station", "to_station"):
                    station = getattr(item, side)
                    if station.id is not None:
                        stations_raw[station.id].append((station, f"{dataset.paths[kind]}:$.items[{index}].{side}"))

    merged: dict[str, Station] = {}
    for station_id, entries in stations_raw.items():
        known: dict[str, str | None] = {"name": None, "city": None, "address": None}
        for station, _ in entries:
            for field_name in known:
                value = getattr(station, field_name)
                if known[field_name] is None and value is not None:
                    known[field_name] = value
        merged[station_id] = Station(id=station_id, **known)
    return _Indexes(occurrences=occurrences, stations=merged, station_occurrences=stations_raw)


def _identifier_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    violations: list[Problem] = []
    all_catalog_ids: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for kind, by_id in indexes.occurrences.items():
        for item_id, entries in by_id.items():
            for _, location in entries:
                all_catalog_ids[item_id].append((kind, location))
            if len(entries) > 1:
                violations.append(
                    Problem(
                        code="DUPLICATE_CATALOG_ID",
                        message=f"ID {item_id!r} повторяется в каталоге {kind}.",
                        file=dataset.paths[kind],
                        path="$.items",
                        object_id=item_id,
                    )
                )
    for item_id, entries in all_catalog_ids.items():
        kinds = {kind for kind, _ in entries}
        if len(kinds) > 1:
            violations.append(
                Problem(
                    code="CATALOG_ID_COLLISION",
                    message=f"ID {item_id!r} используется объектами разных категорий: {', '.join(sorted(kinds))}.",
                    path="$.items",
                    object_id=item_id,
                )
            )

    if dataset.plan is not None:
        seen: set[str] = set()
        reported: set[str] = set()
        for index, event in enumerate(dataset.plan.items):
            if event.id in seen and event.id not in reported:
                violations.append(
                    Problem(
                        code="DUPLICATE_EVENT_ID",
                        message=f"ID события {event.id!r} повторяется.",
                        file=dataset.paths["plan"],
                        path=f"$.items[{index}].id",
                        object_id=event.id,
                    )
                )
                reported.add(event.id)
            seen.add(event.id)

    incomplete = _missing(dataset, [*CATALOG_KINDS, "plan"])
    return _check(
        "identifier_uniqueness",
        "ID записей каталогов и событий уникальны.",
        violations,
        incomplete,
    )


def _station_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    missing = _missing(dataset, ["transport"])
    if missing:
        return _check(
            "station_consistency",
            "",
            incomplete=missing,
            skipped_message="Проверка станций пропущена: transport не загружен.",
        )

    violations: list[Problem] = []
    for station_id, entries in indexes.station_occurrences.items():
        for attribute in ("name", "city", "address"):
            values = {getattr(station, attribute) for station, _ in entries if getattr(station, attribute) is not None}
            if len(values) > 1:
                violations.append(
                    Problem(
                        code="STATION_CONFLICT",
                        message=(
                            f"Станция {station_id!r} имеет противоречащие известные значения поля {attribute}: "
                            f"{', '.join(sorted(repr(value) for value in values))}."
                        ),
                        file=dataset.paths["transport"],
                        path="$.items[*].from_station/to_station",
                        object_id=station_id,
                    )
                )
        object_kinds = [kind for kind, ids in indexes.occurrences.items() if station_id in ids]
        if object_kinds:
            violations.append(
                Problem(
                    code="STATION_OBJECT_ID_COLLISION",
                    message=f"ID станции {station_id!r} совпадает с ID объекта: {', '.join(sorted(object_kinds))}.",
                    file=dataset.paths["transport"],
                    path="$.items[*].from_station/to_station.id",
                    object_id=station_id,
                )
            )
    incomplete = _missing(dataset, [kind for kind in CATALOG_KINDS if kind != "transport"])
    return _check(
        "station_consistency",
        "Повторы станций согласованы; пересечений ID станций и объектов нет.",
        violations,
        incomplete,
    )


def _snapshot_check(dataset: LoadedDataset) -> CheckResult:
    documents: list[tuple[str, Any]] = [
        (kind, document) for kind, document in dataset.catalogs.items() if document is not None
    ]
    if dataset.scenario is not None:
        documents.append(("scenario", dataset.scenario))
    if dataset.plan is not None:
        documents.append(("plan", dataset.plan))
    grouped: dict[str, list[str]] = defaultdict(list)
    for name, document in documents:
        grouped[document.snapshot_id].append(name)
    violations: list[Problem] = []
    if len(grouped) > 1:
        details = "; ".join(f"{snapshot}: {', '.join(names)}" for snapshot, names in sorted(grouped.items()))
        violations.append(
            Problem(
                code="SNAPSHOT_ID_MISMATCH",
                message=f"Связанные файлы имеют разные snapshot_id ({details}).",
                path="$.snapshot_id",
            )
        )
    missing = _missing(dataset, [*CATALOG_KINDS, "scenario", "plan"])
    return _check(
        "snapshot_consistency",
        "Все семь файлов относятся к одному snapshot_id.",
        violations,
        missing,
        skipped_message="Проверка snapshot_id пропущена: нет загруженных документов." if not documents else None,
    )


def _scenario_link_check(dataset: LoadedDataset) -> CheckResult:
    missing = _missing(dataset, ["scenario", "plan"])
    if missing:
        return _check(
            "scenario_link",
            "",
            incomplete=missing,
            skipped_message="Связь плана со сценарием не проверена: зависимость не загружена.",
        )
    violations: list[Problem] = []
    if dataset.plan.scenario_id != dataset.scenario.id:
        violations.append(
            Problem(
                code="SCENARIO_ID_MISMATCH",
                message=f"plan.scenario_id={dataset.plan.scenario_id!r} не совпадает с scenario.id={dataset.scenario.id!r}.",
                file=dataset.paths["plan"],
                path="$.scenario_id",
                object_id=dataset.plan.scenario_id,
            )
        )
    return _check("scenario_link", "План ссылается на загруженный сценарий.", violations)


def _selected_stay_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    missing = _missing(dataset, ["stays", "plan"])
    if missing:
        return _check(
            "selected_stay_reference",
            "",
            incomplete=missing,
            skipped_message="Выбранное проживание не проверено: зависимость не загружена.",
        )
    stay_id = dataset.plan.selected_stay.reference_id
    violations: list[Problem] = []
    if not indexes.has_unique("stays", stay_id):
        code = "AMBIGUOUS_SELECTED_STAY" if indexes.has_any("stays", stay_id) else "MISSING_SELECTED_STAY"
        violations.append(
            Problem(
                code=code,
                message=f"Выбранное проживание {stay_id!r} отсутствует или неоднозначно в stays.",
                file=dataset.paths["plan"],
                path="$.selected_stay.reference_id",
                object_id=stay_id,
            )
        )
    return _check("selected_stay_reference", "Выбранное проживание существует в stays.", violations)


def _required_places_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    missing = _missing(dataset, ["scenario", "places"])
    if missing:
        return _check(
            "required_place_references",
            "",
            incomplete=missing,
            skipped_message="Обязательные места не проверены: зависимость не загружена.",
        )
    violations: list[Problem] = []
    for index, place_id in enumerate(dataset.scenario.constraints.required_place_ids):
        if not indexes.has_unique("places", place_id):
            violations.append(
                Problem(
                    code="INVALID_REQUIRED_PLACE_REFERENCE",
                    message=f"Обязательное место {place_id!r} отсутствует или неоднозначно в places.",
                    file=dataset.paths["scenario"],
                    path=f"$.constraints.required_place_ids[{index}]",
                    object_id=place_id,
                )
            )
    return _check("required_place_references", "Все обязательные места существуют в places.", violations)


def _transfer_endpoints_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    dependencies = ["transport", "stays", "places", "food", "transfers"]
    missing = _missing(dataset, dependencies)
    if missing:
        return _check(
            "transfer_endpoint_references",
            "",
            incomplete=missing,
            skipped_message="Концы перемещений не проверены полностью: каталог точек не загружен.",
        )
    valid_ids = set(indexes.stations)
    for kind in ("stays", "places", "food"):
        valid_ids.update(indexes.unique(kind))
    violations: list[Problem] = []
    for index, transfer in enumerate(dataset.catalogs["transfers"].items):
        for field_name in ("from_id", "to_id"):
            value = getattr(transfer, field_name)
            if value not in valid_ids:
                violations.append(
                    Problem(
                        code="INVALID_TRANSFER_ENDPOINT",
                        message=f"{transfer.id}.{field_name} ссылается на неизвестную или неоднозначную точку {value!r}.",
                        file=dataset.paths["transfers"],
                        path=f"$.items[{index}].{field_name}",
                        object_id=transfer.id,
                        related_ids=[value],
                    )
                )
    return _check("transfer_endpoint_references", "Все from_id/to_id перемещений ссылаются на известные точки.", violations)


def _event_reference_check(dataset: LoadedDataset, indexes: _Indexes) -> CheckResult:
    missing = _missing(dataset, [*CATALOG_KINDS, "plan"])
    if missing:
        return _check(
            "event_references",
            "",
            incomplete=missing,
            skipped_message="Ссылки событий не проверены полностью: зависимость не загружена.",
        )
    expected_kind = {"transport": "transport", "transfer": "transfers", "visit": "places", "meal": "food"}
    violations: list[Problem] = []
    for index, event in enumerate(dataset.plan.items):
        valid = False
        expected_label = ""
        if event.type in expected_kind:
            kind = expected_kind[event.type]
            valid = indexes.has_unique(kind, event.reference_id)
            expected_label = kind
        elif event.type in {"check_in", "check_out", "included_breakfast", "luggage_drop", "luggage_pickup"}:
            valid = event.reference_id == dataset.plan.selected_stay.reference_id and indexes.has_unique(
                "stays", event.reference_id
            )
            expected_label = "выбранное проживание"
        elif event.type in {"buffer", "rest"}:
            valid = event.reference_id in indexes.stations or any(
                indexes.has_unique(kind, event.reference_id) for kind in ("stays", "places", "food")
            )
            expected_label = "известная точка маршрута"
        if valid:
            continue

        other_kinds = [kind for kind in CATALOG_KINDS if indexes.has_any(kind, event.reference_id)]
        if event.reference_id in indexes.stations:
            other_kinds.append("station")
        code = "EVENT_REFERENCE_WRONG_TYPE" if other_kinds else "EVENT_REFERENCE_NOT_FOUND"
        message = (
            f"Событие {event.id!r} типа {event.type!r} должно ссылаться на {expected_label}; "
            f"получен ID {event.reference_id!r}"
        )
        if other_kinds:
            message += f" из {', '.join(sorted(other_kinds))}."
        else:
            message += ", которого нет в индексах."
        violations.append(
            Problem(
                code=code,
                message=message,
                file=dataset.paths["plan"],
                path=f"$.items[{index}].reference_id",
                object_id=event.id,
                related_ids=[event.reference_id],
            )
        )
    return _check("event_references", "Все события ссылаются на объекты допустимого типа.", violations)


def _interval_checks(dataset: LoadedDataset) -> tuple[CheckResult, CheckResult, int]:
    if dataset.plan is None:
        missing = _missing(dataset, ["plan"])
        interval = _check(
            "event_intervals",
            "",
            incomplete=missing,
            skipped_message="Интервалы событий не проверены: план не загружен.",
        )
        overlaps = _check(
            "event_overlaps",
            "",
            incomplete=missing,
            skipped_message="Пересечения не проверены: план не загружен.",
        )
        return interval, overlaps, 0

    plan = dataset.plan
    interval_violations: list[Problem] = []
    incomplete: list[Problem] = []
    valid: list[PlanEvent] = []
    invalid_full_ids: list[str] = []
    for index, event in enumerate(plan.items):
        if event.start_at is None or event.end_at is None:
            missing_fields = []
            if event.start_at is None:
                missing_fields.append("start_at")
            if event.end_at is None:
                missing_fields.append("end_at")
            incomplete.append(
                Problem(
                    code="INCOMPLETE_EVENT_INTERVAL",
                    message=f"У события {event.id!r} не заполнено: {', '.join(missing_fields)}.",
                    file=dataset.paths["plan"],
                    path=f"$.items[{index}]",
                    object_id=event.id,
                )
            )
        elif event.start_at >= event.end_at:
            invalid_full_ids.append(event.id)
            interval_violations.append(
                Problem(
                    code="INVALID_EVENT_INTERVAL",
                    message=f"У события {event.id!r} должно выполняться start_at < end_at.",
                    file=dataset.paths["plan"],
                    path=f"$.items[{index}]",
                    object_id=event.id,
                )
            )
        else:
            valid.append(event)

    interval_result = _check(
        "event_intervals",
        "Все события имеют корректные полные интервалы.",
        interval_violations,
        incomplete,
    )

    overlap_violations: list[Problem] = []
    for left_index, left in enumerate(valid):
        for right in valid[left_index + 1 :]:
            if left.start_at < right.end_at and right.start_at < left.end_at:
                overlap_violations.append(
                    Problem(
                        code="EVENT_OVERLAP",
                        message=f"События {left.id!r} и {right.id!r} пересекаются.",
                        file=dataset.paths["plan"],
                        path="$.items",
                        object_id=left.id,
                        related_ids=[left.id, right.id],
                    )
                )
    overlap_incomplete = list(incomplete)
    for event_id in invalid_full_ids:
        overlap_incomplete.append(
            Problem(
                code="INVALID_INTERVAL_EXCLUDED_FROM_OVERLAPS",
                message=f"Событие {event_id!r} исключено из проверки пересечений из-за неверного интервала.",
                file=dataset.paths["plan"],
                path="$.items",
                object_id=event_id,
            )
        )
    overlap_result = _check(
        "event_overlaps",
        "Пересечений между всеми событиями нет.",
        overlap_violations,
        overlap_incomplete,
    )
    return interval_result, overlap_result, len(valid)


def validate_plan(dataset: LoadedDataset) -> ValidationReport:
    """Validate a loaded dataset and return a structured result without printing."""

    indexes = _build_indexes(dataset)
    interval_result, overlap_result, valid_interval_count = _interval_checks(dataset)
    checks = [
        _snapshot_check(dataset),
        _identifier_check(dataset, indexes),
        _station_check(dataset, indexes),
        _scenario_link_check(dataset),
        _selected_stay_check(dataset, indexes),
        _required_places_check(dataset, indexes),
        _transfer_endpoints_check(dataset, indexes),
        _event_reference_check(dataset, indexes),
        interval_result,
        overlap_result,
    ]

    documents: dict[str, Any | None] = dict(dataset.catalogs)
    documents["scenario"] = dataset.scenario
    documents["plan"] = dataset.plan
    loaded_counts = {
        kind: (len(document.items) if document is not None else None)
        for kind, document in dataset.catalogs.items()
    }
    snapshot_values = {document.snapshot_id for document in documents.values() if document is not None}
    snapshot_id = next(iter(snapshot_values)) if len(snapshot_values) == 1 else None
    return ValidationReport(
        scope="day1",
        scenario_id=(dataset.scenario.id if dataset.scenario is not None else dataset.scenario_name),
        snapshot_id=snapshot_id,
        is_template={name: (document.is_template if document is not None else None) for name, document in documents.items()},
        loaded_counts=loaded_counts,
        event_count=len(dataset.plan.items) if dataset.plan is not None else None,
        valid_complete_interval_count=valid_interval_count,
        checks=checks,
        input_errors=list(dataset.input_errors),
        not_implemented=list(NOT_IMPLEMENTED),
    )


def validate_scenario(data_dir: str, scenario_name: str) -> ValidationReport:
    """Convenience function: load all files and run day-one validation."""

    return validate_plan(load_dataset(data_dir, scenario_name))
