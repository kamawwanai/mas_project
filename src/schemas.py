"""Typed schemas and strict parsers for the project's JSON contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import re
import math
from typing import Any, Callable, Literal, TypeVar
from .evidence import local_zone


SUPPORTED_SCHEMA_VERSIONS = {"0.1.0", "0.2.0"}
CATALOG_KINDS = ("transport", "stays", "places", "food", "transfers")


class SchemaError(ValueError):
    """A structural, type, or format error at a JSON path."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(path, f"ожидался объект, получено {_type_name(value)}")
    return value


def _required(data: dict[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise SchemaError(f"{path}.{key}", "отсутствует обязательное поле")
    return data[key]


def _only_keys(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unexpected = sorted(set(data) - allowed)
    if unexpected:
        raise SchemaError(f"{path}.{unexpected[0]}", "неизвестное поле")


def _string(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise SchemaError(path, f"ожидалась строка, получено {_type_name(value)}")
    if not value:
        raise SchemaError(path, "строка не должна быть пустой")
    return value


def _boolean(value: Any, path: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise SchemaError(path, f"ожидалось логическое значение, получено {_type_name(value)}")
    return value


def _integer(
    value: Any,
    path: str,
    *,
    nullable: bool = False,
    minimum: int | None = None,
) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise SchemaError(path, f"ожидалось целое число, получено {_type_name(value)}")
    if minimum is not None and value < minimum:
        raise SchemaError(path, f"значение должно быть не меньше {minimum}")
    return value


def _enum(value: Any, path: str, values: set[str], *, nullable: bool = False) -> str | None:
    parsed = _string(value, path, nullable=nullable)
    if parsed is not None and parsed not in values:
        raise SchemaError(path, f"неизвестное значение {parsed!r}; допустимы: {', '.join(sorted(values))}")
    return parsed


def _date(value: Any, path: str) -> date:
    parsed = _string(value, path)
    try:
        return date.fromisoformat(parsed)
    except ValueError as exc:
        raise SchemaError(path, "ожидалась дата ISO 8601 в формате YYYY-MM-DD") from exc


def _timestamp(value: Any, path: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    parsed = _string(value, path)
    try:
        result = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(path, "ожидалась временная отметка ISO 8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SchemaError(path, "полная временная отметка должна содержать часовой пояс")
    return result


_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _local_time(value: Any, path: str, *, nullable: bool = False) -> str | None:
    parsed = _string(value, path, nullable=nullable)
    if parsed is not None and not _LOCAL_TIME_RE.fullmatch(parsed):
        raise SchemaError(path, "ожидалось локальное время в формате HH:MM")
    return parsed


T = TypeVar("T")


def _list(
    value: Any,
    path: str,
    parser: Callable[[Any, str], T],
    *,
    nullable: bool = False,
    nonempty: bool = False,
) -> list[T] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, list):
        raise SchemaError(path, f"ожидался список, получено {_type_name(value)}")
    if nonempty and not value:
        raise SchemaError(path, "список не должен быть пустым")
    return [parser(item, f"{path}[{index}]") for index, item in enumerate(value)]


@dataclass(frozen=True)
class Price:
    amount_kopecks: int | None
    currency: str
    price_unit: str
    price_kind: Literal["unknown", "listed", "estimated", "free", "indicative_test_value", "synthetic_assumption"]
    includes_mandatory_fees: bool | None
    group_size_adults: int | None = None


PRICE_UNITS = {"adult_ticket", "room_night", "adult_visit", "adult_meal", "person_trip", "vehicle_trip", "group_storage", "group_trip"}


def parse_price(value: Any, path: str) -> Price:
    data = _object(value, path)
    keys = {"amount_kopecks", "currency", "price_unit", "price_kind", "includes_mandatory_fees", "group_size_adults"}
    _only_keys(data, keys, path)
    amount = _integer(_required(data, "amount_kopecks", path), f"{path}.amount_kopecks", nullable=True, minimum=0)
    kind = _enum(_required(data, "price_kind", path), f"{path}.price_kind", {"unknown", "listed", "estimated", "free", "indicative_test_value", "synthetic_assumption"})
    if kind == "free" and amount != 0:
        raise SchemaError(f"{path}.amount_kopecks", "для price_kind='free' сумма должна быть равна 0")
    return Price(
        amount_kopecks=amount,
        currency=_string(_required(data, "currency", path), f"{path}.currency"),
        price_unit=_enum(_required(data, "price_unit", path), f"{path}.price_unit", PRICE_UNITS),
        price_kind=kind,
        group_size_adults=_integer(data.get("group_size_adults"), f"{path}.group_size_adults", nullable=True, minimum=1),
        includes_mandatory_fees=_boolean(
            _required(data, "includes_mandatory_fees", path),
            f"{path}.includes_mandatory_fees",
            nullable=True,
        ),
    )


@dataclass(frozen=True)
class AdditionalSource:
    source_url: str
    checked_at: datetime | None
    supports: list[str]


def parse_additional_source(value: Any, path: str) -> AdditionalSource:
    data = _object(value, path)
    keys = {"source_url", "checked_at", "supports"}
    _only_keys(data, keys, path)
    return AdditionalSource(
        source_url=_string(_required(data, "source_url", path), f"{path}.source_url"),
        checked_at=_timestamp(_required(data, "checked_at", path), f"{path}.checked_at", nullable=True),
        supports=_list(_required(data, "supports", path), f"{path}.supports", lambda v, p: _string(v, p)),
    )


@dataclass(frozen=True)
class Review:
    source_url: str
    checked_at: datetime | None
    summary: str
    rating: float | None
    scale: float | None


def parse_review(value: Any, path: str) -> Review:
    data = _object(value, path)
    _only_keys(data, {"source_url", "checked_at", "summary", "rating", "scale"}, path)
    rating, scale = _required(data, "rating", path), _required(data, "scale", path)
    if (rating is None) != (scale is None):
        raise SchemaError(path, "rating and scale must both be known or null")
    if rating is not None:
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in (rating, scale)) or scale <= 0 or not 0 <= rating <= scale:
            raise SchemaError(path, "invalid review rating/scale")
    return Review(_string(_required(data, "source_url", path), f"{path}.source_url"),
                  _timestamp(_required(data, "checked_at", path), f"{path}.checked_at", nullable=True),
                  _string(_required(data, "summary", path), f"{path}.summary"), rating, scale)


@dataclass(frozen=True)
class SourceFields:
    source_url: str | None
    checked_at: datetime | None
    notes: str | None
    additional_sources: list[AdditionalSource]
    verified_fields: list[str] = field(default_factory=list)
    features: dict[str, int] = field(default_factory=dict)
    reviews: list[Review] = field(default_factory=list)
    original_fields: dict[str, Any] = field(default_factory=dict)


def _parse_source(data: dict[str, Any], path: str) -> SourceFields:
    return SourceFields(
        source_url=_string(_required(data, "source_url", path), f"{path}.source_url", nullable=True),
        checked_at=_timestamp(_required(data, "checked_at", path), f"{path}.checked_at", nullable=True),
        notes=_string(_required(data, "notes", path), f"{path}.notes", nullable=True),
        additional_sources=_list(
            _required(data, "additional_sources", path),
            f"{path}.additional_sources",
            parse_additional_source,
        ),
        verified_fields=_list(data.get("verified_fields", []), f"{path}.verified_fields", lambda v, p: _string(v, p)),
        features=_parse_features(data.get("features", {}), f"{path}.features"),
        reviews=_list(data.get("reviews", []), f"{path}.reviews", parse_review),
        original_fields=_object(data.get("original_fields", {}), f"{path}.original_fields"),
    )


def _parse_features(value: Any, path: str) -> dict[str, int]:
    data = _object(value, path)
    _only_keys(data, {"culture", "food", "comfort", "quiet", "location"}, path)
    result = {k: _integer(v, f"{path}.{k}", minimum=0) for k, v in data.items()}
    if any(v > 100 for v in result.values()):
        raise SchemaError(path, "feature values must be between 0 and 100")
    return result


@dataclass(frozen=True)
class OpeningInterval:
    start_at: datetime
    end_at: datetime


def parse_opening_interval(value: Any, path: str) -> OpeningInterval:
    data = _object(value, path)
    _only_keys(data, {"start_at", "end_at"}, path)
    start = _timestamp(_required(data, "start_at", path), f"{path}.start_at")
    end = _timestamp(_required(data, "end_at", path), f"{path}.end_at")
    if start >= end:
        raise SchemaError(path, "интервал часов работы должен иметь start_at < end_at")
    return OpeningInterval(start_at=start, end_at=end)


@dataclass(frozen=True)
class OpeningHours:
    date: date
    status: Literal["unknown", "closed", "open"]
    intervals: list[OpeningInterval] | None


def parse_opening_hours(value: Any, path: str) -> OpeningHours:
    data = _object(value, path)
    _only_keys(data, {"date", "status", "intervals"}, path)
    status = _enum(_required(data, "status", path), f"{path}.status", {"unknown", "closed", "open"})
    intervals = _list(
        _required(data, "intervals", path),
        f"{path}.intervals",
        parse_opening_interval,
        nullable=True,
    )
    if status == "unknown" and intervals is not None:
        raise SchemaError(f"{path}.intervals", "для status='unknown' ожидается null")
    if status == "closed" and intervals != []:
        raise SchemaError(f"{path}.intervals", "для status='closed' ожидается пустой список")
    if status == "open" and not intervals:
        raise SchemaError(f"{path}.intervals", "для status='open' нужен непустой список интервалов")
    return OpeningHours(date=_date(_required(data, "date", path), f"{path}.date"), status=status, intervals=intervals)


@dataclass(frozen=True)
class Station:
    id: str | None
    name: str | None
    city: str | None
    address: str | None


def parse_station(value: Any, path: str) -> Station:
    data = _object(value, path)
    keys = {"id", "name", "city", "address"}
    _only_keys(data, keys, path)
    return Station(
        id=_string(_required(data, "id", path), f"{path}.id", nullable=True),
        name=_string(_required(data, "name", path), f"{path}.name", nullable=True),
        city=_string(_required(data, "city", path), f"{path}.city", nullable=True),
        address=_string(_required(data, "address", path), f"{path}.address", nullable=True),
    )


SOURCE_KEYS = {"source_url", "checked_at", "notes", "additional_sources", "verified_fields", "features", "reviews", "original_fields"}


@dataclass(frozen=True)
class TransportItem:
    id: str
    name: str | None
    mode: Literal["train"]
    direction: Literal["outbound", "return"]
    service_date: date
    from_station: Station
    to_station: Station
    departure_at: datetime | None
    arrival_at: datetime | None
    seat_class: str | None
    price: Price
    availability_status: Literal["unknown", "available", "unavailable"]
    source: SourceFields


def parse_transport(value: Any, path: str) -> TransportItem:
    data = _object(value, path)
    keys = {
        "id", "name", "mode", "direction", "service_date", "from_station", "to_station",
        "departure_at", "arrival_at", "seat_class", "price", "availability_status",
    } | SOURCE_KEYS
    _only_keys(data, keys, path)
    return TransportItem(
        id=_string(_required(data, "id", path), f"{path}.id"),
        name=_string(_required(data, "name", path), f"{path}.name", nullable=True),
        mode=_enum(_required(data, "mode", path), f"{path}.mode", {"train"}),
        direction=_enum(_required(data, "direction", path), f"{path}.direction", {"outbound", "return"}),
        service_date=_date(_required(data, "service_date", path), f"{path}.service_date"),
        from_station=parse_station(_required(data, "from_station", path), f"{path}.from_station"),
        to_station=parse_station(_required(data, "to_station", path), f"{path}.to_station"),
        departure_at=_timestamp(_required(data, "departure_at", path), f"{path}.departure_at", nullable=True),
        arrival_at=_timestamp(_required(data, "arrival_at", path), f"{path}.arrival_at", nullable=True),
        seat_class=_string(_required(data, "seat_class", path), f"{path}.seat_class", nullable=True),
        price=parse_price(_required(data, "price", path), f"{path}.price"),
        availability_status=_enum(
            _required(data, "availability_status", path),
            f"{path}.availability_status",
            {"unknown", "available", "unavailable"},
        ),
        source=_parse_source(data, path),
    )


@dataclass(frozen=True)
class StayItem:
    id: str
    name: str | None
    city: str
    address: str | None
    check_in_date: date
    check_out_date: date
    check_in_from: str | None
    check_out_until: str | None
    room_type: str | None
    room_capacity_adults: int | None
    requested_adults: int
    requested_rooms: int
    breakfast_included: bool | None
    luggage_storage_available: bool | None
    price: Price
    availability_status: Literal["unknown", "available", "unavailable"]
    source: SourceFields
    luggage_price: Price | None = None
    breakfast_hours: list[OpeningHours] = field(default_factory=list)


def parse_stay(value: Any, path: str) -> StayItem:
    data = _object(value, path)
    keys = {
        "id", "name", "city", "address", "check_in_date", "check_out_date", "check_in_from",
        "check_out_until", "room_type", "room_capacity_adults", "requested_adults", "requested_rooms",
        "breakfast_included", "luggage_storage_available", "price", "availability_status",
    } | SOURCE_KEYS | {"luggage_price", "breakfast_hours"}
    _only_keys(data, keys, path)
    return StayItem(
        id=_string(_required(data, "id", path), f"{path}.id"),
        name=_string(_required(data, "name", path), f"{path}.name", nullable=True),
        city=_string(_required(data, "city", path), f"{path}.city"),
        address=_string(_required(data, "address", path), f"{path}.address", nullable=True),
        check_in_date=_date(_required(data, "check_in_date", path), f"{path}.check_in_date"),
        check_out_date=_date(_required(data, "check_out_date", path), f"{path}.check_out_date"),
        check_in_from=_local_time(_required(data, "check_in_from", path), f"{path}.check_in_from", nullable=True),
        check_out_until=_local_time(_required(data, "check_out_until", path), f"{path}.check_out_until", nullable=True),
        room_type=_string(_required(data, "room_type", path), f"{path}.room_type", nullable=True),
        room_capacity_adults=_integer(
            _required(data, "room_capacity_adults", path), f"{path}.room_capacity_adults", nullable=True, minimum=1
        ),
        requested_adults=_integer(_required(data, "requested_adults", path), f"{path}.requested_adults", minimum=1),
        requested_rooms=_integer(_required(data, "requested_rooms", path), f"{path}.requested_rooms", minimum=1),
        breakfast_included=_boolean(
            _required(data, "breakfast_included", path), f"{path}.breakfast_included", nullable=True
        ),
        luggage_storage_available=_boolean(
            _required(data, "luggage_storage_available", path),
            f"{path}.luggage_storage_available",
            nullable=True,
        ),
        price=parse_price(_required(data, "price", path), f"{path}.price"),
        availability_status=_enum(
            _required(data, "availability_status", path),
            f"{path}.availability_status",
            {"unknown", "available", "unavailable"},
        ),
        source=_parse_source(data, path),
        luggage_price=parse_price(data["luggage_price"], f"{path}.luggage_price") if data.get("luggage_price") is not None else None,
        breakfast_hours=_list(data.get("breakfast_hours", []), f"{path}.breakfast_hours", parse_opening_hours),
    )


@dataclass(frozen=True)
class BookingConfirmation:
    id: str
    start_at: datetime
    end_at: datetime
    adults: int
    source_url: str | None
    checked_at: datetime | None
    assumption_id: str | None = None


def parse_booking(value, path):
    data = _object(value, path)
    _only_keys(data, {"id", "start_at", "end_at", "adults", "source_url", "checked_at", "assumption_id"}, path)
    interval = parse_opening_interval({k: _required(data, k, path) for k in ("start_at", "end_at")}, path)
    return BookingConfirmation(_string(_required(data, "id", path), path+".id"),
        interval.start_at, interval.end_at,
        _integer(_required(data, "adults", path), path+".adults", minimum=1),
        _string(_required(data, "source_url", path), path+".source_url", nullable=True),
        _timestamp(_required(data, "checked_at", path), path+".checked_at", nullable=True),
        _string(data.get("assumption_id"), path+".assumption_id", nullable=True))


@dataclass(frozen=True)
class PlaceItem:
    id: str
    name: str | None
    city: str
    address: str | None
    category: str | None
    recommended_duration_minutes: int | None
    opening_hours: list[OpeningHours]
    ticket_required: bool | None
    booking_required: bool | None
    price: Price
    availability_status: Literal["unknown", "available", "unavailable"]
    source: SourceFields
    booking_confirmations: list[BookingConfirmation] = field(default_factory=list)


def parse_place(value: Any, path: str) -> PlaceItem:
    data = _object(value, path)
    keys = {
        "id", "name", "city", "address", "category", "recommended_duration_minutes", "opening_hours",
        "ticket_required", "booking_required", "price", "availability_status",
    } | SOURCE_KEYS | {"booking_confirmations"}
    _only_keys(data, keys, path)
    return PlaceItem(
        id=_string(_required(data, "id", path), f"{path}.id"),
        name=_string(_required(data, "name", path), f"{path}.name", nullable=True),
        city=_string(_required(data, "city", path), f"{path}.city"),
        address=_string(_required(data, "address", path), f"{path}.address", nullable=True),
        category=_string(_required(data, "category", path), f"{path}.category", nullable=True),
        recommended_duration_minutes=_integer(
            _required(data, "recommended_duration_minutes", path),
            f"{path}.recommended_duration_minutes",
            nullable=True,
            minimum=0,
        ),
        opening_hours=_list(
            _required(data, "opening_hours", path), f"{path}.opening_hours", parse_opening_hours
        ),
        ticket_required=_boolean(_required(data, "ticket_required", path), f"{path}.ticket_required", nullable=True),
        booking_required=_boolean(_required(data, "booking_required", path), f"{path}.booking_required", nullable=True),
        price=parse_price(_required(data, "price", path), f"{path}.price"),
        availability_status=_enum(
            _required(data, "availability_status", path),
            f"{path}.availability_status",
            {"unknown", "available", "unavailable"},
        ),
        source=_parse_source(data, path),
        booking_confirmations=_list(data.get("booking_confirmations", []), path+".booking_confirmations", parse_booking),
    )


@dataclass(frozen=True)
class FoodItem:
    id: str
    name: str | None
    city: str
    address: str | None
    meal_types: list[Literal["breakfast", "lunch", "dinner", "snack"]] | None
    recommended_duration_minutes: int | None
    opening_hours: list[OpeningHours]
    reservation_required: bool | None
    price_basis: str | None
    price: Price
    source: SourceFields
    booking_confirmations: list[BookingConfirmation] = field(default_factory=list)


def parse_food(value: Any, path: str) -> FoodItem:
    data = _object(value, path)
    keys = {
        "id", "name", "city", "address", "meal_types", "recommended_duration_minutes", "opening_hours",
        "reservation_required", "price_basis", "price",
    } | SOURCE_KEYS | {"booking_confirmations"}
    _only_keys(data, keys, path)
    return FoodItem(
        id=_string(_required(data, "id", path), f"{path}.id"),
        name=_string(_required(data, "name", path), f"{path}.name", nullable=True),
        city=_string(_required(data, "city", path), f"{path}.city"),
        address=_string(_required(data, "address", path), f"{path}.address", nullable=True),
        meal_types=_list(
            _required(data, "meal_types", path),
            f"{path}.meal_types",
            lambda v, p: _enum(v, p, {"breakfast", "lunch", "dinner", "snack"}),
            nullable=True,
        ),
        recommended_duration_minutes=_integer(
            _required(data, "recommended_duration_minutes", path),
            f"{path}.recommended_duration_minutes",
            nullable=True,
            minimum=0,
        ),
        opening_hours=_list(
            _required(data, "opening_hours", path), f"{path}.opening_hours", parse_opening_hours
        ),
        reservation_required=_boolean(
            _required(data, "reservation_required", path), f"{path}.reservation_required", nullable=True
        ),
        price_basis=_string(_required(data, "price_basis", path), f"{path}.price_basis", nullable=True),
        price=parse_price(_required(data, "price", path), f"{path}.price"),
        source=_parse_source(data, path),
        booking_confirmations=_list(data.get("booking_confirmations", []), path+".booking_confirmations", parse_booking),
    )


@dataclass(frozen=True)
class TransferItem:
    id: str
    from_id: str
    to_id: str
    applicable_date: date
    mode: Literal["walk", "public_transport", "taxi"] | None
    duration_minutes: int | None
    distance_meters: int | None
    duration_kind: Literal["unknown", "estimated", "timetable", "measured"]
    price: Price
    source: SourceFields
    walking_minutes: int | None = None
    vehicle_capacity_adults: int | None = None
    departure_windows: list[OpeningInterval] | None = None


def parse_transfer(value: Any, path: str) -> TransferItem:
    data = _object(value, path)
    keys = {
        "id", "from_id", "to_id", "applicable_date", "mode", "duration_minutes", "distance_meters",
        "duration_kind", "price",
    } | SOURCE_KEYS | {"walking_minutes", "vehicle_capacity_adults", "departure_windows"}
    _only_keys(data, keys, path)
    return TransferItem(
        id=_string(_required(data, "id", path), f"{path}.id"),
        from_id=_string(_required(data, "from_id", path), f"{path}.from_id"),
        to_id=_string(_required(data, "to_id", path), f"{path}.to_id"),
        applicable_date=_date(_required(data, "applicable_date", path), f"{path}.applicable_date"),
        mode=_enum(
            _required(data, "mode", path), f"{path}.mode", {"walk", "public_transport", "taxi"}, nullable=True
        ),
        duration_minutes=_integer(
            _required(data, "duration_minutes", path), f"{path}.duration_minutes", nullable=True, minimum=0
        ),
        distance_meters=_integer(
            _required(data, "distance_meters", path), f"{path}.distance_meters", nullable=True, minimum=0
        ),
        duration_kind=_enum(
            _required(data, "duration_kind", path),
            f"{path}.duration_kind",
            {"unknown", "estimated", "timetable", "measured"},
        ),
        price=parse_price(_required(data, "price", path), f"{path}.price"),
        source=_parse_source(data, path),
        departure_windows=_list(data.get("departure_windows"), path+".departure_windows", parse_opening_interval, nullable=True),
        walking_minutes=_integer(data.get("walking_minutes"), f"{path}.walking_minutes", nullable=True, minimum=0),
        vehicle_capacity_adults=_integer(data.get("vehicle_capacity_adults"), f"{path}.vehicle_capacity_adults", nullable=True, minimum=1),
    )


ITEM_PARSERS: dict[str, Callable[[Any, str], Any]] = {
    "transport": parse_transport,
    "stays": parse_stay,
    "places": parse_place,
    "food": parse_food,
    "transfers": parse_transfer,
}


@dataclass(frozen=True)
class CatalogDocument:
    schema_version: str
    snapshot_id: str
    is_template: bool
    timezone: str
    kind: str
    items: list[Any]


def _base(data: dict[str, Any], path: str) -> tuple[str, str, bool, str]:
    version = _string(_required(data, "schema_version", path), f"{path}.schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaError(
            f"{path}.schema_version",
            f"неподдерживаемая версия {version!r}; поддерживается: {', '.join(sorted(SUPPORTED_SCHEMA_VERSIONS))}",
        )
    zone_name = _string(_required(data, "timezone", path), f"{path}.timezone")
    try:
        local_zone(zone_name)
    except ValueError as exc:
        raise SchemaError(f"{path}.timezone", str(exc)) from exc
    return (
        version,
        _string(_required(data, "snapshot_id", path), f"{path}.snapshot_id"),
        _boolean(_required(data, "is_template", path), f"{path}.is_template"),
        zone_name,
    )


def parse_catalog(value: Any, kind: str, path: str = "$") -> CatalogDocument:
    if kind not in ITEM_PARSERS:
        raise ValueError(f"Unknown catalog kind: {kind}")
    data = _object(value, path)
    _only_keys(data, {"schema_version", "snapshot_id", "is_template", "timezone", "items"}, path)
    version, snapshot, template, timezone = _base(data, path)
    return CatalogDocument(
        schema_version=version,
        snapshot_id=snapshot,
        is_template=template,
        timezone=timezone,
        kind=kind,
        items=_list(_required(data, "items", path), f"{path}.items", ITEM_PARSERS[kind]),
    )


@dataclass(frozen=True)
class Preferences:
    pace: str
    interests: list[str]


@dataclass(frozen=True)
class Constraints:
    required_place_ids: list[str]
    max_walking_minutes_per_day: int | None
    min_departure_buffer_minutes: int
    min_rest_minutes_per_night: int = 0
    min_visit_minutes_per_day: int = 0
    required_meals: list[str] = field(default_factory=list)
    luggage_required: bool = False
    activities_on_both_dates: bool = False
    meals_on_both_dates: bool = False
    rest_required: bool = False


@dataclass(frozen=True)
class Scenario:
    schema_version: str
    snapshot_id: str
    is_template: bool
    timezone: str
    id: str
    origin_city: str
    destination_city: str
    start_date: date
    end_date: date
    adults: int
    nights: int
    budget_kopecks: int | None
    currency: str
    preferences: Preferences
    constraints: Constraints
    notes: str | None


def parse_preferences(value: Any, path: str) -> Preferences:
    data = _object(value, path)
    _only_keys(data, {"pace", "interests"}, path)
    return Preferences(
        pace=_string(_required(data, "pace", path), f"{path}.pace"),
        interests=_list(_required(data, "interests", path), f"{path}.interests", lambda v, p: _string(v, p)),
    )


def parse_constraints(value: Any, path: str) -> Constraints:
    data = _object(value, path)
    keys = {"required_place_ids", "max_walking_minutes_per_day", "min_departure_buffer_minutes", "min_rest_minutes_per_night", "min_visit_minutes_per_day", "required_meals", "luggage_required", "activities_on_both_dates", "meals_on_both_dates", "rest_required"}
    _only_keys(data, keys, path)
    return Constraints(
        min_rest_minutes_per_night=_integer(data.get("min_rest_minutes_per_night", 0), f"{path}.min_rest_minutes_per_night", minimum=0),
        min_visit_minutes_per_day=_integer(data.get("min_visit_minutes_per_day", 0), f"{path}.min_visit_minutes_per_day", minimum=0),
        required_meals=_list(data.get("required_meals", []), f"{path}.required_meals", lambda v, p: _enum(v, p, {"breakfast", "lunch", "dinner"})),
        luggage_required=_boolean(data.get("luggage_required", False), f"{path}.luggage_required"),
        activities_on_both_dates=_boolean(data.get("activities_on_both_dates", False), f"{path}.activities_on_both_dates"),
        meals_on_both_dates=_boolean(data.get("meals_on_both_dates", False), f"{path}.meals_on_both_dates"),
        rest_required=_boolean(data.get("rest_required", False), f"{path}.rest_required"),
        required_place_ids=_list(
            _required(data, "required_place_ids", path),
            f"{path}.required_place_ids",
            lambda v, p: _string(v, p),
        ),
        max_walking_minutes_per_day=_integer(
            _required(data, "max_walking_minutes_per_day", path),
            f"{path}.max_walking_minutes_per_day",
            nullable=True,
            minimum=0,
        ),
        min_departure_buffer_minutes=_integer(
            _required(data, "min_departure_buffer_minutes", path),
            f"{path}.min_departure_buffer_minutes",
            minimum=0,
        ),
    )


def parse_scenario(value: Any, path: str = "$") -> Scenario:
    data = _object(value, path)
    keys = {
        "schema_version", "snapshot_id", "is_template", "timezone", "id", "origin_city", "destination_city",
        "start_date", "end_date", "adults", "nights", "budget_kopecks", "currency", "preferences",
        "constraints", "notes",
    }
    _only_keys(data, keys, path)
    version, snapshot, template, timezone = _base(data, path)
    start = _date(_required(data, "start_date", path), f"{path}.start_date")
    end = _date(_required(data, "end_date", path), f"{path}.end_date")
    if start > end:
        raise SchemaError(path, "start_date не должна быть позже end_date")
    return Scenario(
        schema_version=version,
        snapshot_id=snapshot,
        is_template=template,
        timezone=timezone,
        id=_string(_required(data, "id", path), f"{path}.id"),
        origin_city=_string(_required(data, "origin_city", path), f"{path}.origin_city"),
        destination_city=_string(_required(data, "destination_city", path), f"{path}.destination_city"),
        start_date=start,
        end_date=end,
        adults=_integer(_required(data, "adults", path), f"{path}.adults", minimum=1),
        nights=_integer(_required(data, "nights", path), f"{path}.nights", minimum=0),
        budget_kopecks=_integer(
            _required(data, "budget_kopecks", path), f"{path}.budget_kopecks", nullable=True, minimum=0
        ),
        currency=_string(_required(data, "currency", path), f"{path}.currency"),
        preferences=parse_preferences(_required(data, "preferences", path), f"{path}.preferences"),
        constraints=parse_constraints(_required(data, "constraints", path), f"{path}.constraints"),
        notes=_string(_required(data, "notes", path), f"{path}.notes", nullable=True),
    )


EVENT_TYPES = {"transport", "transfer", "visit", "meal", "check_in", "check_out", "rest", "buffer", "included_breakfast", "luggage_drop", "luggage_pickup"}
QUANTITY_EVENT_TYPES = {"transport", "transfer", "visit", "meal"}


@dataclass(frozen=True)
class PlanEvent:
    id: str
    day: int
    type: str
    reference_id: str
    label: str
    start_at: datetime | None
    end_at: datetime | None
    quantity: int | None = None
    meal_type: str | None = None
    notes: str | None = None
    booking_id: str | None = None


def parse_event(value: Any, path: str) -> PlanEvent:
    data = _object(value, path)
    base_keys = {"id", "day", "type", "reference_id", "label", "start_at", "end_at", "notes"}
    event_type = _enum(_required(data, "type", path), f"{path}.type", EVENT_TYPES)
    expected = set(base_keys)
    if event_type in QUANTITY_EVENT_TYPES:
        expected.add("quantity")
    if event_type in {"visit", "meal"}:
        expected.add("booking_id")
    if event_type == "meal":
        expected.add("meal_type")
    _only_keys(data, expected, path)
    quantity = None
    if event_type in QUANTITY_EVENT_TYPES:
        quantity = _integer(_required(data, "quantity", path), f"{path}.quantity", minimum=1)
    meal_type = None
    if event_type == "meal":
        meal_type = _enum(
            _required(data, "meal_type", path),
            f"{path}.meal_type",
            {"breakfast", "lunch", "dinner", "snack"},
        )
    return PlanEvent(
        id=_string(_required(data, "id", path), f"{path}.id"),
        day=_integer(_required(data, "day", path), f"{path}.day", minimum=1),
        type=event_type,
        reference_id=_string(_required(data, "reference_id", path), f"{path}.reference_id"),
        label=_string(_required(data, "label", path), f"{path}.label"),
        start_at=_timestamp(_required(data, "start_at", path), f"{path}.start_at", nullable=True),
        end_at=_timestamp(_required(data, "end_at", path), f"{path}.end_at", nullable=True),
        quantity=quantity,
        meal_type=meal_type,
        notes=_string(data.get("notes"), f"{path}.notes", nullable=True),
        booking_id=_string(data.get("booking_id"), path+".booking_id", nullable=True),
    )


@dataclass(frozen=True)
class SelectedStay:
    reference_id: str
    rooms: int
    nights: int


def parse_selected_stay(value: Any, path: str) -> SelectedStay:
    data = _object(value, path)
    _only_keys(data, {"reference_id", "rooms", "nights"}, path)
    return SelectedStay(
        reference_id=_string(_required(data, "reference_id", path), f"{path}.reference_id"),
        rooms=_integer(_required(data, "rooms", path), f"{path}.rooms", minimum=1),
        nights=_integer(_required(data, "nights", path), f"{path}.nights", minimum=1),
    )


@dataclass(frozen=True)
class ManualPlan:
    schema_version: str
    snapshot_id: str
    is_template: bool
    timezone: str
    id: str
    scenario_id: str
    selected_stay: SelectedStay
    items: list[PlanEvent]
    total_cost_kopecks: int | None
    validation_status: str
    unresolved_items: list[str]


def parse_plan(value: Any, path: str = "$") -> ManualPlan:
    data = _object(value, path)
    keys = {
        "schema_version", "snapshot_id", "is_template", "timezone", "id", "scenario_id", "selected_stay",
        "items", "total_cost_kopecks", "validation_status", "unresolved_items",
    }
    _only_keys(data, keys, path)
    version, snapshot, template, timezone = _base(data, path)
    return ManualPlan(
        schema_version=version,
        snapshot_id=snapshot,
        is_template=template,
        timezone=timezone,
        id=_string(_required(data, "id", path), f"{path}.id"),
        scenario_id=_string(_required(data, "scenario_id", path), f"{path}.scenario_id"),
        selected_stay=parse_selected_stay(_required(data, "selected_stay", path), f"{path}.selected_stay"),
        items=_list(_required(data, "items", path), f"{path}.items", parse_event, nonempty=True),
        total_cost_kopecks=_integer(
            _required(data, "total_cost_kopecks", path),
            f"{path}.total_cost_kopecks",
            nullable=True,
            minimum=0,
        ),
        validation_status=_enum(
            _required(data, "validation_status", path),
            f"{path}.validation_status",
            {"not_checked", "valid", "invalid", "incomplete", "benchmark_assumptions"},
        ),
        unresolved_items=_list(
            _required(data, "unresolved_items", path),
            f"{path}.unresolved_items",
            lambda v, p: _string(v, p),
        ),
    )


CheckStatus = Literal["passed", "failed", "incomplete", "skipped"]


@dataclass
class Problem:
    code: str
    message: str
    file: str | None = None
    path: str | None = None
    object_id: str | None = None
    related_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    violations: list[Problem] = field(default_factory=list)
    incomplete_data: list[Problem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "violations": [item.to_dict() for item in self.violations],
            "incomplete_data": [item.to_dict() for item in self.incomplete_data],
        }


@dataclass
class ValidationReport:
    scope: Literal["day1", "full"]
    scenario_id: str | None
    snapshot_id: str | None
    is_template: dict[str, bool | None]
    loaded_counts: dict[str, int | None]
    event_count: int | None
    valid_complete_interval_count: int
    checks: list[CheckResult]
    input_errors: list[Problem]
    not_implemented: list[str]
    full_trip_feasibility_evaluated: bool = False
    cost: dict[str, Any] | None = None
    evaluation_mode: str = "strict"
    assumptions_hash: str | None = None

    @property
    def exit_code(self) -> int:
        if self.input_errors:
            return 2
        if any(check.status == "failed" for check in self.checks):
            return 1
        if any(check.status in {"incomplete", "skipped"} for check in self.checks):
            return 3
        return 0

    @property
    def overall_status(self) -> str:
        return {0: "passed", 1: "failed", 2: "input_error", 3: "incomplete"}[self.exit_code]

    def to_dict(self) -> dict[str, Any]:
        violations = [item.to_dict() for check in self.checks for item in check.violations]
        incomplete_data = [item.to_dict() for check in self.checks for item in check.incomplete_data]
        return {
            "scope": self.scope,
            "evaluation_mode": self.evaluation_mode,
            "assumptions_hash": self.assumptions_hash,
            "overall_status": self.overall_status,
            "exit_code": self.exit_code,
            "scenario_id": self.scenario_id,
            "snapshot_id": self.snapshot_id,
            "is_template": self.is_template,
            "loaded_counts": self.loaded_counts,
            "event_count": self.event_count,
            "valid_complete_interval_count": self.valid_complete_interval_count,
            "checks": [check.to_dict() for check in self.checks],
            "violations": violations,
            "incomplete_data": incomplete_data,
            "input_errors": [item.to_dict() for item in self.input_errors],
            "not_implemented": self.not_implemented,
            "full_trip_feasibility_evaluated": self.full_trip_feasibility_evaluated,
            "cost": self.cost,
        }
