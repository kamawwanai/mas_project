"""Private deterministic evaluation; never exposed through planner tools."""
from .selection import features, utility, distinct


def validate_profile(profile):
    """Reject malformed private evaluation inputs before any provider construction."""
    if not isinstance(profile, dict):
        raise ValueError("Private profile must be an object")
    from .selection import FEATURES
    utility(dict.fromkeys(FEATURES, 0), profile.get("weights"))
    answers = profile.get("answers", {})
    if not isinstance(answers, dict) or set(answers)-{"interests", "pace", "comfort", "economy", "quiet", "location"}:
        raise ValueError("Invalid private answer topics")
    for key, value in answers.items():
        if key == "interests":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("Interests answer must be a list of strings")
        elif not isinstance(value, str):
            raise ValueError("Preference answer must be a string")


def verify_conflict(catalogs, request, proof, *, context=None):
    """Conservative certificates over saved, confirmed facts only."""
    from math import ceil
    from datetime import timedelta
    from .evidence import supported_fields, synthetic_source
    if context:
        context.verify(catalogs)
        if request.snapshot_id != context.snapshot_id:
            raise ValueError("Certificate and assumptions differ")
        from .contracts import digest
        if proof.get("mode") != "benchmark_assumptions" or proof.get("snapshot_id") != context.snapshot_id or proof.get("public_hash") != digest(request):
            return False

    def confirmed(item, fields):
        if context and all(context.supports(item, f) for f in fields):
            return True
        return fields <= supported_fields(item.source) and not synthetic_source(item.source)

    def price_ok(item, unit):
        p = item.price
        return (confirmed(item, {"price"}) and p.price_unit == unit and
                p.currency == request.currency and (p.price_kind in {"listed", "free"} or
                    context is not None and p.price_kind == "synthetic_assumption") and
                p.amount_kopecks is not None and p.includes_mandatory_fees is True)

    if proof.get("type") == "budget_lower_bound":
        if request.budget_kopecks is None or request.nights < 1:
            return False
        groups = []
        for direction, day, origin, destination in (
            ("outbound", request.start_date, request.origin_city, request.destination_city),
            ("return", request.end_date, request.destination_city, request.origin_city)):
            offers = [i for i in catalogs["transport"].items if i.direction == direction and
                      i.service_date == day and i.availability_status != "unavailable" and
                      (i.from_station.city, i.to_station.city) == (origin, destination)]
            if not offers or not all(price_ok(i, "adult_ticket") for i in offers):
                return False
            groups.append(min(i.price.amount_kopecks for i in offers)*request.adults)
        stays = [i for i in catalogs["stays"].items if
                 (i.check_in_date, i.check_out_date, i.city) ==
                 (request.start_date, request.end_date, request.destination_city) and
                 i.availability_status != "unavailable"]
        if not stays or not all(price_ok(i, "room_night") and confirmed(i, {"capacity"}) and
                                i.room_capacity_adults is not None and
                                i.requested_adults == request.adults and
                                i.requested_rooms * i.room_capacity_adults >= request.adults
                                for i in stays):
            return False
        minimum_stay = min(i.price.amount_kopecks *
                           max(i.requested_rooms, ceil(request.adults/i.room_capacity_adults)) * request.nights
                           for i in stays)
        return sum(groups)+minimum_stay > request.budget_kopecks
    if proof.get("type") == "required_place_closed":
        for key in request.constraints.required_place_ids:
            matches = [i for i in catalogs["places"].items if i.id == key]
            if len(matches) != 1 or not confirmed(matches[0], {"opening_hours"}):
                continue
            item = matches[0]
            days = [request.start_date+timedelta(days=i) for i in range((request.end_date-request.start_date).days+1)]
            if all(any(h.date == day for h in item.opening_hours) and
                   all(h.status == "closed" for h in item.opening_hours if h.date == day) for day in days):
                return True
    return False


class Evaluator:
    def evaluate(self, catalogs, request, result, profile, expected="feasible", proof=None, *, context=None):
        rows, distinct_valid = [], []
        plans = result.get("plans", [])
        if len(plans) > 5:
            plans = plans[:5]
        for plan in plans:
            try:
                f = features(catalogs, request, plan, context=context)
                score = utility(f["utility"], profile["weights"]) if f["valid"] else 0.0
                is_distinct = all(distinct(plan, f, p, pf) for p, pf in distinct_valid)
                if f["valid"] and is_distinct:
                    distinct_valid.append((plan, f))
                rows.append({"valid": f["valid"], "distinct": is_distinct, "utility": score,
                             "success": f["valid"] and is_distinct and score >= .7, "report": f["report"]})
            except (ValueError, TypeError, KeyError) as exc:
                rows.append({"valid": False, "distinct": False, "utility": 0, "success": False, "error": str(exc)})
        metrics = {
            "expected": expected,
            "success_at_1": bool(rows and rows[0]["success"]) if expected == "feasible" else None,
            "success_at_5": any(r["success"] for r in rows) if expected == "feasible" else None,
            "feasible_at_1": bool(rows and rows[0]["valid"]) if expected == "feasible" else None,
            "feasible_at_5": any(r["valid"] for r in rows) if expected == "feasible" else None,
            "valid_fraction": sum(r["valid"] for r in rows)/len(rows) if rows else 0,
            "utility_at_1": rows[0]["utility"] if rows else 0,
            "best_utility": max((r["utility"] for r in rows), default=0),
            "conflict_correct": bool(result.get("status") == "no_feasible_plan" and not rows and verify_conflict(catalogs, request, proof or {}, context=context)) if expected == "infeasible" else None,
        }
        metrics.update({k: v for k, v in result.get("resources", {}).items() if k not in metrics})
        return {"scenario_id": request.id, "run_id": result["run_id"], "architecture": result["architecture"],
                "evaluation_mode": "benchmark_assumptions" if context else "strict",
                "assumptions_hash": context.registry_hash if context else None,
                "mode": result["mode"], "metrics": metrics, "plans": rows}
