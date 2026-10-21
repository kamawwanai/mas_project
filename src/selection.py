"""Public features, substantive diversity, Pareto filtering and ranking."""
from __future__ import annotations
from statistics import mean
from .checks import validate_trip
from .schemas import parse_plan

FEATURES = {"culture", "food", "comfort", "pace", "economy"}


def features(catalogs, request, raw):
    plan = parse_plan(raw) if isinstance(raw, dict) else raw
    report = validate_trip(catalogs, request, plan)
    by_id = {i.id: i for d in catalogs.values() for i in d.items}
    chosen = [by_id[e.reference_id] for e in plan.items if e.type in {"visit", "meal"} and e.reference_id in by_id]
    if plan.selected_stay.reference_id in by_id:
        chosen.append(by_id[plan.selected_stay.reference_id])
    values = {}
    for feature in ("culture", "food", "comfort"):
        scores = [i.source.features[feature]/100 for i in chosen if feature in i.source.features and "features" in i.source.verified_fields]
        values[feature] = mean(scores) if scores else 0.0
    walking, travel = 0, 0
    modes = []
    for event in plan.items:
        item = by_id.get(event.reference_id)
        if event.type == "transfer" and item:
            walking += (item.duration_minutes or 0) if item.mode == "walk" else (item.walking_minutes or 0)
            travel += item.duration_minutes or 0
            modes.append(item.mode)
        elif event.type == "transport" and event.start_at and event.end_at:
            travel += (event.end_at-event.start_at).total_seconds()/60
    days = (request.end_date-request.start_date).days+1
    cost = (report.cost or {}).get("total_kopecks")
    values["pace"] = max(0, 1-walking/(120*days))
    values["economy"] = max(0, 1-cost/request.budget_kopecks) if cost is not None and request.budget_kopecks else 0.0
    return {"utility": values, "cost": cost, "walking": walking, "travel": travel,
            "valid": report.exit_code == 0, "modes": tuple(modes), "report": report.to_dict()}


def identity(plan):
    return (plan["selected_stay"]["reference_id"],
            tuple(e["reference_id"] for e in plan["items"] if e["type"] == "transport"),
            tuple(sorted(e["reference_id"] for e in plan["items"] if e["type"] == "visit")))


def distinct(left, lf, right, rf):
    return identity(left) != identity(right) or (lf["modes"] != rf["modes"] and abs(lf["walking"]-rf["walking"]) >= 30)


def utility(values, weights):
    if not weights or set(weights)-FEATURES or any(type(w) not in (int, float) or w < 0 for w in weights.values()) or abs(sum(weights.values())-1) > 1e-6:
        raise ValueError("Utility weights must be nonnegative, known and sum to one")
    return sum(values[k]*v for k, v in weights.items())


def dominates(a, b):
    av = [-a["cost"], -a["walking"], -a["travel"], *[a["utility"][k] for k in ("culture", "food", "comfort")]]
    bv = [-b["cost"], -b["walking"], -b["travel"], *[b["utility"][k] for k in ("culture", "food", "comfort")]]
    return all(x >= y for x, y in zip(av, bv)) and any(x > y for x, y in zip(av, bv))


def public_weights(request, revealed):
    weights = dict.fromkeys(sorted(FEATURES), 1.0)
    interests = revealed.get("interests", request.preferences.interests)
    if not isinstance(interests, list):
        interests = [interests]
    if any(i in interests for i in ("museums", "culture")):
        weights["culture"] += 1
    if any(i in interests for i in ("local_food", "food")):
        weights["food"] += 1
    if revealed.get("pace", request.preferences.pace) == "relaxed":
        weights["pace"] += 1
    if revealed.get("economy") == "save":
        weights["economy"] += 1
    if revealed.get("comfort") == "high":
        weights["comfort"] += 1
    total = sum(weights.values())
    return {k: v/total for k, v in weights.items()}


def select(catalogs, request, candidates, revealed=None):
    unique, rejected = [], []
    for raw in candidates:
        try:
            f = features(catalogs, request, raw)
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"reason": "schema", "message": str(exc), "plan": raw})
            continue
        if not f["valid"]:
            rejected.append({"reason": "invalid", "report": f["report"], "plan": raw})
        elif all(distinct(raw, f, p, pf) for p, pf in unique):
            unique.append((raw, f))
    frontier = [(p, f) for p, f in unique if not any(dominates(g, f) for _, g in unique)]
    weights = public_weights(request, revealed or {})
    frontier.sort(key=lambda pair: (-utility(pair[1]["utility"], weights), pair[1]["cost"], str(identity(pair[0]))))
    return [p for p, _ in frontier[:5]], rejected
