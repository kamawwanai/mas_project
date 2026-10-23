"""Regressions from the technical audit. All offers here are synthetic fixtures."""
from copy import deepcopy
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import socket
import unittest

from src.catalog import load_catalogs, load_scenario, load_plan
from src.checks import validate_trip
from src.evaluator import verify_conflict
from src.evidence import valid_url
from src.llm import OpenAIClient
from src.runtime import Limits, Runtime, LimitExceeded
from src.schemas import parse_scenario, SchemaError
from src.selection import features, select
from tests.full_fixtures import full_data, parsed
from tests.test_llm_adapter import RateLimit, TimeoutFailure, ConnectionFailure


class ValidatorRegressions(unittest.TestCase):
    def report(self, data):
        catalogs, request, plan = parsed(data)
        return validate_trip(catalogs, request, plan)

    def test_unknown_time_does_not_imply_missing_transport_or_wrong_direction(self):
        for field in ("start_at", "end_at"):
            data = full_data()
            data["plan"]["items"][0][field] = None
            report = self.report(data)
            self.assertEqual(3, report.exit_code, report.to_dict())
            self.assertEqual([], [p.code for c in report.checks for p in c.violations])

    def test_missing_event_and_independent_known_violation_still_fail(self):
        data = full_data()
        data["plan"]["items"].pop(0)
        self.assertIn("EVENT_COVERAGE", [p.code for c in self.report(data).checks for p in c.violations])
        data = full_data()
        data["plan"]["items"][0]["start_at"] = None
        data["plan"]["items"][4]["end_at"] = data["plan"]["items"][4]["start_at"]
        self.assertEqual(1, self.report(data).exit_code)

    def test_rest_split_before_at_and_after_midnight_is_invariant(self):
        for split in ("2026-10-24T23:00:00+03:00", "2026-10-25T00:00:00+03:00", "2026-10-25T03:00:00+03:00"):
            data = full_data()
            first = data["plan"]["items"][8]
            second = deepcopy(first)
            first["end_at"] = split
            second.update(id="rest-part-2", start_at=split, day=1 if "10-24" in split else 2)
            data["plan"]["items"].insert(9, second)
            self.assertEqual(0, self.report(data).exit_code, self.report(data).to_dict())

    def test_rest_gap_is_not_filled(self):
        data = full_data()
        data["plan"]["items"][8]["end_at"] = "2026-10-24T23:59:00+03:00"
        self.assertIn("REST_COVERAGE", [p.code for c in self.report(data).checks for p in c.violations])

    def test_malformed_url_does_not_confirm_source(self):
        for url in ("https:///", "https://", "file:///tmp/a", "https://a b.test", "https://x:wrong", "https://user:pass@host.test", "https://host.test\\evil"):
            self.assertFalse(valid_url(url), url)
            data = full_data()
            data["places"]["items"][0]["source_url"] = url
            self.assertEqual(3, self.report(data).exit_code)
        self.assertTrue(valid_url("https://example.test/path?q=1"))

    def test_bad_timezone_is_structured_for_parser_and_typed_callers(self):
        data = full_data()
        data["scenario"]["timezone"] = "NoSuch/Timezone"
        with self.assertRaises(SchemaError) as error:
            parse_scenario(data["scenario"])
        self.assertEqual("$.timezone", error.exception.path)
        c, r, p = parsed()
        report = validate_trip(c, replace(r, timezone="NoSuch/Timezone"), p)
        self.assertEqual(2, report.exit_code)
        self.assertEqual("INVALID_TIMEZONE", report.input_errors[0].code)

    def booking_data(self):
        data = full_data()
        item = data["places"]["items"][0]
        item["booking_required"] = True
        item["booking_confirmations"] = [{"id": "fixture-booking", "start_at": "2026-10-24T10:00:00+03:00",
            "end_at": "2026-10-25T12:00:00+03:00", "adults": 2,
            "source_url": "https://example.test/synthetic-booking", "checked_at": "2026-10-01T10:00:00+03:00"}]
        item["booking_confirmations"] = []
        for event in data["plan"]["items"]:
            if event["type"] == "visit":
                booking_id = "fixture-booking-" + event["id"]
                item["booking_confirmations"].append({"id": booking_id, "start_at": event["start_at"],
                    "end_at": event["end_at"], "adults": 2, "source_url": "https://example.test/fixture", "checked_at": "2026-10-01T10:00:00+03:00"})
                event["booking_id"] = booking_id
        return data

    def test_saved_booking_covers_time_and_party(self):
        self.assertEqual(0, self.report(self.booking_data()).exit_code)

    def test_booking_missing_forged_wrong_time_or_party(self):
        for mutation, expected in ((lambda d: d["places"]["items"][0].update(booking_confirmations=[]), 1),
            (lambda d: d["places"]["items"][0]["booking_confirmations"][0].update(adults=1), 1),
            (lambda d: d["places"]["items"][0]["booking_confirmations"][0].update(end_at="2026-10-24T10:41:00+03:00"), 1),
            (lambda d: d["places"]["items"][0]["booking_confirmations"][0].update(checked_at=None), 3)):
            data = self.booking_data()
            mutation(data)
            self.assertEqual(expected, self.report(data).exit_code)

    def test_legacy_booking_requirement_remains_unknown(self):
        data = full_data()
        data["food"]["items"][0]["reservation_required"] = True
        self.assertEqual(3, self.report(data).exit_code)

    def transfer_data(self):
        data = full_data()
        row = data["transfers"]["items"][0]
        row.update(mode="public_transport", walking_minutes=2)
        row["verified_fields"] = row["verified_fields"] + ["departure_windows"]
        return data

    def test_public_transport_windows_unknown_matching_and_closed(self):
        data = self.transfer_data()
        self.assertEqual(3, self.report(data).exit_code)
        row = data["transfers"]["items"][0]
        row["departure_windows"] = [{"start_at": "2026-10-24T09:00:00+03:00", "end_at": "2026-10-24T11:00:00+03:00"}]
        self.assertEqual(0, self.report(data).exit_code)
        row["departure_windows"][0]["end_at"] = "2026-10-24T10:00:00+03:00"
        self.assertEqual(1, self.report(data).exit_code)
        row["departure_windows"] = []
        self.assertEqual(1, self.report(data).exit_code)


class CertificateRegressions(unittest.TestCase):
    def test_resource_fields_cannot_override_independent_scores(self):
        from src.evaluator import Evaluator
        from src.contracts import RunResult
        c, r, _ = parsed()
        result = RunResult("test", "single_agent", "case", "hash", resources={"success_at_1": True}).to_dict()
        value = Evaluator().evaluate(c, r, result, {"weights": {"culture": 1}})
        self.assertFalse(value["metrics"]["success_at_1"])

    def test_synthetic_estimated_fees_units_and_unconfirmed_prices_are_rejected(self):
        for change in ({"price_kind": "indicative_test_value"}, {"price_kind": "estimated"},
                       {"includes_mandatory_fees": None}, {"amount_kopecks": None}, {"price_unit": "group_trip"}):
            data = full_data()
            data["transport"]["items"][0]["price"].update(change)
            c, r, _ = parsed(data)
            self.assertFalse(verify_conflict(c, replace(r, budget_kopecks=1), {"type": "budget_lower_bound"}), change)
        data = full_data()
        data["transport"]["items"][0]["verified_fields"] = []
        c, r, _ = parsed(data)
        self.assertFalse(verify_conflict(c, replace(r, budget_kopecks=1), {"type": "budget_lower_bound"}))

    def test_lower_bound_respects_adults_rooms_and_equality(self):
        c, r, _ = parsed()
        for budget, result in ((49999, True), (50000, False)):
            self.assertEqual(result, verify_conflict(c, replace(r, budget_kopecks=budget), {"type": "budget_lower_bound"}))
        data = full_data()
        data["stays"]["items"][0].update(requested_rooms=2, room_capacity_adults=1)
        c, r, _ = parsed(data)
        self.assertTrue(verify_conflict(c, replace(r, budget_kopecks=59999), {"type": "budget_lower_bound"}))
        self.assertFalse(verify_conflict(c, replace(r, adults=3, budget_kopecks=1), {"type": "budget_lower_bound"}))

    def test_unknown_cheaper_offer_blocks_certificate(self):
        data = full_data()
        other = deepcopy(data["transport"]["items"][0])
        other.update(id="unknown-cheap")
        other["price"].update(amount_kopecks=None, price_kind="unknown")
        data["transport"]["items"].append(other)
        c, r, _ = parsed(data)
        self.assertFalse(verify_conflict(c, replace(r, budget_kopecks=1), {"type": "budget_lower_bound"}))

    def test_closed_place_requires_all_dates_and_evidence(self):
        data = full_data()
        for row in data["places"]["items"][0]["opening_hours"]:
            row.update(status="closed", intervals=[])
        c, r, _ = parsed(data)
        self.assertTrue(verify_conflict(c, r, {"type": "required_place_closed"}))
        data["places"]["items"][0]["opening_hours"].pop()
        c, r, _ = parsed(data)
        self.assertFalse(verify_conflict(c, r, {"type": "required_place_closed"}))


class SelectionRegressions(unittest.TestCase):
    def test_food_alternatives_survive_and_order_does_not_matter(self):
        data = full_data()
        other = deepcopy(data["food"]["items"][0])
        other.update(id="food_2")
        other["features"].update(food=0)
        other["price"]["amount_kopecks"] = 5000
        data["food"]["items"].append(other)
        for row in list(data["transfers"]["items"]):
            if "food_1" in [row["from_id"], row["to_id"]]:
                copy = deepcopy(row)
                copy["id"] += "_food2"
                for key in ("from_id", "to_id"):
                    if copy[key] == "food_1":
                        copy[key] = "food_2"
                data["transfers"]["items"].append(copy)
        c, r, a = parsed(data)
        b = deepcopy(a)
        b["id"] = "food-alternative"
        ids = {row["id"] for row in data["transfers"]["items"]}
        for e in b["items"]:
            if e["reference_id"] == "food_1":
                e["reference_id"] = "food_2"
            elif e["type"] == "transfer" and e["reference_id"]+"_food2" in ids:
                e["reference_id"] += "_food2"
        self.assertTrue(features(c, r, b)["valid"])
        results = [select(c, r, list(order))[0] for order in permutations([a, b])]
        self.assertEqual(results[0], results[1])
        self.assertEqual(2, len(results[0]))

    def test_same_route_keeps_better_travel_variant_independent_of_order(self):
        c, r, a = parsed()
        b = deepcopy(a)
        b["id"] = "renamed"
        b["items"][0]["label"] = "Text only"
        self.assertEqual(select(c, r, [a, b])[0], select(c, r, [b, a])[0])
        self.assertEqual(1, len(select(c, r, [a, a])[0]))

    def test_more_expensive_duplicate_cannot_hide_cheaper_plan(self):
        data = full_data()
        costly = deepcopy(data["transfers"]["items"][0])
        costly["id"] = "costly-equivalent"
        costly["price"].update(amount_kopecks=1000, price_kind="listed")
        data["transfers"]["items"].append(costly)
        c, r, cheap = parsed(data)
        expensive = deepcopy(cheap)
        expensive["id"] = "expensive"
        expensive["items"][1]["reference_id"] = costly["id"]
        self.assertTrue(features(c, r, expensive)["valid"])
        for order in permutations([expensive, cheap]):
            self.assertEqual([cheap], select(c, r, list(order))[0])


class ResourceRegressions(unittest.TestCase):
    rates = {"input_per_million": 1, "cached_input_per_million": .5, "output_per_million": 2}

    def test_unknown_usage_blocks_monetary_run(self):
        runtime = Runtime("test", limits=Limits(cost_usd=1), pricing=self.rates)
        with self.assertRaises(LimitExceeded):
            runtime.record_usage(None)
        self.assertIsNone(runtime.summary()["cost_usd"])
        with self.assertRaises(LimitExceeded):
            runtime.reserve_llm(10)

    def test_invalid_cached_usage_and_pricing_are_rejected(self):
        runtime = Runtime("test", limits=Limits(cost_usd=1), pricing=self.rates)
        with self.assertRaises(LimitExceeded):
            runtime.record_usage({"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 2}})
        for value in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Runtime("test", pricing={**self.rates, "input_per_million": value})

    def test_timeout_connection_and_server_failure_stop_before_retry(self):
        server = RuntimeError("server")
        server.status_code = 503
        for failure in (TimeoutFailure(), ConnectionFailure(), server):
            create = Mock(side_effect=failure)
            sdk = SimpleNamespace(OpenAI=Mock(return_value=SimpleNamespace(responses=SimpleNamespace(create=create))),
                                  RateLimitError=RateLimit, APIConnectionError=ConnectionFailure, APITimeoutError=TimeoutFailure)
            with patch.dict("sys.modules", {"openai": sdk}), patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch("src.llm.time.sleep") as sleep:
                client = OpenAIClient({"api_enabled": True, "model": "test-model"}, allow_api=True)
                runtime = Runtime("test", limits=Limits(cost_usd=1), pricing=self.rates)
                with self.assertRaises(LimitExceeded):
                    client.generate([], [], runtime, "test")
                self.assertEqual(1, create.call_count)
                self.assertIsNone(runtime.summary()["cost_usd"])
                sleep.assert_not_called()


class RepositoryRegressions(unittest.TestCase):
    def test_invalid_private_profile_fails_preflight(self):
        from src.evaluator import validate_profile
        for profile in ({}, {"weights": {"food": float("nan")}},
                        {"weights": {"food": 1}, "answers": {"all_private_data": "expose"}}):
            with self.assertRaises(ValueError):
                validate_profile(profile)

    def test_canonical_alias_and_reference_remain_honestly_incomplete(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(load_scenario(root/"data/dev/dev_01.json"), load_scenario(root/"data/scenarios/dev/dev_01.json"))
        report = validate_trip(load_catalogs(root/"data/snapshot"), load_scenario(root/"data/scenarios/dev/dev_01.json"), load_plan(root/"data/reference_plans/dev_01.json"))
        self.assertEqual(3, report.exit_code)
        self.assertIsNone(report.cost["total_kopecks"])

    def test_suite_is_distinct_with_private_profiles(self):
        from src.contracts import read_json, digest
        root = Path(__file__).resolve().parents[1]
        suite = read_json(root/"data/benchmark/suite.json")
        normalized = set()
        for case in suite["cases"]:
            path = root/"data/benchmark/scenarios"/case["split"]/f"{case['id']}.json"
            raw = read_json(path)
            self.assertEqual(digest(load_scenario(path)), case["public_hash"])
            profile = read_json(root/"evaluation/benchmark/profiles"/f"{case['id']}.json")
            self.assertEqual(digest(profile), case["profile_hash"])
            self.assertNotIn("weights", json.dumps(raw))
            raw.pop("id"); raw.pop("notes")
            normalized.add(digest(raw))
            self.assertEqual([], case["pending"])
            self.assertEqual("synthetic_validated", case["status"])
        self.assertEqual(16, len(normalized))
