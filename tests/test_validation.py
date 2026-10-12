from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager

from src.validator import validate_scenario
from tests.fixtures import clone, event, make_dataset, write_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP = PROJECT_ROOT / "tests" / ".tmp"


@contextmanager
def working_directory():
    """Use normal directory permissions; MSYS tempfile creates unusable ACLs here."""

    path = TEST_TEMP / f"case_{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def check(report, name: str):
    return next(item for item in report.checks if item.name == name)


class ValidationTests(unittest.TestCase):
    def validate(self, data):
        with working_directory() as temp:
            root = write_dataset(temp, data)
            return validate_scenario(str(root), "case")

    def test_project_templates_load_and_are_incomplete(self):
        report = validate_scenario(str(PROJECT_ROOT / "data"), "dev_01")
        self.assertEqual([], report.input_errors)
        self.assertEqual(3, report.exit_code)
        self.assertEqual("incomplete", check(report, "event_intervals").status)
        self.assertEqual(26, report.event_count)

    def test_adjacent_events_and_overnight_interval_pass(self):
        report = self.validate(make_dataset())
        self.assertEqual(0, report.exit_code)
        self.assertEqual("passed", check(report, "event_intervals").status)
        self.assertEqual("passed", check(report, "event_overlaps").status)
        self.assertEqual(3, report.valid_complete_interval_count)

    def test_equal_and_reversed_boundaries_fail(self):
        events = [
            event("equal", "transport", "train_1", "2026-10-24T09:00:00+03:00", "2026-10-24T09:00:00+03:00"),
            event("reverse", "visit", "place_1", "2026-10-24T11:00:00+03:00", "2026-10-24T10:00:00+03:00"),
        ]
        report = self.validate(make_dataset(events))
        interval_check = check(report, "event_intervals")
        self.assertEqual(1, report.exit_code)
        self.assertEqual(2, len(interval_check.violations))
        self.assertTrue(all(item.code == "INVALID_EVENT_INTERVAL" for item in interval_check.violations))

    def test_nested_and_ordinary_overlaps_are_all_found(self):
        events = [
            event("outer", "transport", "train_1", "2026-10-24T09:00:00+03:00", "2026-10-24T14:00:00+03:00"),
            event("inner1", "transport", "train_1", "2026-10-24T10:00:00+03:00", "2026-10-24T11:00:00+03:00"),
            event("inner2", "transport", "train_1", "2026-10-24T12:00:00+03:00", "2026-10-24T13:00:00+03:00"),
        ]
        report = self.validate(make_dataset(events))
        overlaps = check(report, "event_overlaps")
        self.assertEqual("failed", overlaps.status)
        pairs = {tuple(item.related_ids) for item in overlaps.violations}
        self.assertEqual({("outer", "inner1"), ("outer", "inner2")}, pairs)

    def test_known_overlap_wins_over_incomplete_event(self):
        events = [
            event("a", "transport", "train_1", "2026-10-24T09:00:00+03:00", "2026-10-24T11:00:00+03:00"),
            event("unknown", "visit", "place_1", None, None),
            event("b", "transport", "train_1", "2026-10-24T10:00:00+03:00", "2026-10-24T12:00:00+03:00"),
        ]
        report = self.validate(make_dataset(events))
        overlaps = check(report, "event_overlaps")
        self.assertEqual(1, report.exit_code)
        self.assertEqual("failed", overlaps.status)
        self.assertEqual(1, len(overlaps.violations))
        self.assertTrue(overlaps.incomplete_data)

    def test_missing_wrong_type_and_duplicate_references(self):
        data = make_dataset(
            [
                event("same", "visit", "missing", None, None),
                event("same", "visit", "food_1", None, None),
            ]
        )
        report = self.validate(data)
        identifier = check(report, "identifier_uniqueness")
        references = check(report, "event_references")
        self.assertIn("DUPLICATE_EVENT_ID", {item.code for item in identifier.violations})
        self.assertEqual(
            {"EVENT_REFERENCE_NOT_FOUND", "EVENT_REFERENCE_WRONG_TYPE"},
            {item.code for item in references.violations},
        )

        duplicate_catalog = clone(make_dataset())
        duplicate_catalog["places"]["items"].append(clone(duplicate_catalog)["places"]["items"][0])
        duplicate_report = self.validate(duplicate_catalog)
        self.assertIn(
            "DUPLICATE_CATALOG_ID",
            {item.code for item in check(duplicate_report, "identifier_uniqueness").violations},
        )

    def test_station_repeat_merges_unknown_and_detects_known_conflict(self):
        data = make_dataset()
        repeated = clone(data)["transport"]["items"][0]
        repeated["id"] = "train_2"
        repeated["from_station"]["name"] = "А"
        data["transport"]["items"].append(repeated)
        report = self.validate(data)
        self.assertEqual("passed", check(report, "station_consistency").status)

        conflict = clone(data)
        conflict["transport"]["items"][1]["from_station"]["city"] = "Другой город"
        conflict_report = self.validate(conflict)
        self.assertIn(
            "STATION_CONFLICT",
            {item.code for item in check(conflict_report, "station_consistency").violations},
        )

    def test_input_errors_are_diagnostic_and_exit_two(self):
        mutations = {
            "money": lambda root, data: data["stays"]["items"][0]["price"].__setitem__("amount_kopecks", "100"),
            "timezone": lambda root, data: data["plan"]["items"][0].__setitem__("start_at", "2026-10-24T09:00:00"),
            "empty_plan": lambda root, data: data["plan"].__setitem__("items", []),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = make_dataset()
                with working_directory() as root:
                    mutate(root, data)
                    write_dataset(root, data)
                    report = validate_scenario(str(root), "case")
                    self.assertEqual(2, report.exit_code)
                    self.assertEqual("SCHEMA_ERROR", report.input_errors[0].code)
                    self.assertIn("поле $", report.input_errors[0].message)

        with working_directory() as temp:
            root = write_dataset(temp, make_dataset())
            (root / "snapshot" / "food.json").unlink()
            report = validate_scenario(str(root), "case")
            self.assertEqual("FILE_NOT_FOUND", report.input_errors[0].code)
            self.assertEqual(2, report.exit_code)

        with working_directory() as temp:
            root = write_dataset(temp, make_dataset())
            (root / "snapshot" / "food.json").write_text("{broken", encoding="utf-8")
            report = validate_scenario(str(root), "case")
            self.assertEqual("INVALID_JSON", report.input_errors[0].code)
            self.assertEqual(2, report.exit_code)

    def test_template_flag_does_not_change_temporal_result(self):
        unknown = [event("unknown", "transport", "train_1", None, None)]
        false_data = make_dataset(unknown)
        false_report = self.validate(false_data)
        self.assertEqual(3, false_report.exit_code)

        true_data = make_dataset(
            [event("known", "transport", "train_1", "2026-10-24T09:00:00+03:00", "2026-10-24T10:00:00+03:00")]
        )
        for document in true_data.values():
            document["is_template"] = True
        true_report = self.validate(true_data)
        self.assertEqual(0, true_report.exit_code)

    def test_version_snapshot_and_cross_catalog_reference_rules(self):
        unsupported = make_dataset()
        unsupported["food"]["schema_version"] = "9.9.9"
        unsupported_report = self.validate(unsupported)
        self.assertEqual(2, unsupported_report.exit_code)
        self.assertEqual("UNSUPPORTED_SCHEMA_VERSION", unsupported_report.input_errors[0].code)

        mismatched = make_dataset()
        mismatched["food"]["snapshot_id"] = "another_snapshot"
        mismatch_report = self.validate(mismatched)
        self.assertIn(
            "SNAPSHOT_ID_MISMATCH",
            {item.code for item in check(mismatch_report, "snapshot_consistency").violations},
        )

        bad_links = make_dataset()
        bad_links["scenario"]["constraints"]["required_place_ids"] = ["missing_place"]
        bad_links["transfers"]["items"][0]["to_id"] = "missing_point"
        bad_links_report = self.validate(bad_links)
        self.assertIn(
            "INVALID_REQUIRED_PLACE_REFERENCE",
            {item.code for item in check(bad_links_report, "required_place_references").violations},
        )
        self.assertIn(
            "INVALID_TRANSFER_ENDPOINT",
            {item.code for item in check(bad_links_report, "transfer_endpoint_references").violations},
        )

        collision = make_dataset()
        collision["places"]["items"][0]["id"] = "station_a"
        collision["scenario"]["constraints"]["required_place_ids"] = ["station_a"]
        collision["plan"]["items"][2]["reference_id"] = "station_a"
        collision_report = self.validate(collision)
        self.assertIn(
            "STATION_OBJECT_ID_COLLISION",
            {item.code for item in check(collision_report, "station_consistency").violations},
        )

    def test_cli_human_json_and_exit_codes(self):
        with working_directory() as temp:
            root = write_dataset(temp, make_dataset())
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "check_scenario.py"),
                "--scenario",
                "case",
                "--data-dir",
                str(root),
            ]
            human = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(0, human.returncode, human.stderr)
            self.assertIn("Проверки первого дня", human.stdout)
            machine = subprocess.run(
                command + ["--json"], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8"
            )
            payload = json.loads(machine.stdout)
            self.assertEqual(0, machine.returncode, machine.stderr)
            self.assertEqual("day1", payload["scope"])
            self.assertFalse(payload["full_trip_feasibility_evaluated"])


if __name__ == "__main__":
    unittest.main()
