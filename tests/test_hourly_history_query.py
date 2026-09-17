"""Logical history proof, calendar and revision behavior without Home Assistant."""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_history_query_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
module_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_history_query", ROOT / "hourly_history_query.py"
)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError("Unable to load hourly history query")
query_module = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = query_module
module_spec.loader.exec_module(query_module)
history_response = query_module.history_response
METHOD = "five_minute_complete_hour_v3"
POLICY = "complete_points_else_legacy_day"
HOUR = timedelta(hours=1)
START = datetime(2026, 9, 10, 4, tzinfo=UTC)
QUANTITY = "thermostat:1:heat_runtime"


class HourlyHistoryQueryTest(unittest.TestCase):
    """Use synthetic, explicit source proof rather than inferred wall-clock coverage."""

    def setUp(self):
        self.context = {
            "identity": {
                "entry_id": "fixture",
                "api_base": "https://api.beestat.io/",
                "account_anchors": ["account-1"],
            },
            "config_revision": "config-1",
            "timezone": "America/New_York",
            "timezone_revision": "tz-1",
            "evaluated_at": (START + 48 * HOUR).isoformat(),
        }
        self.material = {
            "descriptors": [],
            "hours": {},
            "native_rows": {},
            "native_metadata": {},
            "expected_metadata": {},
            "legacy_days": {},
            "pending_affected": {},
            "root_revision": 1,
            "source_revision": "source-1",
            "coverage_revision": 1,
        }
        self.add_quantity()

    def add_quantity(self, quantity=QUANTITY, kind="runtime"):
        statistic = f"beestat:fixture_{len(self.material['descriptors'])}_hourly_v3"
        descriptor = {
            "quantity_id": quantity,
            "thermostat_id": 1,
            "sensor_id": None,
            "quantity": quantity.rsplit(":", 1)[1],
            "kind": kind,
            "logical_unit": {
                "runtime": "h",
                "degree_days": "°F·d",
                "measurement": "°F",
            }[kind],
            "method_version": METHOD,
            "statistic_id": statistic,
            "legacy_statistic_ids": [f"{statistic}_legacy"],
            "representation": {"kind": "arithmetic_mean"},
            "admission": "eligible",
            "writer_status": "adopted",
        }
        metadata = {
            "statistic_id": statistic,
            "source": "beestat",
            "unit_of_measurement": "%" if kind == "runtime" else "°F",
            "unit_class": {
                "runtime": "unitless",
                "degree_days": "temperature_delta",
                "measurement": "temperature",
            }[kind],
            "mean_type": 1,
            "has_sum": False,
        }
        self.material["descriptors"].append(descriptor)
        self.material["expected_metadata"][quantity] = metadata
        self.material["native_metadata"][quantity] = dict(metadata)
        for key in ("hours", "native_rows", "legacy_days", "pending_affected"):
            self.material[key][quantity] = []
        return quantity

    def point(
        self,
        start=START,
        mean=125.0,
        *,
        quantity=QUANTITY,
        low=None,
        high=None,
        **changes,
    ):
        proof = {
            "start": start.isoformat(),
            "status": "ready",
            "committed": True,
            "valid_slots": 12,
            "expected_slots": 12,
            "mean": mean,
            "min": low,
            "max": high,
            "source_ids": ["chunk-1"],
            "confidence": ["ordered_provider"],
            **changes,
        }
        self.material["hours"][quantity].append(proof)
        self.material["native_rows"][quantity].append(
            {
                "start": start.isoformat(),
                "mean": mean,
                "min": low,
                "max": high,
                "state": None,
                "sum": None,
            }
        )
        return proof

    def legacy(self, *, quantity=QUANTITY, start=START, end=None, **changes):
        end = end or start + 24 * HOUR
        evidence = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": self.context["timezone"],
            "timezone_revision": "tz-1",
            "calendar_verified": True,
            "complete": True,
            "closed": True,
            "adoption_day": False,
            "native_verified": True,
            "source_ids": ["legacy-1"],
            "confidence": ["legacy_summary_precision"],
            "method": "legacy_daily_cumulative",
            "predecessor_valid": True,
            "previous_start": (start - 24 * HOUR).isoformat(),
            "previous_sum": 10,
            "sum": 12.5,
            **changes,
        }
        self.material["legacy_days"][quantity].append(evidence)
        return evidence

    def request(self, start=START, end=None, **changes):
        return {
            "contract_version": 3,
            "quantity_ids": [QUANTITY],
            "start": start.isoformat(),
            "end": (end or start + HOUR).isoformat(),
            "period": "hour",
            "daily_policy": POLICY,
            **changes,
        }

    def result(self, request=None):
        return history_response(request or self.request(), self.material, self.context)

    def bucket(self, request=None):
        return self.result(request)["series"][0]["buckets"][0]

    def test_first_complete_hour_is_amount_without_predecessor_or_heat_clamp(self):
        self.point(mean=175)
        result = self.result()
        series = result["series"][0]
        self.assertEqual(series["buckets"][0]["value"], 1.75)
        self.assertEqual(
            series["buckets"][0]["eligible_intervals"],
            [[START.isoformat(), (START + HOUR).isoformat()]],
        )
        self.assertEqual(series["summary"]["complete_total"], 1.75)
        self.assertIsNone(series["buckets"][0]["min"])
        self.assertIsNone(series["buckets"][0]["max"])

    def test_degree_day_amount_uses_fixed_twenty_four_hour_denominator(self):
        identifier = self.add_quantity(
            "thermostat:1:heating_degree_days", "degree_days"
        )
        self.point(mean=12, quantity=identifier)
        bucket = self.bucket(self.request(quantity_ids=[identifier]))
        self.assertEqual(bucket["value"], 0.5)

    def test_measurement_has_genuine_extrema_and_hour_weighted_summary(self):
        identifier = self.add_quantity("sensor:2:temperature", "measurement")
        self.point(mean=70, low=68, high=72, quantity=identifier)
        self.point(START + HOUR, mean=74, low=73, high=76, quantity=identifier)
        series = self.result(
            self.request(end=START + 2 * HOUR, quantity_ids=[identifier])
        )["series"][0]
        self.assertEqual(series["summary"]["observed_mean"], 72)
        self.assertEqual(series["summary"]["observed_min"], 68)
        self.assertEqual(series["summary"]["observed_max"], 76)
        self.assertEqual(series["summary"]["summary_basis"], "verified_point_hours")

    def test_missing_proof_native_and_committed_states_stay_distinct(self):
        self.assertEqual(self.bucket()["reason"], "unassessed")
        self.point(committed=False)
        self.assertEqual(self.bucket()["reason"], "unverified_native")
        self.material["hours"][QUANTITY].clear()
        self.assertEqual(self.bucket()["reason"], "unverified_native")
        self.material["native_rows"][QUANTITY].clear()
        self.material["hours"][QUANTITY].append(
            {"start": START.isoformat(), "status": "missing_slots", "valid_slots": 0}
        )
        self.assertEqual(self.bucket()["reason"], "missing")
        self.assertIsNone(self.bucket()["value"])

    def test_metadata_or_row_change_invalidates_previously_committed_hour(self):
        self.point()
        self.material["native_metadata"][QUANTITY]["unit_class"] = "temperature"
        self.assertEqual(self.bucket()["reason"], "unverified_native")
        self.material["native_metadata"][QUANTITY] = dict(
            self.material["expected_metadata"][QUANTITY]
        )
        self.material["native_rows"][QUANTITY][0]["mean"] = 150
        self.assertEqual(self.bucket()["reason"], "unverified_native")
        self.assertIsNone(self.bucket()["value"])

    def test_blocked_descriptors_and_unrecognized_proof_codes_are_sanitized(self):
        descriptor = self.material["descriptors"][0]
        descriptor.update(admission="blocked", blocked_reason="voc_unit_unresolved")
        self.assertEqual(self.bucket()["reason"], "blocked")
        self.assertEqual(self.bucket()["failure_reason"], "voc_unit_unresolved")
        descriptor.update(admission="eligible")
        self.point(status="unexpected provider payload")
        self.assertEqual(self.bucket()["reason"], "blocked")
        self.assertEqual(self.bucket()["failure_reason"], "source_evidence_invalid")
        self.material["hours"][QUANTITY][0]["status"] = "source_conflict"
        self.assertEqual(self.bucket()["reason"], "conflict")
        self.assertEqual(self.bucket()["failure_reason"], "source_conflict")

    def test_gap_is_not_zero_and_observed_zero_is_not_missing(self):
        self.point(mean=0)
        self.point(START + HOUR, mean=None, status="missing_slots", valid_slots=0)
        series = self.result(self.request(end=START + 2 * HOUR))["series"][0]
        self.assertEqual([item["value"] for item in series["buckets"]], [0, None])
        self.assertEqual(series["summary"]["observed_amount"], 0)
        self.assertIsNone(series["summary"]["complete_total"])
        self.assertEqual(series["summary"]["verified_hours"], 1)
        self.assertEqual(series["summary"]["expected_hours"], 2)

    def test_blocked_voc_day_preserves_reason_and_qualification_with_pending_priority(
        self,
    ):
        self.material["descriptors"][0].update(
            quantity="voc_concentration",
            admission="blocked",
            blocked_reason="voc_unit_unresolved",
        )
        request = self.request(end=START + 24 * HOUR, period="day")
        bucket = self.bucket(request)
        self.assertEqual(bucket["reason"], "blocked")
        self.assertEqual(bucket["failure_reason"], "voc_unit_unresolved")
        self.assertIsNone(bucket["value"])
        self.material["pending_affected"][QUANTITY] = [
            [START.isoformat(), (START + HOUR).isoformat()]
        ]
        bucket = self.bucket(request)
        self.assertEqual(bucket["reason"], "pending")
        self.assertEqual(bucket["failure_reason"], "voc_unit_unresolved")

    def test_current_bucket_remains_provisional_and_elapsed_time_does_not_commit(self):
        self.context["evaluated_at"] = (START + timedelta(minutes=30)).isoformat()
        self.point()
        self.assertEqual(self.bucket()["reason"], "provisional")
        self.context["evaluated_at"] = (START + 2 * HOUR).isoformat()
        self.material["hours"][QUANTITY][0]["committed"] = False
        self.assertEqual(self.bucket()["reason"], "unverified_native")

    def test_pending_suppresses_only_affected_hours_and_prevents_daily_legacy_fallback(
        self,
    ):
        self.point()
        self.point(START + HOUR)
        self.material["pending_affected"][QUANTITY] = [
            [START.isoformat(), (START + HOUR).isoformat()]
        ]
        response = self.result(self.request(end=START + 2 * HOUR))
        self.assertEqual(
            [item["value"] for item in response["series"][0]["buckets"]], [None, 1.25]
        )
        self.legacy()
        bucket = self.bucket(self.request(end=START + 24 * HOUR, period="day"))
        self.assertEqual(bucket["value"], 1.25)
        self.assertEqual(bucket["source_basis"], "points")
        self.assertEqual(bucket["coverage_reasons"]["pending"], 1)
        self.assertEqual(bucket["eligible_intervals"], [])

    def test_daily_complete_points_win_over_legacy_and_component_hours_exceed_day(self):
        for index in range(24):
            self.point(START + index * HOUR, mean=125)
        self.legacy()
        bucket = self.bucket(self.request(end=START + 24 * HOUR, period="day"))
        self.assertEqual(bucket["value"], 30)
        self.assertEqual(bucket["complete_total"], 30)
        self.assertEqual(bucket["source_basis"], "points")
        self.assertEqual(len(bucket["eligible_intervals"]), 24)

    def test_immutable_source_hold_suppresses_old_proof_and_legacy_fallback(self):
        self.point()
        self.point(START + HOUR)
        self.legacy()
        self.material["source_holds"] = {
            QUANTITY: [
                {
                    "start": START.isoformat(),
                    "end": (START + HOUR).isoformat(),
                    "reason": "source_conflict",
                    "source_ids": ["challenging-source"],
                }
            ]
        }
        buckets = self.result(self.request(end=START + 2 * HOUR))["series"][0][
            "buckets"
        ]
        self.assertEqual(buckets[0]["reason"], "conflict")
        self.assertEqual(buckets[0]["failure_reason"], "source_conflict")
        self.assertIsNone(buckets[0]["value"])
        self.assertIn("challenging-source", buckets[0]["source_ids"])
        self.assertEqual(buckets[1]["value"], 1.25)
        day = self.bucket(self.request(end=START + 24 * HOUR, period="day"))
        self.assertEqual(day["value"], 1.25)
        self.assertEqual(day["source_basis"], "points")
        self.assertIsNone(day["complete_total"])

    def test_daily_fallback_selects_legacy_once_and_retains_partial_point_coverage(
        self,
    ):
        self.point()
        self.legacy()
        result = self.result(self.request(end=START + 24 * HOUR, period="day"))
        bucket = result["series"][0]["buckets"][0]
        self.assertEqual(bucket["value"], 2.5)
        self.assertEqual(bucket["point_observed_amount"], 1.25)
        self.assertEqual(bucket["observed_amount"], 2.5)
        self.assertEqual(bucket["verified_hours"], 1)
        self.assertEqual(bucket["source_basis"], "legacy_daily")
        self.assertEqual(bucket["confidence"], ["legacy_summary_precision"])
        self.assertEqual(len(bucket["eligible_intervals"]), 24)
        self.assertEqual(result["series"][0]["summary"]["complete_total"], 2.5)

    def test_unproven_legacy_day_does_not_fill_gaps(self):
        modifications = (
            {"predecessor_valid": False},
            {"complete": False},
            {"native_verified": False},
            {"adoption_day": True},
            {"adoption_day": None},
            {"previous_start": (START - 48 * HOUR).isoformat()},
            {"sum": 9},
            {"calendar_verified": False},
            {"timezone": "UTC"},
        )
        for changes in modifications:
            with self.subTest(changes=changes):
                self.material["legacy_days"][QUANTITY].clear()
                self.legacy(**changes)
                bucket = self.bucket(self.request(end=START + 24 * HOUR, period="day"))
                self.assertIsNone(bucket["value"])
                self.assertEqual(bucket["eligible_intervals"], [])
        self.assertEqual(bucket["legacy_reason"], "legacy_calendar_mismatch")

    def test_missing_legacy_adoption_evidence_is_not_treated_as_false(self):
        evidence = self.legacy()
        del evidence["adoption_day"]
        self.assertIsNone(
            self.bucket(self.request(end=START + 24 * HOUR, period="day"))["value"]
        )

    def test_partial_requested_calendar_day_cannot_be_complete_or_use_legacy_total(
        self,
    ):
        self.legacy()
        for index in range(6, 24):
            self.point(START + index * HOUR, mean=100)
        bucket = self.bucket(
            self.request(start=START + 6 * HOUR, end=START + 24 * HOUR, period="day")
        )
        self.assertEqual(bucket["value"], 18)
        self.assertEqual(bucket["calendar_hours"], 24)
        self.assertEqual(bucket["expected_hours"], 18)
        self.assertIsNone(bucket["complete_total"])
        self.assertEqual(bucket["eligible_intervals"], [])

    def test_local_dst_days_use_actual_twenty_three_and_twenty_five_hours(self):
        cases = (
            (datetime(2026, 3, 8, 5, tzinfo=UTC), 23),
            (datetime(2026, 11, 1, 4, tzinfo=UTC), 25),
        )
        for first, count in cases:
            with self.subTest(count=count):
                self.material["hours"][QUANTITY].clear()
                self.material["native_rows"][QUANTITY].clear()
                for index in range(count):
                    self.point(first + index * HOUR, mean=100)
                self.context["evaluated_at"] = (first + 48 * HOUR).isoformat()
                bucket = self.bucket(
                    self.request(start=first, end=first + count * HOUR, period="day")
                )
                self.assertEqual(bucket["value"], count)
                self.assertEqual(bucket["expected_hours"], count)
                self.assertEqual(bucket["closed_hours"], count)
                self.assertEqual(len(bucket["eligible_intervals"]), count)

    def test_calendar_boundary_that_splits_utc_hour_is_rejected(self):
        self.context["timezone"] = "Asia/Kolkata"
        with self.assertRaisesRegex(ValueError, "history_calendar_splits_utc_hour"):
            self.result(self.request(end=START + 24 * HOUR, period="day"))

    def test_legacy_measurement_fallback_is_sample_mean_with_distinct_method_basis(
        self,
    ):
        identifier = self.add_quantity("sensor:2:temperature", "measurement")
        self.legacy(
            quantity=identifier, method="legacy_sample_mean", mean=68, min=66, max=70
        )
        result = self.result(
            self.request(end=START + 24 * HOUR, period="day", quantity_ids=[identifier])
        )
        bucket = result["series"][0]["buckets"][0]
        self.assertEqual((bucket["value"], bucket["min"], bucket["max"]), (68, 66, 70))
        self.assertEqual(bucket["method_basis"], "legacy_sample_mean")
        self.assertIsNone(result["series"][0]["summary"]["observed_mean"])
        self.assertEqual(result["series"][0]["summary"]["legacy_days"], 1)

    def test_paging_keeps_evaluation_fixed_and_applies_limit_across_series(self):
        second = self.add_quantity("thermostat:1:fan_runtime")
        for identifier in (QUANTITY, second):
            self.point(quantity=identifier)
            self.point(START + HOUR, quantity=identifier)
        request = self.request(
            end=START + 2 * HOUR, quantity_ids=[QUANTITY, second], page_size=3
        )
        first = self.result(request)
        self.assertEqual(
            first["pagination"],
            {"offset": 0, "next_offset": 3, "has_more": True, "total_buckets": 4},
        )
        self.assertEqual([len(item["buckets"]) for item in first["series"]], [2, 1])
        self.context["evaluated_at"] = (START + 50 * HOUR).isoformat()
        next_page = self.result(
            {
                **request,
                "offset": 3,
                "page_size": 1,
                "view_token": first["view_token"],
                "evaluated_at": first["evaluated_at"],
            }
        )
        self.assertEqual(next_page["view_token"], first["view_token"])
        self.assertEqual(next_page["series"][0]["descriptor"]["quantity_id"], second)
        self.assertEqual(len(next_page["series"][0]["buckets"]), 1)
        self.assertFalse(next_page["pagination"]["has_more"])
        self.assertEqual(
            next_page["series"][0]["summary"], first["series"][1]["summary"]
        )
        self.assertEqual(next_page["series"][0]["summary"]["scope"], "query")
        self.assertEqual(next_page["series"][0]["summary"]["start"], request["start"])
        self.assertEqual(next_page["series"][0]["summary"]["end"], request["end"])

    def test_mixed_revision_or_native_change_rejects_later_page(self):
        self.point()
        self.point(START + HOUR)
        request = self.request(end=START + 2 * HOUR, page_size=1)
        first = self.result(request)
        following = {
            **request,
            "offset": 1,
            "view_token": first["view_token"],
            "evaluated_at": first["evaluated_at"],
        }
        for field in ("root_revision", "source_revision", "coverage_revision"):
            old = self.material[field]
            self.material[field] = "changed"
            with self.assertRaisesRegex(ValueError, "history_view_changed"):
                self.result(following)
            self.material[field] = old
        self.material["native_rows"][QUANTITY][0]["mean"] = 150
        with self.assertRaisesRegex(ValueError, "history_view_changed"):
            self.result(following)

    def test_pages_require_token_and_fixed_evaluation_and_reject_future_evaluation(
        self,
    ):
        for changes in ({"offset": 1}, {"view_token": "old"}):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    ValueError, "history_page_requires_view_and_evaluation"
                ),
            ):
                self.result(self.request(**changes))
        with self.assertRaisesRegex(ValueError, "history_evaluation_in_future"):
            self.result(self.request(evaluated_at=(START + 49 * HOUR).isoformat()))

    def test_request_limits_and_unique_explicit_quantities_are_enforced(self):
        for changes in (
            {"page_size": 4097},
            {"page_size": True},
            {"offset": -1},
            {"quantity_ids": []},
            {"quantity_ids": [QUANTITY, QUANTITY]},
            {"end": START + timedelta(days=367)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.result(self.request(**changes))

    def test_response_does_not_mutate_detached_sources_or_request(self):
        self.point()
        request = self.request()
        before = copy.deepcopy((request, self.material, self.context))
        self.result(request)
        self.assertEqual((request, self.material, self.context), before)

    def test_mean_only_legacy_measurement_does_not_invent_extrema(self):
        identifier = self.add_quantity("thermostat:1:indoor_humidity", "measurement")
        self.legacy(quantity=identifier, method="legacy_sample_mean", mean=45)
        bucket = self.bucket(
            self.request(end=START + 24 * HOUR, period="day", quantity_ids=[identifier])
        )
        self.assertEqual(bucket["value"], 45)
        self.assertIsNone(bucket["min"])
        self.assertIsNone(bucket["max"])

    def test_public_identity_excludes_internal_resource_map_and_operation_is_preserved(
        self,
    ):
        self.context["identity"]["resources"] = {"private": {"sensor_id": 123}}
        self.material["operation"] = {"status": "blocked", "root_revision": 1}
        response = self.result()
        self.assertEqual(
            set(response["identity"]), {"entry_id", "api_base", "account_anchors"}
        )
        self.assertEqual(response["operation"], self.material["operation"])


if __name__ == "__main__":
    unittest.main()
