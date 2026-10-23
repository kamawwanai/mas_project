"""Deterministic validation. Unknown evidence never becomes a zero or a pass."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from ..evidence import local_zone, valid_url, supported_fields, synthetic_source
from ..catalog import LoadedDataset
from ..schemas import CheckResult, Problem, parse_plan, SchemaError
from ..validator import validate_plan, _build_indexes


def calculate_cost(catalogs, request, plan, *, context=None):
    """Account every charge once; unknown charge => total is null."""
    if context:
        context.verify(catalogs)
    index = _build_indexes(LoadedDataset(Path("."), request.id, catalogs=catalogs))
    lines, violations, unknown = [], [], []

    def charge(item, quantity, expected, label, price=None):
        price = price or item.price
        if price.price_unit not in expected or price.currency != request.currency:
            violations.append(Problem("INVALID_PRICE_UNIT", f"Invalid price unit/currency: {label}", object_id=item.id))
            return
        synthetic = price.price_kind == "synthetic_assumption"
        confirmed = price.amount_kopecks is not None and price.includes_mandatory_fees is True and (
            price.price_kind not in {"unknown", "indicative_test_value", "synthetic_assumption"} or
            (synthetic and context is not None and context.supports(item, "price")))
        if not confirmed:
            unknown.append(Problem("UNKNOWN_FULL_PRICE", f"Full price not confirmed: {label}", object_id=item.id))
        if price.amount_kopecks is None:
            return
        lines.append({"label": label, "reference_id": item.id, "quantity": quantity,
                      "unit_kopecks": price.amount_kopecks, "cost_kopecks": price.amount_kopecks * quantity,
                      "full_price_confirmed": confirmed and not synthetic,
                      "synthetic_assumption": synthetic, "condition_price_defined": confirmed})

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
            units = {"transport": {"adult_ticket"}, "visit": {"adult_visit"}, "meal": {"adult_meal"}, "transfer": {"person_trip", "vehicle_trip", "group_trip"}}
            quantity = request.adults
            if event.type == "transfer" and item.price.price_unit == "group_trip":
                quantity = 1
                if item.price.group_size_adults is None:
                    unknown.append(Problem("UNKNOWN_GROUP_SIZE", "Group fare occupancy not specified", object_id=event.id))
                elif item.price.group_size_adults != request.adults:
                    violations.append(Problem("GROUP_SIZE_MISMATCH", "Group fare is for a different number of adults", object_id=event.id))
            elif event.type == "transfer" and item.price.price_unit == "vehicle_trip":
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
    estimate = None if violations or any(p.code in {"UNKNOWN_EVENT_PRICE", "UNKNOWN_STAY_PRICE", "UNKNOWN_VEHICLE_CAPACITY", "UNKNOWN_STORAGE_PRICE"} for p in unknown) or any(
        p.code == "UNKNOWN_FULL_PRICE" and not any(line["reference_id"] == p.object_id for line in lines) for p in unknown
    ) else sum(x["cost_kopecks"] for x in lines)
    if request.budget_kopecks is None:
        unknown.append(Problem("UNKNOWN_BUDGET", "Scenario budget missing"))
    elif total is not None and total > request.budget_kopecks:
        violations.append(Problem("BUDGET_EXCEEDED", f"{total} > {request.budget_kopecks}"))
    if total is not None and plan.total_cost_kopecks is not None and total != plan.total_cost_kopecks:
        violations.append(Problem("TOTAL_COST_MISMATCH", "Declared total differs from computed total"))
    return {"lines": lines, "total_kopecks": total, "estimate_kopecks": estimate,
            "violations": [p.to_dict() for p in violations], "incomplete_data": [p.to_dict() for p in unknown]}


def validate_trip(catalogs, request, plan, *, context=None):
    if context:
        context.verify(catalogs)
        if request.snapshot_id != context.snapshot_id:
            raise ValueError("Request and assumption snapshot differ")
    dataset = LoadedDataset(Path("."), request.id, catalogs=catalogs, scenario=request)
    try:
        dataset.plan = parse_plan(plan) if isinstance(plan, dict) else plan
    except (SchemaError, TypeError, ValueError) as exc:
        dataset.input_errors.append(Problem("SCHEMA_ERROR", str(exc)))
    report = validate_plan(dataset)
    report.scope = "full"
    if context:
        report.evaluation_mode = "benchmark_assumptions"
        report.assumptions_hash = context.registry_hash
    report.not_implemented = []
    report.full_trip_feasibility_evaluated = dataset.plan is not None
    if dataset.plan is None:
        return report
    plan = dataset.plan
    index = _build_indexes(dataset)
    errors, unknown = [], []

    incomplete_events = [e for e in plan.items if e.start_at is None or e.end_at is None]
    sequence_uncertain = bool(incomplete_events)

    def fail(code, message, event=None):
        # Unknown temporal placement may bridge any apparent spatial/state gap.
        dependent = {"LOCATION_DISCONTINUITY", "TRANSPORT_DIRECTION", "CHECK_OUT_ORDER",
                     "REST_OUTSIDE_STAY", "LUGGAGE_ORDER", "LUGGAGE_UNHANDLED",
                     "LUGGAGE_NOT_COLLECTED", "BREAKFAST_NOT_INCLUDED", "DEPARTURE_BUFFER"}
        if sequence_uncertain and code in dependent:
            unknown.append(Problem("UNKNOWN_SEQUENCE", message, object_id=getattr(event, "id", None)))
            return
        errors.append(Problem(code, message, object_id=getattr(event, "id", None)))

    def missing(code, message, event=None):
        unknown.append(Problem(code, message, object_id=getattr(event, "id", None)))

    try:
        zone = local_zone(request.timezone)
        for document in [*catalogs.values(), plan]:
            local_zone(document.timezone)
    except ValueError as exc:
        report.input_errors.append(Problem("INVALID_TIMEZONE", str(exc), path="$.timezone"))
        report.full_trip_feasibility_evaluated = False
        return report
    if any(d.timezone != request.timezone for d in catalogs.values()) or plan.timezone != request.timezone:
        fail("TIMEZONE_MISMATCH", "Document timezones differ")
    if request.nights != (request.end_date - request.start_date).days:
        fail("SCENARIO_NIGHTS", "Nights inconsistent with scenario dates")
    events = sorted((e for e in plan.items if e.start_at and e.end_at and e.start_at < e.end_at), key=lambda e: (e.start_at, e.id))
    counts = Counter(e.type for e in plan.items)
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
    outbound = next((index.unique("transport").get(e.reference_id) for e in events if e.type == "transport"), None)
    location = outbound.from_station.id if outbound and outbound.direction == "outbound" else None
    checked_in, checked_out, stored, transport_count = False, False, False, 0
    walking, visits, rest, meals = defaultdict(int), defaultdict(int), defaultdict(int), set()
    rest_intervals = []
    visited, used = set(), {}
    used_bookings = set()
    for e in plan.items:
        obj = index.unique(kinds[e.type]).get(e.reference_id) if e.type in kinds else None
        if obj:
            used[obj.id] = obj
    previous_end = None
    for event in events:
        start, end = event.start_at.astimezone(zone), event.end_at.astimezone(zone)
        day = start.date()
        minutes = (end - start).total_seconds() / 60
        if event.day != (day - request.start_date).days + 1:
            fail("EVENT_DAY", "Event day disagrees with local date", event)
        if day < request.start_date or end > datetime.combine(request.end_date + timedelta(days=1), datetime.min.time(), zone):
            fail("TRIP_DATES", "Event outside trip dates", event)
        if event.type in {"buffer", "rest"}:
            if location != event.reference_id:
                fail("LOCATION_DISCONTINUITY", "Pause is not at current location", event)
            if event.type == "rest" and stay and event.reference_id == stay.id:
                if not checked_in or checked_out:
                    fail("REST_OUTSIDE_STAY", "Rest outside occupied stay", event)
                rest_intervals.append((start, end))
            previous_end = event.end_at
            continue
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
            expected_direction = "outbound" if transport_count == 0 else "return"
            transport_count += 1
            if item.direction != expected_direction:
                fail("TRANSPORT_DIRECTION", "Unexpected train direction", event)
            origin, destination = (request.origin_city, request.destination_city) if item.direction == "outbound" else (request.destination_city, request.origin_city)
            if (item.from_station.city, item.to_station.city) != (origin, destination):
                fail("TRANSPORT_CITIES", "Train cities do not match request", event)
            if item.from_station.id is None or item.to_station.id is None:
                missing("UNKNOWN_STATION_ID", "Train station ID is not known", event)
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
            if item.departure_windows is None:
                if item.mode == "public_transport":
                    missing("UNKNOWN_TRANSFER_WINDOWS", "Departure time applicability is unknown", event)
            elif not any(w.start_at <= start < w.end_at for w in item.departure_windows):
                fail("TRANSFER_DEPARTURE_WINDOW", "Departure outside saved applicability windows", event)
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
                if booking is None:
                    missing("BOOKING_UNCONFIRMED", "Reservation requirement unknown", event)
                if booking is True or event.booking_id:
                    confirmations = [b for b in item.booking_confirmations if b.id == event.booking_id]
                    if not event.booking_id:
                        missing("BOOKING_UNCONFIRMED", "Saved booking confirmation required", event)
                    elif len(confirmations) != 1:
                        fail("INVALID_BOOKING_REFERENCE", "Booking ID is missing or ambiguous", event)
                    else:
                        booking_key = (item.id, event.booking_id)
                        if booking_key in used_bookings:
                            fail("BOOKING_REUSED", "One confirmation cannot cover multiple visits/meals", event)
                        used_bookings.add(booking_key)
                        confirmation = confirmations[0]
                        if confirmation.adults != request.adults or not (confirmation.start_at <= start and end <= confirmation.end_at):
                            fail("BOOKING_MISMATCH", "Saved booking does not cover group/time", event)
                        assumed_booking = (context is not None and context.supports(item, "booking") and
                            confirmation.assumption_id == item.source.original_fields.get("synthetic_assumption", {}).get("id"))
                        if not assumed_booking and (not valid_url(confirmation.source_url) or not confirmation.checked_at):
                            missing("BOOKING_UNCONFIRMED", "Booking evidence incomplete", event)
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
    # Union adjacent/overlapping hotel rest before assigning each midnight's night.
    merged = []
    for start, end in sorted(rest_intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in merged:
        night = start.date()
        while night < end.date():
            left = start if night == start.date() else datetime.combine(night, datetime.min.time(), zone) + timedelta(hours=12)
            right = end if night + timedelta(days=1) == end.date() else datetime.combine(night + timedelta(days=1), datetime.min.time(), zone) + timedelta(hours=12)
            rest[night] += max(0, (min(end, right)-max(start, left)).total_seconds()/60)
            night += timedelta(days=1)

    def coverage_fail(code, message, kinds, day=None):
        relevant = [e for e in incomplete_events if e.type in kinds and
                    (day is None or e.day == (day-request.start_date).days+1 or e.type == "rest")]
        (missing if relevant else fail)(code, message)

    if stored:
        fail("LUGGAGE_NOT_COLLECTED", "Stored luggage not collected")
    for place in request.constraints.required_place_ids:
        if place not in visited:
            coverage_fail("REQUIRED_PLACE_NOT_VISITED", f"Required place not visited: {place}", {"visit"})
    for offset in range((request.end_date - request.start_date).days + 1):
        day = request.start_date + timedelta(days=offset)
        if request.constraints.max_walking_minutes_per_day is not None and walking[day] > request.constraints.max_walking_minutes_per_day:
            fail("WALKING_LIMIT", f"Walking limit exceeded on {day}")
        if visits[day] < request.constraints.min_visit_minutes_per_day:
            coverage_fail("VISIT_COVERAGE", f"Not enough visits on {day}", {"visit"}, day)
        if request.constraints.activities_on_both_dates and visits[day] <= 0:
            coverage_fail("VISIT_COVERAGE", f"Missing visits on {day}", {"visit"}, day)
        if request.constraints.meals_on_both_dates and not any(d == day for d, _ in meals):
            coverage_fail("MEAL_COVERAGE", f"Missing meals on {day}", {"meal", "included_breakfast"}, day)
        if day < request.end_date and rest[day] < request.constraints.min_rest_minutes_per_night:
            coverage_fail("REST_COVERAGE", f"Insufficient nightly rest on {day}", {"rest"}, day)
        if request.constraints.rest_required and day < request.end_date and rest[day] <= 0:
            coverage_fail("REST_COVERAGE", f"Missing nightly rest on {day}", {"rest"}, day)
        for meal in request.constraints.required_meals:
            if (day, meal) not in meals:
                coverage_fail("MEAL_COVERAGE", f"Missing {meal} on {day}", {"meal", "included_breakfast"}, day)
    if stay:
        used[stay.id] = stay
    for text in plan.unresolved_items:
        missing("PLAN_UNCERTAINTY", text)
    for item in used.values():
        source = item.source
        assumed = context is not None and context.supports(item, "price")
        if not assumed and (not valid_url(source.source_url) or not source.checked_at):
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
            if item.mode == "public_transport":
                required.add("departure_windows")
        supported = supported_fields(source)
        if context:
            supported |= {field for field in required if context.supports(item, field)}
        if not required <= supported:
            missing("UNSUPPORTED_FACTS", f"Unconfirmed fields for {item.id}: {sorted(required-supported)}")
        if not assumed and source.notes and any(marker in source.notes.lower() for marker in ("synthetic", "синтетич", "not verified")):
            missing("SYNTHETIC_FACTS", f"Synthetic/unverified record: {item.id}")
    cost = calculate_cost(catalogs, request, plan, context=context)
    report.cost = cost
    cost_errors = [Problem(**p) for p in cost["violations"]]
    cost_unknown = [Problem(**p) for p in cost["incomplete_data"]]
    report.checks.append(CheckResult("trip_feasibility", "failed" if errors else "incomplete" if unknown else "passed", "Full trip constraints", errors, unknown))
    report.checks.append(CheckResult("budget", "failed" if cost_errors else "incomplete" if cost_unknown else "passed", "Full cost accounting", cost_errors, cost_unknown))
    return report
