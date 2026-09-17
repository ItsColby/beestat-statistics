"""Coverage contracts for filter exposure and complete-day runtime rates."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
SPEC = importlib.util.spec_from_file_location(
    "filter_runtime_unit", ROOT / "filter_runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


UTC_ZONE = ZoneInfo("UTC")


def points(day: date, *, zone: ZoneInfo = UTC_ZONE, fan: float = 60) -> list[dict]:
    start, end = runtime.local_day_bounds(day, zone)
    return [
        {"timestamp": (start + timedelta(minutes=5 * i)).isoformat(), "fan": fan}
        for i in range(int((end - start).total_seconds()) // 300)
    ]


class FilterRuntimeTest(unittest.TestCase):
    def test_uncertainty_deadline_preserves_normalized_horizon_and_tail(self) -> None:
        before = datetime(2026, 7, 1, 13, tzinfo=UTC)
        for source_end, changed, evaluated, expected in (
            (
                before - timedelta(seconds=300),
                before - timedelta(hours=1),
                before,
                before + timedelta(seconds=100),
            ),
            (
                before - timedelta(seconds=283),
                before - timedelta(hours=1),
                before,
                before + timedelta(seconds=100),
            ),
            (
                before - timedelta(seconds=300),
                before - timedelta(hours=1),
                before + timedelta(seconds=30),
                before + timedelta(seconds=100),
            ),
            (
                before - timedelta(seconds=600),
                before - timedelta(seconds=50),
                before,
                before + timedelta(seconds=50),
            ),
        ):
            with self.subTest(
                source_end=source_end, changed=changed, evaluated=evaluated
            ):
                observation = runtime.build_filter_runtime_observation(
                    [],
                    changed_date=changed.date(),
                    changed_at=changed,
                    change_day=runtime.ChangeDayObservation(
                        3500, 0, 0, "finalized", 0, source_end
                    ),
                    source_data_end=source_end,
                    evaluated_at=evaluated,
                    local_tz=UTC_ZONE,
                )
                self.assertFalse(observation.threshold_reached(1))
                self.assertEqual(
                    runtime.next_filter_uncertainty_deadline(
                        observation, lifetime_hours=1, evaluated_at=evaluated
                    ),
                    expected,
                )

    def test_uncertainty_deadline_omits_unknown_due_and_missing_source(self) -> None:
        before = datetime(2026, 7, 1, 13, tzinfo=UTC)
        observation = runtime.FilterRuntimeObservation(
            3500, "complete", 0, 0, "finalized", before - timedelta(seconds=300)
        )
        for candidate in (
            replace(observation, observed_seconds=None),
            replace(observation, unknown_interval_seconds=None),
            replace(observation, unknown_interval_seconds=100),
            replace(observation, observed_seconds=3600),
            replace(observation, source_data_end=None),
        ):
            with self.subTest(observation=candidate):
                self.assertIsNone(
                    runtime.next_filter_uncertainty_deadline(
                        candidate, lifetime_hours=1, evaluated_at=before
                    )
                )

    def test_uncertainty_deadline_rounds_positive_submicrosecond_margin_up(
        self,
    ) -> None:
        before = datetime(2026, 7, 1, 13, tzinfo=UTC)
        observation = runtime.FilterRuntimeObservation(
            3599.9999999, "complete", 0, 0, "finalized", before
        )
        self.assertEqual(
            runtime.next_filter_uncertainty_deadline(
                observation, lifetime_hours=1, evaluated_at=before
            ),
            before + timedelta(microseconds=1),
        )

    def test_fractional_boundary_keeps_complete_source_and_bounded_uncertainty(
        self,
    ) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, 1, 2, tzinfo=UTC)
        end = datetime(2026, 7, 6, tzinfo=UTC)
        raw = runtime.assess_change_day(
            points(day),
            changed,
            local_tz=ZoneInfo("UTC"),
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
        )
        self.assertEqual(raw.observed_seconds, 275 * 60)
        self.assertEqual(raw.gap_seconds, 0)
        self.assertEqual(raw.boundary_uncertainty_seconds, 180)
        self.assertEqual(raw.boundary_status, "finalized")
        observed = runtime.build_filter_runtime_observation(
            [],
            changed_date=day,
            changed_at=changed,
            change_day=raw,
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
            local_tz=ZoneInfo("UTC"),
        )
        self.assertEqual(observed.coverage, "complete")
        self.assertTrue(observed.is_lower_bound)
        self.assertEqual(observed.unknown_interval_seconds, 180)

    def test_gap_after_boundary_and_missing_whole_day_remain_unknown_exposure(
        self,
    ) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, tzinfo=UTC)
        end = datetime(2026, 7, 8, tzinfo=UTC)
        raw_rows = points(day, fan=0)
        del raw_rows[10]
        raw = runtime.assess_change_day(
            raw_rows,
            changed,
            local_tz=ZoneInfo("UTC"),
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
        )
        observed = runtime.build_filter_runtime_observation(
            [{"date": "2026-07-07", "count": 144, "sum_fan": 3600}],
            changed_date=day,
            changed_at=changed,
            change_day=raw,
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
            local_tz=ZoneInfo("UTC"),
        )
        self.assertEqual(observed.observed_seconds, 3600)
        self.assertEqual(observed.coverage, "partial")
        self.assertEqual(observed.unknown_interval_seconds, 300 + 86400 + 43200)
        self.assertTrue(observed.threshold_reached(1))
        self.assertIsNone(observed.threshold_reached(2))

    def test_invalid_duplicate_and_deleted_point_is_missing_not_zero(self) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, tzinfo=UTC)
        end = changed + timedelta(days=1)
        rows = points(day)
        rows.extend(
            [
                {"timestamp": rows[0]["timestamp"], "fan": float("inf")},
                {"timestamp": rows[1]["timestamp"], "fan": 60, "deleted": True},
                {"timestamp": rows[2]["timestamp"], "fan": 60, "deleted": "false"},
                {"timestamp": "bad", "fan": 300},
                {"timestamp": "2026-07-05T01:01:00+00:00", "fan": 300},
            ]
        )
        raw = runtime.assess_change_day(
            rows,
            changed,
            local_tz=ZoneInfo("UTC"),
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
        )
        self.assertEqual(raw.gap_seconds, 600)
        self.assertEqual(raw.observed_seconds, 286 * 60)
        self.assertEqual(raw.boundary_status, "source_gap")
        self.assertIsNone(raw.baseline_seconds)

    def test_unknown_count_cannot_prove_not_due_but_observed_threshold_can(
        self,
    ) -> None:
        observed = runtime.build_filter_runtime_observation(
            [{"date": "2026-07-06", "sum_fan": 3600}],
            changed_date=date(2026, 7, 5),
            changed_at=None,
            change_day=None,
            source_data_end=datetime(2026, 7, 6, 23, 55, tzinfo=UTC),
            evaluated_at=datetime(2026, 7, 7, tzinfo=UTC),
            local_tz=ZoneInfo("UTC"),
        )
        self.assertEqual(observed.coverage, "unknown")
        self.assertIsNone(observed.unknown_interval_seconds)
        self.assertIsNone(observed.threshold_reached(2))
        self.assertTrue(observed.threshold_reached(1))

    def test_stale_source_tail_bounds_not_due_proof(self) -> None:
        observation = runtime.FilterRuntimeObservation(
            3500, "complete", 200, 0, "finalized", None
        )
        self.assertTrue(observation.is_lower_bound)
        self.assertIsNone(observation.threshold_reached(1))
        self.assertFalse(observation.threshold_reached(2))

    def test_source_uncertainty_excludes_elapsed_tail_but_retains_gap_corrections(
        self,
    ) -> None:
        changed = datetime(2026, 7, 5, tzinfo=UTC)
        end = changed + timedelta(days=1)
        rows = points(changed.date(), fan=0)[1:]

        def observe(at: datetime):
            raw = runtime.assess_change_day(
                rows,
                changed,
                local_tz=UTC_ZONE,
                source_data_end=end - timedelta(minutes=5),
                evaluated_at=at,
            )
            return runtime.build_filter_runtime_observation(
                [],
                changed_date=changed.date(),
                changed_at=changed,
                change_day=raw,
                source_data_end=end - timedelta(minutes=5),
                evaluated_at=at,
                local_tz=UTC_ZONE,
            )

        first = observe(end + timedelta(seconds=1))
        later = observe(end + timedelta(minutes=5, seconds=1))
        self.assertEqual(first.source_unknown_interval_seconds, 300)
        self.assertEqual(later.source_unknown_interval_seconds, 300)
        self.assertEqual(first.unknown_interval_seconds, 301)
        self.assertEqual(later.unknown_interval_seconds, 601)
        self.assertEqual(later.observed_seconds, first.observed_seconds)
        self.assertEqual(later.coverage, first.coverage)
        rows.pop()
        corrected = observe(end + timedelta(minutes=5, seconds=1))
        self.assertEqual(corrected.source_unknown_interval_seconds, 600)
        self.assertEqual(corrected.unknown_interval_seconds, 901)
        self.assertEqual(corrected.coverage, later.coverage)
        self.assertEqual(corrected.observed_seconds, later.observed_seconds)

    def test_recent_rate_excludes_current_missing_and_incomplete_days(self) -> None:
        rate = runtime.build_recent_runtime_rate(
            [
                {"date": "2026-07-03", "count": 144, "sum_fan": 18000},
                {"date": "2026-07-04", "count": 288, "sum_fan": 3600},
                {"date": "2026-07-05", "count": 288, "sum_fan": 7200},
                {"date": "2026-07-06", "count": 144, "sum_fan": 10000},
            ],
            today=date(2026, 7, 6),
            local_tz=ZoneInfo("UTC"),
            window_days=4,
        )
        self.assertEqual(rate.hours_per_day, 1.5)
        self.assertEqual(rate.complete_days, 2)
        self.assertEqual(rate.excluded_days, 2)
        self.assertEqual(rate.window_end, date(2026, 7, 5))

    def test_dst_day_completeness_uses_elapsed_utc_slots(self) -> None:
        zone = ZoneInfo("America/New_York")
        for day, count in ((date(2026, 3, 8), 276), (date(2026, 11, 1), 300)):
            with self.subTest(day=day):
                rate = runtime.build_recent_runtime_rate(
                    [{"date": day.isoformat(), "count": count, "sum_fan": 3600}],
                    today=day + timedelta(days=1),
                    local_tz=zone,
                    window_days=1,
                )
                self.assertEqual(rate.complete_days, 1)
                self.assertEqual(rate.hours_per_day, 1)

    def test_later_source_correction_recomputes_baseline_without_changing_event(
        self,
    ) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, 12, tzinfo=UTC)
        end = changed.replace(hour=23, minute=55)
        rows = points(day)
        first = runtime.assess_change_day(
            rows,
            changed,
            local_tz=ZoneInfo("UTC"),
            source_data_end=end,
            evaluated_at=end + timedelta(minutes=5),
        )
        rows[0] = {**rows[0], "fan": 120}
        second = runtime.assess_change_day(
            rows,
            changed,
            local_tz=ZoneInfo("UTC"),
            source_data_end=end,
            evaluated_at=end + timedelta(minutes=5),
        )
        self.assertEqual(second.baseline_seconds, first.baseline_seconds + 60)
        self.assertEqual(second.observed_seconds, first.observed_seconds)

    def test_threshold_date_uses_same_validated_observations_and_source_horizon(
        self,
    ) -> None:
        rows = [
            {"date": "2026-07-06", "count": 288, "sum_fan": 3600},
            {"date": "2026-07-07", "count": 288, "sum_fan": 3600},
            {"date": "2026-07-07", "count": 1, "sum_fan": 7200},
            {"date": "2026-07-08", "count": 288, "sum_fan": 3600},
            {"date": "2026-07-09", "count": 288, "sum_fan": 7200},
        ]
        args = {
            "changed_date": date(2026, 7, 5),
            "change_day": None,
            "lifetime_hours": 2,
            "local_tz": UTC_ZONE,
            "source_data_end": datetime(2026, 7, 8, 23, 55, tzinfo=UTC),
            "evaluated_at": datetime(2026, 7, 9, tzinfo=UTC),
        }
        self.assertEqual(
            runtime.observed_threshold_date(rows, **args), date(2026, 7, 8)
        )
        self.assertIsNone(
            runtime.observed_threshold_date(rows, **{**args, "lifetime_hours": 3})
        )

    def test_missing_source_horizon_never_proves_not_due(self) -> None:
        observed = runtime.build_filter_runtime_observation(
            [{"date": "2026-07-06", "count": 288, "sum_fan": 3600}],
            changed_date=date(2026, 7, 5),
            changed_at=None,
            change_day=None,
            source_data_end=None,
            evaluated_at=datetime(2026, 7, 7, tzinfo=UTC),
            local_tz=UTC_ZONE,
        )
        self.assertEqual(observed.observed_hours, 1)
        self.assertEqual(observed.coverage, "unknown")
        self.assertIsNone(observed.threshold_reached(2))

    def test_partial_clock_bucket_is_bounded_unknown_not_zero(self) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, tzinfo=UTC)
        evaluated = datetime(2026, 7, 5, 1, 2, tzinfo=UTC)
        raw = runtime.assess_change_day(
            points(day),
            changed,
            local_tz=UTC_ZONE,
            source_data_end=evaluated,
            evaluated_at=evaluated,
        )
        observed = runtime.build_filter_runtime_observation(
            [],
            changed_date=day,
            changed_at=changed,
            change_day=raw,
            source_data_end=evaluated,
            evaluated_at=evaluated,
            local_tz=UTC_ZONE,
        )
        self.assertEqual(observed.observed_seconds, 12 * 60)
        self.assertEqual(observed.unknown_interval_seconds, 120)
        self.assertEqual(observed.coverage, "complete")

    def test_rounding_cannot_create_threshold_proof_or_exact_remaining(self) -> None:
        observation = runtime.FilterRuntimeObservation(
            3599, "complete", 0, 0, "finalized", None
        )
        self.assertEqual(observation.observed_hours, 0.9)
        self.assertFalse(observation.threshold_reached(1))
        self.assertTrue(observation.is_lower_bound)
        self.assertTrue(
            runtime.FilterRuntimeObservation(
                3600, "partial", None, 0, "source_gap", None
            ).threshold_reached(1)
        )

    def test_prechange_gap_does_not_erase_known_postchange_coverage(self) -> None:
        day = date(2026, 7, 5)
        changed = datetime(2026, 7, 5, 12, tzinfo=UTC)
        end = datetime(2026, 7, 6, tzinfo=UTC)
        raw = runtime.assess_change_day(
            points(day)[1:],
            changed,
            local_tz=UTC_ZONE,
            source_data_end=end - timedelta(minutes=5),
            evaluated_at=end,
        )
        self.assertEqual(raw.boundary_status, "source_gap")
        self.assertIsNone(raw.baseline_seconds)
        self.assertEqual(raw.gap_seconds, 0)
        self.assertEqual(raw.observed_seconds, 144 * 60)
