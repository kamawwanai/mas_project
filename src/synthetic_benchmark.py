"""Deterministic synthetic environment derived from the preserved October snapshot.

No network, provider client, private-file access by planners, or historical claims.
The finite candidate pool is an audit aid, not a tool offered to architectures.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from itertools import product
from pathlib import Path
import json

from .assumptions import AssumptionContext, load_context
from .catalog import load_catalogs, load_public_scenario, load_plan
from .contracts import digest, jsonable, read_json
from .schemas import parse_catalog, parse_scenario, parse_plan
from .checks import validate_trip, calculate_cost
from .evaluator import verify_conflict, validate_profile
from .selection import features, identity, distinct, dominates, utility

ROOT = Path(__file__).resolve().parents[1]
MODE = "benchmark_assumptions"
SNAPSHOT = "synthetic_2026_10_17_v1"
DAYS = ("2026-10-17", "2026-10-18")
KINDS = ("transport", "stays", "places", "food", "transfers")


def encoded(value):
    return (json.dumps(jsonable(value), ensure_ascii=False, indent=2)+"\n").encode("utf-8")


def timestamp(day, time):
    return f"{day}T{time}:00+03:00"


def hours(start="08:00", end="23:00"):
    return [{"date": d, "status": "open", "intervals": [{"start_at": timestamp(d, start), "end_at": timestamp(d, end)}]} for d in DAYS]


def price(amount, unit, group=None):
    result = {"amount_kopecks": amount, "currency": "RUB", "price_unit": unit,
              "price_kind": "synthetic_assumption", "includes_mandatory_fees": True}
    if group:
        result["group_size_adults"] = group
    return result


def source(kind, row, origin=None, scores=None):
    identifier = f"assumption:{kind}:{row['id']}"
    original = {"synthetic_assumption": {"id": identifier, "status": "synthetic_assumption"}}
    if origin:
        original["origin"] = {"path": f"data/snapshot/{kind}.json", "object_id": origin["id"],
                              "row_sha256": sha256(encoded(origin)).hexdigest(),
                              "source_url": origin.get("source_url"), "checked_at": origin.get("checked_at")}
    row.update(source_url=None, checked_at=None, additional_sources=[], verified_fields=[],
               features=scores or {}, reviews=[], original_fields=original,
               notes="synthetic_assumption: fixed experimental condition; not historical evidence or an actual booking.")
    for booking in row.get("booking_confirmations", []):
        booking.update(source_url=None, checked_at=None, assumption_id=identifier)
    return row


def compile_snapshot(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    workspace = manifest_path.parents[2]
    manifest = read_json(manifest_path)
    for name, expected in manifest.get("protected_sources", {}).items():
        path = (workspace/name).resolve()
        if not path.is_relative_to(workspace) or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Protected original changed: " + name)
    original = {}
    for spec in manifest["sources"]:
        path = (workspace/spec["path"]).resolve()
        if not path.is_relative_to(workspace) or sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
            raise ValueError("Protected source changed: " + spec["path"])
        raw = read_json(path)
        original[spec["kind"]] = {r["id"]: r for r in raw["items"]}
    design_path = manifest_path.parent/"design.json"
    if sha256(design_path.read_bytes()).hexdigest() != manifest["design_sha256"]:
        raise ValueError("Synthetic design differs from manifest")
    design = read_json(design_path)
    rows = {k: [] for k in KINDS}
    for key in design["transport_ids"]:
        old = original["transport"][key]
        row = deepcopy(old)
        row["price"] = price(old["price"]["amount_kopecks"], "adult_ticket")
        row["availability_status"] = "available"
        rows["transport"].append(source("transport", row, old))
    for spec in design["hotels"]:
        old = original["stays"][spec["id"]]
        row = deepcopy(old)
        row.update(check_in_from="14:00", check_out_until="12:00", room_capacity_adults=2,
                   requested_adults=2, requested_rooms=1, breakfast_included=False,
                   luggage_storage_available=True, luggage_price=price(0, "group_storage"), availability_status="available",
                   price=price(spec["amount_kopecks"], "room_night"))
        rows["stays"].append(source("stays", row, old, spec["features"]))
    for n, old in enumerate(original["places"].values()):
        row = deepcopy(old)
        row.update(opening_hours=hours("11:00", "19:00"), recommended_duration_minutes=60,
                   availability_status="available", booking_required=False, booking_confirmations=[],
                   price=price(old["price"]["amount_kopecks"], "adult_visit"))
        if row["id"] == "place_02":
            row.update(opening_hours=[{"date": d, "status": "closed", "intervals": []} for d in DAYS])
        if row["id"] in {"place_04", "place_05"}:
            row["opening_hours"] = hours("06:00", "23:00")
        if row["id"] == "place_06":
            row["booking_required"] = True
            # These are reusable simulated reservation entitlements, not purchased tickets.
            row["booking_confirmations"] = [{"id": f"slot_place_06_{d}", "start_at": timestamp(d, "15:00"),
                "end_at": timestamp(d, "18:00"), "adults": 2} for d in DAYS]
            row["opening_hours"] = hours("15:00", "18:00")
        rows["places"].append(source("places", row, old, {"culture": design["culture_scores"][row["id"]]}))
    for key, spec in design["food"].items():
        old = original["food"][key]
        row = deepcopy(old)
        row.update(price=price(spec["amount_kopecks"], "adult_meal"), price_basis="synthetic_assumption: one fixed meal basket per adult, all mandatory fees included",
                   meal_types=["breakfast", "lunch", "dinner", "snack"], reservation_required=False,
                   recommended_duration_minutes=30, opening_hours=hours("08:00", "23:00"), booking_confirmations=[])
        rows["food"].append(source("food", row, old, {"food": spec["score"]}))
    valid_nodes = {r["id"] for k in ("stays", "places", "food") for r in rows[k]} | {"station_nn_01"}
    for old in original["transfers"].values():
        if old["from_id"] not in valid_nodes or old["to_id"] not in valid_nodes:
            continue
        row = deepcopy(old)
        duration = row["duration_minutes"] or 25
        row.update(duration_minutes=duration, distance_meters=row["distance_meters"] or duration*150,
                   duration_kind="estimated", walking_minutes=duration if row["mode"] == "walk" else min(6, duration),
                   departure_windows=hours("05:00", "23:00")[DAYS.index(row["applicable_date"])]["intervals"],
                   price=price(old["price"]["amount_kopecks"], "group_trip", 2))
        rows["transfers"].append(source("transfers", row, old))
    # Directed edges for every pair: no hidden solver or missing-hotel bias.
    coords = design["coordinates"]
    for day, left, right, mode in product(DAYS, sorted(valid_nodes), sorted(valid_nodes), ("walk", "public_transport", "taxi")):
        if left == right:
            continue
        a, b = coords[left], coords[right]
        distance = abs(a[0]-b[0])+abs(a[1]-b[1])
        duration = 4+3*distance if mode == "walk" else 9+distance if mode == "public_transport" else 8+distance//2
        amount = 0 if mode == "walk" else 8000 if mode == "public_transport" else 60000+2000*distance
        row = {"id": edge_id(day, left, right, mode), "from_id": left, "to_id": right, "applicable_date": day,
               "mode": mode, "duration_minutes": duration, "distance_meters": max(100, distance*300),
               "duration_kind": "estimated", "walking_minutes": duration if mode == "walk" else 4+distance%4 if mode == "public_transport" else 0,
               "departure_windows": hours("05:00", "23:00")[DAYS.index(day)]["intervals"],
               "price": price(amount, "group_trip", 2)}
        rows["transfers"].append(source("transfers", row))
    docs = {k: {"schema_version": "0.2.0", "snapshot_id": SNAPSHOT, "is_template": False,
                "timezone": "Europe/Moscow", "items": sorted(v, key=lambda r: r["id"])} for k, v in rows.items()}
    parsed = {k: parse_catalog(doc, k) for k, doc in docs.items()}
    entries = []
    for kind, doc in parsed.items():
        for item in doc.items:
            semantic = {"price", "features"}
            semantic |= ({"timetable", "availability"} if kind == "transport" else
                         {"policies", "capacity", "availability"} if kind == "stays" else
                         {"opening_hours", "duration", "booking"} if kind in {"food", "places"} else
                         {"duration", "walking", "departure_windows"})
            values = jsonable(item)
            values.pop("source")
            entries.append({"id": item.source.original_fields["synthetic_assumption"]["id"], "status": "synthetic_assumption",
                            "catalog": kind, "object_id": item.id, "applies_to": list(DAYS), "fields": sorted(semantic),
                            "values": values, "features": item.source.features, "row_hash": digest(item),
                            "rationale": "Fixed condition defined by design.json; inherited identifiers are not real-world verification."})
    registry = {"mode": MODE, "snapshot_id": SNAPSHOT, "closed_world": True,
                "notice": "All active fields, including inherited amounts, are synthetic_assumption. No historical prices, inventory or bookings are confirmed.",
                "assumptions": entries}
    AssumptionContext.load(parsed, registry)
    return docs, registry


def edge_id(day, left, right, mode):
    return f"syn_{day[-2:]}_{left}__{right}__{mode}"


def prepare(manifest_path, check=False):
    root = Path(manifest_path).resolve().parent
    try:
        docs, registry = compile_snapshot(manifest_path)
        outputs = {root/"snapshot"/f"{kind}.json": encoded(doc) for kind, doc in docs.items()}
        outputs[root/"assumptions.json"] = encoded(registry)
        problems = []
        for path, content in outputs.items():
            if check:
                if not path.exists() or path.read_bytes() != content:
                    problems.append({"file": str(path), "error": "Differs from deterministic synthetic preparation"})
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        return {"status": "blocked" if problems else "synthetic_assumptions_prepared", "mode": MODE,
                "problems": problems, "files": list(docs), "experiment_ready": False}
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "blocked", "mode": MODE, "problems": [str(exc)], "experiment_ready": False}


def make_plan(catalogs, case_id, hotel, lunch, dinner, mode="public_transport", *, dense=False, required=None, return_id="train_return_07"):
    """Private deterministic witness constructor; never imported by planner tools."""
    by_id = {i.id: i for doc in catalogs.values() for i in doc.items}
    items = []
    location = "station_moscow_east"
    now = datetime.fromisoformat(timestamp(DAYS[0], "06:00"))

    def event(kind, ref, minutes, meal=None, booking=None):
        nonlocal now
        end = now+timedelta(minutes=minutes)
        row = {"id": f"event_{len(items)+1:02d}", "day": 1+(now.date().isoformat() == DAYS[1]), "type": kind,
               "reference_id": ref, "label": f"synthetic_assumption: {kind} {ref}", "start_at": now.isoformat(), "end_at": end.isoformat()}
        if kind in {"transport", "transfer", "visit", "meal"}:
            row["quantity"] = 1 if kind == "transfer" else 2
        if meal:
            row["meal_type"] = meal
        if booking:
            row["booking_id"] = booking
        items.append(row); now = end

    def until(day, time, kind="buffer"):
        target = datetime.fromisoformat(timestamp(day, time))
        if target > now:
            event(kind, location, int((target-now).total_seconds()/60))

    def move(target):
        nonlocal location
        if location == target:
            return
        ref = edge_id(now.date().isoformat(), location, target, mode)
        event("transfer", ref, by_id[ref].duration_minutes)
        location = target

    def visit(ref, minutes):
        move(ref)
        booking = None
        if ref == "place_06":
            until(now.date().isoformat(), "15:00")
            booking = f"slot_place_06_{now.date().isoformat()}"
        else:
            until(now.date().isoformat(), "11:00")
        event("visit", ref, minutes, booking=booking)

    event("buffer", location, 30)
    train = by_id["train_out_01"]
    event("transport", train.id, int((train.arrival_at-train.departure_at).total_seconds()/60))
    location = "station_nn_01"
    event("buffer", location, 15)
    move(hotel); event("luggage_drop", hotel, 10)
    move(lunch); until(DAYS[0], "12:00"); event("meal", lunch, 45, "lunch")
    move(hotel); until(DAYS[0], "14:00"); event("check_in", hotel, 15); event("luggage_pickup", hotel, 10)
    visit(required or "place_01", 90 if dense else 60)
    if dense:
        visit("place_03", 90)
    move(dinner); until(DAYS[0], "18:00"); event("meal", dinner, 60, "dinner")
    move(hotel); until(DAYS[1], "07:30", "rest")
    move("food_03"); until(DAYS[1], "08:00"); event("meal", "food_03", 30, "breakfast")
    move(hotel); until(DAYS[1], "10:00"); event("check_out", hotel, 15); event("luggage_drop", hotel, 10)
    visit("place_03", 90)
    if dense:
        visit("place_05", 90)
    move(lunch); event("meal", lunch, 45, "lunch")
    if return_id == "train_return_08":
        move(dinner); until(DAYS[1], "16:30"); event("meal", dinner, 60, "dinner")
    move(hotel); event("luggage_pickup", hotel, 10)
    move("station_nn_01")
    train = by_id[return_id]
    if now > train.departure_at-timedelta(minutes=30):
        return None
    until(DAYS[1], train.departure_at.strftime("%H:%M"))
    event("transport", train.id, int((train.arrival_at-train.departure_at).total_seconds()/60))
    return {"schema_version": "0.2.0", "snapshot_id": SNAPSHOT, "is_template": False, "timezone": "Europe/Moscow",
            "id": f"reference_{case_id}_{hotel}_{lunch}_{dinner}_{mode}_{return_id}", "scenario_id": case_id,
            "selected_stay": {"reference_id": hotel, "rooms": 1, "nights": 1}, "items": items,
            "total_cost_kopecks": None, "validation_status": "not_checked", "unresolved_items": []}


def canonical_copy():
    plan = deepcopy(read_json(ROOT/"data/reference_plans/dev_01.json"))
    plan.update(schema_version="0.2.0", snapshot_id=SNAPSHOT, is_template=False,
                id="synthetic_reference_dev_01_45_events", total_cost_kopecks=None,
                validation_status="benchmark_assumptions", unresolved_items=[])
    for event in plan["items"]:
        event["notes"] = "synthetic_assumption: original planned interval retained in experimental copy."
    return plan


def save(root, value):
    root = Path(root); root.parent.mkdir(parents=True, exist_ok=True); root.write_bytes(encoded(value))


def public_request(base, spec):
    request = deepcopy(base)
    request.update(id=spec["id"], schema_version="0.2.0", snapshot_id=SNAPSHOT, is_template=False,
                   budget_kopecks=spec["budget_kopecks"], notes="synthetic_assumption environment. " + spec["public_request"])
    request["constraints"].update(spec.get("constraints", {}))
    if spec.get("pace"):
        request["preferences"]["pace"] = spec["pace"]
    return request


def build(root):
    """Generate all artifacts from protected sources and a public design specification."""
    root = Path(root).resolve()
    result = prepare(root/"manifest.json")
    if result["problems"]:
        raise ValueError(result["problems"])
    catalogs = load_catalogs(root/"snapshot")
    context = AssumptionContext.load(catalogs, read_json(root/"assumptions.json"))
    design = read_json(root/"design.json")
    profiles = read_json(ROOT/"evaluation/benchmark/design_profiles.json")
    base = read_json(ROOT/"data/scenarios/dev/dev_01.json")
    cases, quality = [], []
    for spec in design["scenarios"]:
        key, split = spec["id"], spec["split"]
        request = public_request(base, spec)
        public = parse_scenario(request)
        save(root/"scenarios"/split/f"{key}.json", request)
        profile = {**profiles[key], "private_marker": f"PRIVATE_SYNTHETIC_{key}", "status": "synthetic_assumption"}
        validate_profile(profile)
        save(ROOT/"evaluation/benchmark/profiles"/f"{key}.json", profile)
        case = {"id": key, "split": split, "title": spec["title"], "status": "synthetic_validated",
                "expected": spec["expected"], "evaluation_mode": MODE, "public_hash": digest(public),
                "profile_hash": digest(profile), "changes": spec.get("changes", []), "pending": [],
                "success_criteria": {"hard_constraints": "full validation in benchmark_assumptions", "utility_threshold": 0.7,
                                     "infeasibility": "validated certificate within this finite closed synthetic catalog"}}
        feasible = []
        if spec["expected"] == "feasible":
            pool = []
            if key == "dev_01":
                pool.append(canonical_copy())
            # Three independently meaningful meal profiles; six hotel tradeoffs; two mobility profiles.
            for hotel, meals, mode in product(design["hotels"], design["meal_pairs"], ("public_transport", "taxi")):
                candidate = make_plan(catalogs, key, hotel["id"], *meals, mode,
                                      dense=spec.get("dense", False), required=spec.get("required_witness_place"),
                                      return_id="train_return_08" if spec.get("late_return") else "train_return_07")
                if candidate:
                    pool.append(candidate)
            for candidate in pool:
                f = features(catalogs, public, candidate, context=context)
                if f["valid"]:
                    candidate["total_cost_kopecks"] = f["cost"]
                    candidate["validation_status"] = "benchmark_assumptions"
                    feasible.append((candidate, f))
            if not feasible:
                raise ValueError("No feasible witness: " + key)
            scored = sorted(feasible, key=lambda pair: (-utility(pair[1]["utility"], profile["weights"]), pair[1]["cost"], pair[0]["id"]))
            witness = feasible[0][0] if key == "dev_01" else scored[0][0]
            if spec.get("initial_hotel"):
                initial = [(p, f) for p, f in feasible if p["selected_stay"]["reference_id"] == spec["initial_hotel"]]
                witness = max(initial, key=lambda pair: pair[1]["cost"])[0]
            initial_score = utility(next(f for p, f in feasible if p is witness)["utility"], profile["weights"])
            if initial_score < .7:
                raise ValueError("Initial witness does not satisfy utility threshold: " + key)
            save(ROOT/"evaluation/benchmark/reference_plans"/f"{key}.json", witness)
            best = utility(scored[0][1]["utility"], profile["weights"])
            if best < .7:
                raise ValueError(f"No utility-success witness for {key}: {best}")
            alternatives = [p for p, f in sorted(feasible, key=lambda pair: pair[0]["id"])]
            save(ROOT/"evaluation/benchmark/alternatives"/f"{key}.json", {"mode": MODE, "scope": "finite sampled pool, not exhaustive", "plans": alternatives})
            # Keep a separate success witness when the preserved 45-event route is not utility-optimal.
            save(ROOT/"evaluation/benchmark/reference_plans"/f"{key}.success.json", scored[0][0])
            frontier = [(p, f) for p, f in feasible if not any(dominates(g, f) for _, g in feasible)]
            unique = []
            for p, f in scored:
                if all(distinct(p, f, q, g) for q, g in unique):
                    unique.append((p, f))
            row = {"id": key, "expected": "feasible", "validated_alternatives": len(feasible),
                   "distinct_alternatives": len(unique), "pareto_alternatives": len(frontier),
                   "min_cost_kopecks": min(f["cost"] for _, f in feasible), "max_cost_kopecks": max(f["cost"] for _, f in feasible),
                   "best_utility": best, "reference_utility": initial_score, "reference_events": len(witness["items"]), "reference_exit_code": 0}
            if case["changes"]:
                from .tools import ToolRegistry
                from .runtime import Runtime
                from .experiments import apply_change
                probe = ToolRegistry(catalogs, public, Runtime("synthetic-preflight"), context=context)
                for change in case["changes"]:
                    apply_change(probe, change)
                if validate_trip(probe.catalogs, probe.request, witness, context=probe.context).exit_code == 0:
                    raise ValueError("Intervention does not invalidate initial witness: " + key)
                final = [(p, features(probe.catalogs, probe.request, p, context=probe.context)) for p, _ in feasible]
                final = [(p, f) for p, f in final if f["valid"]]
                if not final:
                    raise ValueError("No changed-condition witness: " + key)
                final.sort(key=lambda pair: (-utility(pair[1]["utility"], profile["weights"]), pair[1]["cost"], pair[0]["id"]))
                if utility(final[0][1]["utility"], profile["weights"]) < .7:
                    raise ValueError("Final witness does not satisfy utility threshold: " + key)
                save(ROOT/"evaluation/benchmark/reference_plans"/f"{key}.final.json", final[0][0])
                row["final_exit_code"] = 0
                row["final_alternatives"] = len(final)
        else:
            proof = {"type": spec["certificate"], "mode": MODE, "snapshot_id": SNAPSHOT, "public_hash": digest(public),
                     "scope": "Closed synthetic catalog only; not a claim about real travel feasibility."}
            if not verify_conflict(catalogs, public, proof, context=context):
                raise ValueError("Invalid impossibility proof: " + key)
            save(ROOT/"evaluation/benchmark/feasibility"/f"{key}.json", proof)
            row = {"id": key, "expected": "infeasible", "certificate_valid": True, "validated_alternatives": 0}
        cases.append(case); quality.append(row)
    save(root/"suite.json", {"status": "synthetic_validated", "mode": MODE, "cases": cases})
    save(root/"quality.json", {"mode": MODE, "synthetic": True, "scenario_count": len(cases), "cases": quality,
                               "warning": "Witness/pool validation is not evidence of any LLM architecture performance."})
    manifest = read_json(root/"manifest.json")
    manifest.update(catalog_hash=digest(catalogs), assumptions_hash=context.registry_hash,
                    generator_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
    save(root/"manifest.json", manifest)
    return check_benchmark(root)


def check_benchmark(root):
    root = Path(root).resolve()
    issues, cases = [], []
    manifest = read_json(root/"manifest.json")
    prepared = prepare(root/"manifest.json", check=True)
    issues.extend(prepared["problems"])
    try:
        catalogs = load_catalogs(root/"snapshot")
        context = load_context(root, catalogs, MODE)
        if manifest.get("catalog_hash") != digest(catalogs) or manifest.get("assumptions_hash") != context.registry_hash:
            issues.append("Catalog/assumptions hash differs")
        if manifest.get("generator_sha256") != sha256(Path(__file__).read_bytes()).hexdigest():
            issues.append("Generator changed; regenerate and validate")
        suite = read_json(root/"suite.json")
        design = read_json(root/"design.json")
        specs = {s["id"]: s for s in design["scenarios"]}
        base = read_json(ROOT/"data/scenarios/dev/dev_01.json")
        private_design = read_json(ROOT/"evaluation/benchmark/design_profiles.json")
        expected = {f"dev_{i:02d}" for i in range(1, 5)} | {f"test_{i:02d}" for i in range(1, 13)}
        if len(suite["cases"]) != 16 or {c["id"] for c in suite["cases"]} != expected:
            issues.append("Expected four dev and twelve test scenarios")
        from .experiments import validate_case
        for case in suite["cases"]:
            try:
                request = load_public_scenario(root/"scenarios"/case["split"]/f"{case['id']}.json")
                profile = read_json(ROOT/"evaluation/benchmark/profiles"/f"{case['id']}.json")
                validate_profile(profile)
                spec = specs[case["id"]]
                if digest(request) != digest(parse_scenario(public_request(base, spec))) or case["expected"] != spec["expected"] or case["changes"] != spec.get("changes", []):
                    raise ValueError("Case differs from reproducible public design")
                expected_profile = {**private_design[case["id"]], "private_marker": f"PRIVATE_SYNTHETIC_{case['id']}", "status": "synthetic_assumption"}
                if profile != expected_profile:
                    raise ValueError("Profile differs from private design")
                if case["profile_hash"] != digest(profile):
                    raise ValueError("Profile hash differs")
                validate_case(root, case, catalogs, request, context=context)
                if case["expected"] == "feasible":
                    alternatives = read_json(ROOT/"evaluation/benchmark/alternatives"/f"{case['id']}.json")["plans"]
                    if len(alternatives) < 2:
                        raise ValueError("No substantive alternatives")
                    scores = []
                    for plan in alternatives:
                        f = features(catalogs, request, plan, context=context)
                        if not f["valid"]:
                            raise ValueError("Saved alternative failed")
                        scores.append(utility(f["utility"], profile["weights"]))
                    if max(scores) < .7:
                        raise ValueError("No success witness")
                public_text = json.dumps(jsonable(request)) + json.dumps(jsonable(catalogs))
                if profile["private_marker"] in public_text or '"weights"' in public_text:
                    raise ValueError("Hidden profile leaked")
                cases.append({"id": case["id"], "ready": True})
            except (OSError, ValueError, KeyError) as exc:
                cases.append({"id": case["id"], "ready": False, "reason": str(exc)})
    except (OSError, ValueError, KeyError) as exc:
        issues.append(str(exc))
    ready = not issues and len(cases) == 16 and all(c["ready"] for c in cases)
    return {"ready": ready, "status": "BENCHMARK_READY" if ready else "NOT_READY", "mode": MODE,
            "synthetic": True, "real_data_verified": False, "issues": issues, "cases": cases}
