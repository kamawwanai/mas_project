"""Synthetic fixtures ONLY for program tests; never used as experiment offers."""
from copy import deepcopy
from datetime import datetime

from src.schemas import parse_catalog, parse_scenario
from tests.fixtures import make_dataset, event, price


def full_data():
    data = make_dataset()
    for key in data:
        data[key]["schema_version"] = "0.2.0"
    request = data["scenario"]
    request["budget_kopecks"] = 200000
    request["constraints"].update(min_rest_minutes_per_night=420, min_visit_minutes_per_day=60, required_meals=["lunch"], luggage_required=False)
    supports = ["price", "timetable", "availability", "policies", "capacity", "opening_hours", "duration", "booking", "walking", "features"]
    for kind in ("transport", "stays", "places", "food", "transfers"):
        for row in data[kind]["items"]:
            row.update(notes="Unit test fixture; not an offer", verified_fields=supports,
                       features={"culture": 100, "food": 100, "comfort": 100}, reviews=[])
    back = deepcopy(data["transport"]["items"][0])
    back.update(id="train_back", direction="return", service_date="2026-10-25", departure_at="2026-10-25T12:30:00+03:00", arrival_at="2026-10-25T13:30:00+03:00")
    back["from_station"], back["to_station"] = back["to_station"], back["from_station"]
    data["transport"]["items"].append(back)
    data["stays"]["items"][0]["check_in_from"] = "10:00"
    for kind in ("places", "food"):
        hours = deepcopy(data[kind]["items"][0]["opening_hours"][0])
        hours["date"] = "2026-10-25"
        for interval in hours["intervals"]:
            for key in ("start_at", "end_at"):
                interval[key] = interval[key].replace("2026-10-24", "2026-10-25")
        data[kind]["items"][0]["opening_hours"].append(hours)
    template = data["transfers"]["items"][0]
    for key, left, right in [("hotel_place", "stay_1", "place_1"), ("place_food", "place_1", "food_1"), ("food_hotel", "food_1", "stay_1"), ("hotel_station", "stay_1", "station_b")]:
        for day in (24, 25):
            row = deepcopy(template)
            row.update(id=f"{key}_{day}", from_id=left, to_id=right, applicable_date=f"2026-10-{day}")
            data["transfers"]["items"].append(row)
    events = []

    def add(kind, reference, start, end, day=24):
        e = event(f"e{len(events)+1}", kind, reference, f"2026-10-{day}T{start}:00+03:00", f"2026-10-{day}T{end}:00+03:00")
        e["day"] = day-23
        if kind in {"transport", "transfer", "visit", "meal"}:
            e["quantity"] = 2
        events.append(e)

    add("transport", "train_1", "09:00", "10:00")
    add("transfer", "transfer_1", "10:00", "10:15")
    add("check_in", "stay_1", "10:15", "10:25")
    add("transfer", "hotel_place_24", "10:25", "10:40")
    add("visit", "place_1", "10:40", "11:40")
    add("transfer", "place_food_24", "11:40", "11:55")
    add("meal", "food_1", "11:55", "12:40")
    add("transfer", "food_hotel_24", "12:40", "12:55")
    add("rest", "stay_1", "22:00", "23:59")
    events[-1]["end_at"] = "2026-10-25T07:00:00+03:00"
    add("transfer", "hotel_place_25", "09:00", "09:15", 25)
    add("visit", "place_1", "09:15", "10:15", 25)
    add("transfer", "place_food_25", "10:15", "10:30", 25)
    add("meal", "food_1", "10:30", "11:15", 25)
    add("transfer", "food_hotel_25", "11:15", "11:30", 25)
    add("check_out", "stay_1", "11:30", "11:40", 25)
    add("transfer", "hotel_station_25", "11:40", "11:55", 25)
    add("buffer", "station_b", "11:55", "12:30", 25)
    add("transport", "train_back", "12:30", "13:30", 25)
    data["plan"]["items"] = events
    return data


def parsed(data=None):
    data = data or full_data()
    catalogs = {k: parse_catalog(data[k], k) for k in ("transport", "stays", "places", "food", "transfers")}
    return catalogs, parse_scenario(data["scenario"]), deepcopy(data["plan"])
