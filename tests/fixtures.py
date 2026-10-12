"""Small synthetic datasets for validator tests (not real trip offers)."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any


def price(unit: str, amount: int | None = 10000) -> dict[str, Any]:
    return {
        "amount_kopecks": amount,
        "currency": "RUB",
        "price_unit": unit,
        "price_kind": "listed" if amount is not None else "unknown",
        "includes_mandatory_fees": True if amount is not None else None,
    }


def source() -> dict[str, Any]:
    return {
        "source_url": "https://example.test/source",
        "checked_at": "2026-10-01T10:00:00+03:00",
        "notes": "Синтетическая тестовая запись.",
        "additional_sources": [],
    }


def catalog(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "snapshot_id": "test_snapshot",
        "is_template": False,
        "timezone": "Europe/Moscow",
        "items": items,
    }


def event(
    event_id: str,
    event_type: str,
    reference_id: str,
    start_at: str | None,
    end_at: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": event_id,
        "day": 1,
        "type": event_type,
        "reference_id": reference_id,
        "label": f"Тест {event_id}",
        "start_at": start_at,
        "end_at": end_at,
    }
    if event_type in {"transport", "transfer", "visit", "meal"}:
        result["quantity"] = 1
    if event_type == "meal":
        result["meal_type"] = "lunch"
    return result


def make_dataset(events: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    src = source()
    transport = catalog(
        [
            {
                "id": "train_1",
                "name": "Тестовый поезд",
                "mode": "train",
                "direction": "outbound",
                "service_date": "2026-10-24",
                "from_station": {"id": "station_a", "name": None, "city": "А", "address": None},
                "to_station": {"id": "station_b", "name": "Б", "city": "Б", "address": None},
                "departure_at": "2026-10-24T09:00:00+03:00",
                "arrival_at": "2026-10-24T10:00:00+03:00",
                "seat_class": "seat",
                "price": price("adult_ticket"),
                "availability_status": "available",
                **src,
            }
        ]
    )
    stays = catalog(
        [
            {
                "id": "stay_1",
                "name": "Тестовый отель",
                "city": "Б",
                "address": "Адрес",
                "check_in_date": "2026-10-24",
                "check_out_date": "2026-10-25",
                "check_in_from": "14:00",
                "check_out_until": "12:00",
                "room_type": "double",
                "room_capacity_adults": 2,
                "requested_adults": 2,
                "requested_rooms": 1,
                "breakfast_included": False,
                "luggage_storage_available": True,
                "price": price("room_night"),
                "availability_status": "available",
                **src,
            }
        ]
    )
    hours = [
        {
            "date": "2026-10-24",
            "status": "open",
            "intervals": [
                {"start_at": "2026-10-24T08:00:00+03:00", "end_at": "2026-10-24T22:00:00+03:00"}
            ],
        }
    ]
    places = catalog(
        [
            {
                "id": "place_1",
                "name": "Тестовое место",
                "city": "Б",
                "address": "Адрес",
                "category": "museum",
                "recommended_duration_minutes": 60,
                "opening_hours": hours,
                "ticket_required": True,
                "booking_required": False,
                "price": price("adult_visit"),
                "availability_status": "available",
                **src,
            }
        ]
    )
    food = catalog(
        [
            {
                "id": "food_1",
                "name": "Тестовое кафе",
                "city": "Б",
                "address": "Адрес",
                "meal_types": ["lunch"],
                "recommended_duration_minutes": 45,
                "opening_hours": hours,
                "reservation_required": False,
                "price_basis": "Тест",
                "price": price("adult_meal"),
                **src,
            }
        ]
    )
    transfers = catalog(
        [
            {
                "id": "transfer_1",
                "from_id": "station_b",
                "to_id": "stay_1",
                "applicable_date": "2026-10-24",
                "mode": "walk",
                "duration_minutes": 15,
                "distance_meters": 1000,
                "duration_kind": "estimated",
                "price": price("person_trip", 0) | {"price_kind": "free"},
                **src,
            }
        ]
    )
    scenario = {
        "schema_version": "0.1.0",
        "snapshot_id": "test_snapshot",
        "is_template": False,
        "timezone": "Europe/Moscow",
        "id": "case",
        "origin_city": "А",
        "destination_city": "Б",
        "start_date": "2026-10-24",
        "end_date": "2026-10-25",
        "adults": 2,
        "nights": 1,
        "budget_kopecks": 100000,
        "currency": "RUB",
        "preferences": {"pace": "relaxed", "interests": []},
        "constraints": {
            "required_place_ids": ["place_1"],
            "max_walking_minutes_per_day": None,
            "min_departure_buffer_minutes": 30,
        },
        "notes": "Синтетический тест.",
    }
    if events is None:
        events = [
            event("e1", "transport", "train_1", "2026-10-24T09:00:00+03:00", "2026-10-24T10:00:00+03:00"),
            event("e2", "rest", "stay_1", "2026-10-24T10:00:00+03:00", "2026-10-25T07:00:00+03:00"),
            event("e3", "visit", "place_1", "2026-10-25T07:00:00+03:00", "2026-10-25T08:00:00+03:00"),
        ]
        events[2]["day"] = 2
    plan = {
        "schema_version": "0.1.0",
        "snapshot_id": "test_snapshot",
        "is_template": False,
        "timezone": "Europe/Moscow",
        "id": "plan_case",
        "scenario_id": "case",
        "selected_stay": {"reference_id": "stay_1", "rooms": 1, "nights": 1},
        "items": events,
        "total_cost_kopecks": None,
        "validation_status": "not_checked",
        "unresolved_items": [],
    }
    return {
        "transport": transport,
        "stays": stays,
        "places": places,
        "food": food,
        "transfers": transfers,
        "scenario": scenario,
        "plan": plan,
    }


def write_dataset(root: Path, data: dict[str, dict[str, Any]]) -> Path:
    (root / "snapshot").mkdir(parents=True)
    (root / "scenarios" / "dev").mkdir(parents=True)
    (root / "reference_plans").mkdir(parents=True)
    for kind in ("transport", "stays", "places", "food", "transfers"):
        (root / "snapshot" / f"{kind}.json").write_text(
            json.dumps(data[kind], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (root / "scenarios" / "dev" / "case.json").write_text(
        json.dumps(data["scenario"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "reference_plans" / "case.json").write_text(
        json.dumps(data["plan"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def clone(data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return deepcopy(data)
