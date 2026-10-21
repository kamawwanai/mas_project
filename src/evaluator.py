"""Private deterministic evaluation; never exposed through planner tools."""
from .selection import features, utility, distinct


def verify_conflict(catalogs, request, proof):
    """Only support certificates that can actually be checked from the snapshot."""
    if proof.get("type") == "budget_lower_bound":
        def confirmed_price(item):
            source = item.source
            return bool(source.source_url and source.checked_at and "price" in source.verified_fields and item.price.price_kind != "unknown" and item.price.currency == request.currency)
        candidates = [i for i in catalogs["transport"].items if i.availability_status != "unavailable" and i.service_date in {request.start_date, request.end_date}]
        stay_candidates = [i for i in catalogs["stays"].items if i.check_in_date == request.start_date and i.check_out_date == request.end_date and i.availability_status != "unavailable"]
        if not all(confirmed_price(i) for i in candidates+stay_candidates):
            return False
        outgoing = [i.price.amount_kopecks for i in candidates if i.direction == "outbound" and i.service_date == request.start_date]
        returning = [i.price.amount_kopecks for i in candidates if i.direction == "return" and i.service_date == request.end_date]
        # A lower bound over all potential room arrangements must not assume one room.
        stays = [i.price.amount_kopecks for i in stay_candidates]
        if not all((outgoing, returning, stays)) or any(x is None for x in outgoing+returning+stays):
            return False
        lower = (min(outgoing)+min(returning))*request.adults + min(stays)*request.nights
        return request.budget_kopecks is not None and lower > request.budget_kopecks
    if proof.get("type") == "required_place_closed":
        places = {i.id: i for i in catalogs["places"].items}
        for key in request.constraints.required_place_ids:
            item = places.get(key)
            if not item or not item.source.source_url or not item.source.checked_at or "opening_hours" not in item.source.verified_fields:
                continue
            dates = {h.date: h.status for h in item.opening_hours}
            from datetime import timedelta
            days = [request.start_date+timedelta(days=i) for i in range((request.end_date-request.start_date).days+1)]
            if all(dates.get(day) == "closed" for day in days):
                return True
    return False


class Evaluator:
    def evaluate(self, catalogs, request, result, profile, expected="feasible", proof=None):
        rows, distinct_valid = [], []
        plans = result.get("plans", [])
        if len(plans) > 5:
            plans = plans[:5]
        for plan in plans:
            try:
                f = features(catalogs, request, plan)
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
            "conflict_correct": bool(result.get("status") == "no_feasible_plan" and not rows and verify_conflict(catalogs, request, proof or {})) if expected == "infeasible" else None,
            **result.get("resources", {}),
        }
        return {"scenario_id": request.id, "run_id": result["run_id"], "architecture": result["architecture"],
                "mode": result["mode"], "metrics": metrics, "plans": rows}
