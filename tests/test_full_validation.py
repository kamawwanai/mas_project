import unittest
from copy import deepcopy

from src.checks import validate_trip, calculate_cost
from src.schemas import parse_plan
from tests.full_fixtures import full_data, parsed


class FullValidationTests(unittest.TestCase):
    def codes(self, data):
        catalogs, request, plan = parsed(data)
        report = validate_trip(catalogs, request, plan)
        return report, {p.code for check in report.checks for p in check.violations+check.incomplete_data}

    def test_complete_route_and_independent_expected_cost(self):
        catalogs, request, plan = parsed()
        report = validate_trip(catalogs, request, plan)
        self.assertEqual(0, report.exit_code, report.to_dict())
        self.assertEqual(130000, report.cost["total_kopecks"])
        self.assertTrue(report.full_trip_feasibility_evaluated)

    def test_hard_constraint_mutations(self):
        mutations = [
            ("BUDGET_EXCEEDED", lambda d: d["scenario"].update(budget_kopecks=129999)),
            ("WRONG_QUANTITY", lambda d: d["plan"]["items"][0].update(quantity=1)),
            ("TOTAL_COST_MISMATCH", lambda d: d["plan"].update(total_cost_kopecks=1)),
            ("LOCATION_DISCONTINUITY", lambda d: d["plan"]["items"].pop(3)),
            ("WALKING_LIMIT", lambda d: d["scenario"]["constraints"].update(max_walking_minutes_per_day=59)),
            ("TIMETABLE_MISMATCH", lambda d: d["transport"]["items"][0].update(arrival_at="2026-10-24T10:01:00+03:00")),
            ("CHECK_IN_TIME", lambda d: d["stays"]["items"][0].update(check_in_from="14:00")),
            ("ROOM_CAPACITY", lambda d: d["stays"]["items"][0].update(room_capacity_adults=1)),
            ("UNAVAILABLE", lambda d: d["transport"]["items"][0].update(availability_status="unavailable")),
            ("OUTSIDE_OPENING_HOURS", lambda d: d["places"]["items"][0]["opening_hours"][0].update(status="closed", intervals=[])),
            ("LUGGAGE_UNHANDLED", lambda d: (d["scenario"]["constraints"].update(luggage_required=True), d["plan"]["items"].__setitem__(slice(2,3), []))),
            ("DEPARTURE_BUFFER", lambda d: d["plan"]["items"][-2].update(start_at="2026-10-25T12:10:00+03:00")),
        ]
        for code, mutate in mutations:
            with self.subTest(code=code):
                data = full_data()
                mutate(data)
                report, codes = self.codes(data)
                self.assertEqual(1, report.exit_code, report.to_dict())
                self.assertIn(code, codes)

    def test_unknown_is_incomplete_not_free(self):
        for field, value, code in [("amount_kopecks", None, "UNKNOWN_FULL_PRICE"), ("includes_mandatory_fees", None, "UNKNOWN_FULL_PRICE")]:
            with self.subTest(field=field):
                data = full_data()
                data["stays"]["items"][0]["price"][field] = value
                report, codes = self.codes(data)
                self.assertEqual(3, report.exit_code)
                self.assertIsNone(report.cost["total_kopecks"])
                self.assertIn(code, codes)

    def test_unknown_evidence_and_date_do_not_pass(self):
        data = full_data()
        data["places"]["items"][0]["verified_fields"] = []
        report, codes = self.codes(data)
        self.assertEqual(3, report.exit_code)
        self.assertIn("UNSUPPORTED_FACTS", codes)
        data = full_data()
        data["transfers"]["items"][0]["applicable_date"] = "2026-10-23"
        self.assertIn("TRANSFER_DATE", self.codes(data)[1])

    def test_vehicle_charges_group_not_people(self):
        data = full_data()
        row = data["transfers"]["items"][0]
        row.update(mode="taxi", vehicle_capacity_adults=4, walking_minutes=0)
        row["price"].update(price_unit="vehicle_trip", amount_kopecks=5000, price_kind="listed")
        data["plan"]["items"][1]["quantity"] = 1
        report, _ = self.codes(data)
        self.assertEqual(0, report.exit_code, report.to_dict())
        self.assertEqual(135000, report.cost["total_kopecks"])

    def test_schema_error_is_structured(self):
        catalogs, request, plan = parsed()
        plan["items"][0]["start_at"] = "bad"
        self.assertEqual(2, validate_trip(catalogs, request, plan).exit_code)


if __name__ == "__main__":
    unittest.main()
