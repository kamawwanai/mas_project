"""Deterministic validation. Unknown evidence never becomes a zero or a pass."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..catalog import LoadedDataset
from ..schemas import CheckResult, Problem, parse_plan, SchemaError
from ..validator import validate_plan, _build_indexes


def local_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name)
        raise ValueError(f"Timezone database unavailable for {name}; install tzdata")


def calculate_cost(catalogs, request, plan):
    """Account every charge once; unknown charge => total is null."""
    index = _build_indexes(LoadedDataset(Path("."), request.id, catalogs=catalogs))
    lines, violations, unknown = [], [], []

    def charge(item, quantity, expected, label, price=None):
        price = price or item.price
        if price.price_unit not in expected or price.currency != request.currency:
            violations.append(Problem("INVALID_PRICE_UNIT", f"Invalid price unit/currency: {label}", object_id=item.id))
            return
        if price.amount_kopecks is None or price.price_kind == "unknown" or price.includes_mandatory_fees is not True:
            unknown.append(Problem("UNKNOWN_FULL_PRICE", f"Full price not confirmed: {label}", object_id=item.id))
            return
        lines.append({"label": label, "reference_id": item.id, "quantity": quantity,
                      "unit_kopecks": price.amount_kopecks, "cost_kopecks": price.amount_kopecks * quantity})

    stay = index.unique("stays").get(plan.selected_stay.reference_id)
    if stay:
        charge(stay, plan.selected_stay.rooms * plan.selected_stay.nights, {"room_night"}, "stay")
    else:
        unknown.append(Problem("UNKNOWN_STAY_PRICE", "Selected stay is missing"))
    kinds = {"transport": "transport", "transfer": "transfers", "visit": "places", "meal": "food"}
    for event in plan.items:
        if event.type in kinds:
            item = index.unique(kinds[event.type]).get(event.reference_id)
            if not item:
                unknown.append(Problem("UNKNOWN_EVENT_PRICE", "Referenced object missing", object_id=event.id))
                continue
            units = {"transport": {"adult_ticket"}, "visit": {"adult_visit"}, "meal": {"adult_meal"}, "transfer": {"person_trip", "vehicle_trip"}}
            quantity = request.adults
            if event.type == "transfer" and item.price.price_unit == "vehicle_trip":
                if item.vehicle_capacity_adults is None:
                    unknown.append(Problem("UNKNOWN_VEHICLE_CAPACITY", "Vehicle capacity missing", object_id=item.id))
                    continue
                quantity = ceil(request.adults / item.vehicle_capacity_adults)
            if event.quantity != quantity:
                violations.append(Problem("WRONG_QUANTITY", f"Expected quantity {quantity}", object_id=event.id))
            charge(item, quantity, units[event.type], event.id)
        elif event.type == "luggage_drop" and stay:
            if stay.luggage_price is None:
                unknown.append(Problem("UNKNOWN_STORAGE_PRICE", "Luggage storage price missing", object_id=event.id))
            else:
                charge(stay, 1, {"group_storage"}, event.id, stay.luggage_price)
    total = None if unknown or violations else sum(x["cost_kopecks"] for x in lines)
    if request.budget_kopecks is None:
        unknown.append(Problem("UNKNOWN_BUDGET", "Scenario budget missing"))
    elif total is not None and total > request.budget_kopecks:
        violations.append(Problem("BUDGET_EXCEEDED", f"{total} > {request.budget_kopecks}"))
    if total is not None and plan.total_cost_kopecks is not None and total != plan.total_cost_kopecks:
        violations.append(Problem("TOTAL_COST_MISMATCH", "Declared total differs from computed total"))
    return {"lines": lines, "total_kopecks": total,
            "violations": [p.to_dict() for p in violations], "incomplete_data": [p.to_dict() for p in unknown]}


def validate_trip(catalogs, request, plan):
    dataset = LoadedDataset(Path("."), request.id, catalogs=catalogs, scenario=request)
    try:
        dataset.plan = parse_plan(plan) if isinstance(plan, dict) else plan
    except (SchemaError, TypeError, ValueError) as exc:
        dataset.input_errors.append(Problem("SCHEMA_ERROR", str(exc)))
    report = validate_plan(dataset)
    report.scope = "full"
    report.not_implemented = []
    report.full_trip_feasibility_evaluated = True
    if dataset.plan is None:
        return report
    plan = dataset.plan
    index = _build_indexes(dataset)
    errors, unknown = [], []

    def fail(code, message, event=None):
        errors.append(Problem(code, message, object_id=getattr(event, "id", None)))

    def missing(code, message, event=None):
        unknown.append(Problem(code, message, object_id=getattr(event, "id", None)))

    zone = local_zone(request.timezone)
    if any(d.timezone != request.timezone for d in catalogs.values()) or plan.timezone != request.timezone:
        fail("TIMEZONE_MISMATCH", "Document timezones differ")
    if request.nights != (request.end_date - request.start_date).days:
        fail("SCENARIO_NIGHTS", "Nights inconsistent with scenario dates")
    events = sorted((e for e in plan.items if e.start_at and e.end_at and e.start_at < e.end_at), key=lambda e: (e.start_at, e.id))
    counts = Counter(e.type for e in events)
    for kind, expected in (("transport", 2), ("check_in", 1), ("check_out", 1)):
        if counts[kind] != expected:
            fail("EVENT_COVERAGE", f"Expected {expected} events of type {kind}")
    stay = index.unique("stays").get(plan.selected_stay.reference_id)
    if stay:
        if (stay.check_in_date, stay.check_out_date) != (request.start_date, request.end_date):
            fail("STAY_DATES", "Stay dates do not match request")
        if plan.selected_stay.nights != request.nights:
            fail("STAY_NIGHTS", "Selected nights do not match request")
        if stay.room_capacity_adults is None:
            missing("UNKNOWN_ROOM_CAPACITY", "Room capacity unknown")
        elif stay.room_capacity_adults * plan.selected_stay.rooms < request.adults:
            fail("ROOM_CAPACITY", "Not enough room capacity")
        if (stay.requested_adults, stay.requested_rooms) != (request.adults, plan.selected_stay.rooms):
            fail("OFFER_OCCUPANCY", "Stay quote is for a different occupancy")
    kinds = {"transport": "transport", "transfer": "transfers", "visit": "places", "meal": "food"}
    location, checked_in, checked_out, stored = None, False, False, False
    walking, visits, rest, meals = defaultdict(int), defaultdict(int), defaultdict(int), set()
    visited, used = set(), {}
    previous_end = None
    for event in events:
        start, end = event.start_at.astimezone(zone), event.end_at.astimezone(zone)
        day = start.date()
        minutes = (end - start).total_seconds() / 60
        if event.day != (day - request.start_date).days + 1:
            fail("EVENT_DAY", "Event day disagrees with local date", event)
        if day < request.start_date or end > datetime.combine(request.end_date + timedelta(days=1), datetime.min.time(), zone):
            fail("TRIP_DATES", "Event outside trip dates", event)
        item = index.unique(kinds[event.type]).get(event.reference_id) if event.type in kinds else stay
        if item is None:
            continue
        used[item.id] = item
        if getattr(item, "city", request.destination_city) != request.destination_city:
            fail("WRONG_CITY", "Object belongs to another city", event)
        availability = getattr(item, "availability_status", "available")
        if availability == "unavailable":
            fail("UNAVAILABLE", "Offer unavailable in snapshot", event)
        elif availability == "unknown":
            missing("UNKNOWN_AVAILABILITY", "Offer availability unknown", event)
        if event.type == "transport":
            if not item.departure_at or not item.arrival_at:
                missing("UNKNOWN_TIMETABLE", "Train timetable missing", event)
            elif (start, end) != (item.departure_at, item.arrival_at):
                fail("TIMETABLE_MISMATCH", "Event must match saved timetable", event)
            if item.service_date != day:
                fail("SERVICE_DATE", "Service date differs", event)
            expected_direction = "outbound" if location is None else "return"
            if item.direction != expected_direction:
                fail("TRANSPORT_DIRECTION", "Unexpected train direction", event)
            origin, destination = (request.origin_city, request.destination_city) if item.direction == "outbound" else (request.destination_city, request.origin_city)
            if (item.from_station.city, item.to_station.city) != (origin, destination):
                fail("TRANSPORT_CITIES", "Train cities do not match request", event)
            if item.direction == "return":
                if location != item.from_station.id:
                    fail("LOCATION_DISCONTINUITY", "Not at departure station", event)
                if previous_end is not None and (start - previous_end).total_seconds() / 60 < request.constraints.min_departure_buffer_minutes:
                    # An explicit buffer ends at departure: check its duration instead.
                    buffers = [e for e in events if e.type == "buffer" and e.end_at == event.start_at and e.reference_id == item.from_station.id]
                    if not buffers or max((e.end_at-e.start_at).total_seconds()/60 for e in buffers) < request.constraints.min_departure_buffer_minutes:
                        fail("DEPARTURE_BUFFER", "Insufficient departure buffer", event)
            location = item.to_station.id
        elif event.type == "transfer":
            if location != item.from_id:
                fail("LOCATION_DISCONTINUITY", "Transfer does not start at current location", event)
            if item.applicable_date != day:
                fail("TRANSFER_DATE", "Transfer quote is for another date", event)
            if item.duration_minutes is None or item.mode is None or item.duration_kind == "unknown":
                missing("UNKNOWN_TRANSFER_DURATION", "Transfer duration/mode unknown", event)
            elif minutes < item.duration_minutes:
                fail("TRANSFER_TOO_SHORT", "Not enough time for transfer", event)
            walking_part = item.duration_minutes if item.mode == "walk" else item.walking_minutes
            if walking_part is None:
                missing("UNKNOWN_WALKING", "Walking portion unknown", event)
            else:
                walking[day] += walking_part
            location = item.to_id
        else:
            target = event.reference_id
            if location != target:
                fail("LOCATION_DISCONTINUITY", "Event is not at current location", event)
            if event.type in {"visit", "meal"}:
                duration = item.recommended_duration_minutes
                if duration is None:
                    missing("UNKNOWN_VISIT_DURATION", "Duration missing", event)
                elif minutes < duration:
                    fail("VISIT_TOO_SHORT", "Insufficient visit/meal time", event)
                hours = [h for h in item.opening_hours if h.date == day]
                if not hours or any(h.status == "unknown" for h in hours):
                    missing("UNKNOWN_OPENING_HOURS", "Opening hours missing", event)
                elif not any(h.status == "open" and any(start >= i.start_at and end <= i.end_at for i in h.intervals) for h in hours):
                    fail("OUTSIDE_OPENING_HOURS", "Event outside opening hours", event)
                booking = item.booking_required if event.type == "visit" else item.reservation_required
                if booking is None or booking:
                    missing("BOOKING_UNCONFIRMED", "Reservation requirements need confirmation", event)
                if event.type == "visit":
                    visited.add(item.id)
                    visits[day] += minutes
                else:
                    if item.meal_types is None:
                        missing("UNKNOWN_MEAL_TYPES", "Meal types unknown", event)
                    elif event.meal_type not in item.meal_types:
                        fail("MEAL_TYPE", "Unsupported meal type", event)
                    meals.add((day, event.meal_type))
            elif event.type == "check_in" and stay:
                if stay.check_in_from is None:
                    missing("UNKNOWN_CHECK_IN", "Check-in time unknown", event)
                elif day != stay.check_in_date or start.time().replace(tzinfo=None) < datetime.strptime(stay.check_in_from, "%H:%M").time():
                    fail("CHECK_IN_TIME", "Invalid check-in time", event)
                checked_in = True
            elif event.type == "check_out" and stay:
                if not checked_in:
                    fail("CHECK_OUT_ORDER", "Check-out before check-in", event)
                if stay.check_out_until is None:
                    missing("UNKNOWN_CHECK_OUT", "Check-out deadline unknown", event)
                elif day != stay.check_out_date or end.time().replace(tzinfo=None) > datetime.strptime(stay.check_out_until, "%H:%M").time():
                    fail("CHECK_OUT_TIME", "Invalid check-out time", event)
                checked_out = True
            elif event.type == "rest":
                if not checked_in or checked_out:
                    fail("REST_OUTSIDE_STAY", "Rest outside occupied stay", event)
                rest[day] += minutes
            elif event.type == "included_breakfast" and stay:
                if stay.breakfast_included is not True or not checked_in or checked_out:
                    fail("BREAKFAST_NOT_INCLUDED", "Breakfast not applicable", event)
                hours = [h for h in stay.breakfast_hours if h.date == day]
                if not hours or any(h.status == "unknown" for h in hours):
                    missing("UNKNOWN_BREAKFAST_HOURS", "Included breakfast hours unknown", event)
                elif not any(h.status == "open" and any(start >= i.start_at and end <= i.end_at for i in h.intervals) for h in hours):
                    fail("BREAKFAST_HOURS", "Breakfast outside saved service window", event)
                meals.add((day, "breakfast"))
            elif event.type in {"luggage_drop", "luggage_pickup"} and stay:
                if stay.luggage_storage_available is None:
                    missing("UNKNOWN_STORAGE", "Storage availability unknown", event)
                elif not stay.luggage_storage_available:
                    fail("STORAGE_UNAVAILABLE", "No luggage storage", event)
                if event.type == "luggage_drop":
                    if stored:
                        fail("LUGGAGE_ORDER", "Luggage already stored", event)
                    stored = True
                else:
                    if not stored:
                        fail("LUGGAGE_ORDER", "No luggage to collect", event)
                    stored = False
            if request.constraints.luggage_required and event.type in {"visit", "meal"} and not stored and (not checked_in or checked_out):
                fail("LUGGAGE_UNHANDLED", "Luggage must be stored outside occupied stay", event)
        previous_end = event.end_at
    if stored:
        fail("LUGGAGE_NOT_COLLECTED", "Stored luggage not collected")
    for place in request.constraints.required_place_ids:
        if place not in visited:
            fail("REQUIRED_PLACE_NOT_VISITED", f"Required place not visited: {place}")
    for offset in range((request.end_date - request.start_date).days + 1):
        day = request.start_date + timedelta(days=offset)
        if request.constraints.max_walking_minutes_per_day is not None and walking[day] > request.constraints.max_walking_minutes_per_day:
            fail("WALKING_LIMIT", f"Walking limit exceeded on {day}")
        if visits[day] < request.constraints.min_visit_minutes_per_day:
            fail("VISIT_COVERAGE", f"Not enough visits on {day}")
        if day < request.end_date and rest[day] < request.constraints.min_rest_minutes_per_night:
            fail("REST_COVERAGE", f"Insufficient nightly rest on {day}")
        for meal in request.constraints.required_meals:
            if (day, meal) not in meals:
                fail("MEAL_COVERAGE", f"Missing {meal} on {day}")
    if stay:
        used[stay.id] = stay
    for item in used.values():
        source = item.source
        if not source.source_url or urlparse(source.source_url).scheme not in {"http", "https"} or not source.checked_at:
            missing("MISSING_SOURCE", f"Missing source URL/check timestamp: {item.id}")
        required = {"price"}
        if item.id in index.unique("transport"):
            required |= {"timetable", "availability"}
        elif item.id in index.unique("stays"):
            required |= {"policies", "capacity", "availability"}
        elif item.id in index.unique("places") or item.id in index.unique("food"):
            required |= {"opening_hours", "duration", "booking"}
        else:
            required |= {"duration", "walking"}
        supported = set(source.verified_fields)
        for evidence in source.additional_sources:
            if evidence.checked_at and urlparse(evidence.source_url).scheme in {"http", "https"}:
                supported.update(evidence.supports)
        if not required <= supported:
            missing("UNSUPPORTED_FACTS", f"Unconfirmed fields for {item.id}: {sorted(required-supported)}")
        if source.notes and any(marker in source.notes.lower() for marker in ("synthetic", "синтетич", "not verified")):
            missing("SYNTHETIC_FACTS", f"Synthetic/unverified record: {item.id}")
    cost = calculate_cost(catalogs, request, plan)
    report.cost = cost
    cost_errors = [Problem(**p) for p in cost["violations"]]
    cost_unknown = [Problem(**p) for p in cost["incomplete_data"]]
    report.checks.append(CheckResult("trip_feasibility", "failed" if errors else "incomplete" if unknown else "passed", "Full trip constraints", errors, unknown))
    report.checks.append(CheckResult("budget", "failed" if cost_errors else "incomplete" if cost_unknown else "passed", "Full cost accounting", cost_errors, cost_unknown))
    return report
