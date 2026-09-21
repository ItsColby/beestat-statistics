"""Exercise the pure successor planner's continuity and recovery boundaries."""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from ._module_loader import load_module
else:
    from _module_loader import load_module

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_plan_test"


def _load_module(name: str):
    return load_module(ROOT, PACKAGE, name)


builder = _load_module("hourly_statistics")
planner = _load_module("hourly_import_plan")
HOUR = timedelta(hours=1)
START = datetime(2026, 9, 10, 4, tzinfo=UTC)


def _series(increments=(1.0, 2.0), *, cumulative=True):
    statistic_id = (
        "beestat:zone_fan_runtime_hours_hourly_v2"
        if cumulative
        else "beestat:room_temperature_hourly_v2"
    )
    metadata = {
        "statistic_id": statistic_id,
        "source": "beestat",
        "unit_of_measurement": "h" if cumulative else "°F",
        "unit_class": "duration" if cumulative else "temperature",
        "mean_type": 0 if cumulative else 1,
        "has_sum": cumulative,
        "name": "Hourly test statistic",
    }
    hours = tuple(
        builder.HourlyBucket(
            START + index * HOUR,
            None
            if value is None
            else (
                {"increment": value}
                if cumulative
                else {"mean": value, "min": value - 1, "max": value + 1}
            ),
            11 if value is None else 12,
            1 if value is None else 0,
            0,
            0,
            "missing_slots" if value is None else "ready",
        )
        for index, value in enumerate(increments)
    )
    return builder.HourlySeries(metadata, hours, len(hours) * 12)


def _snapshot(series, rows=(), *, complete=True):
    return planner.RecorderSnapshot(tuple(rows), dict(series.metadata), complete)


def _provisional(series, *indices):
    return replace(
        series,
        hours=tuple(
            replace(hour, values=None, reason="provisional")
            if index in indices
            else hour
            for index, hour in enumerate(series.hours)
        ),
    )


def _plan(
    series, *, snapshot=None, epoch=START, verified=START - HOUR, trusted_row=None
):
    return planner.plan_hourly_import(
        (series,),
        snapshots={} if snapshot is None else {series.statistic_id: snapshot},
        checkpoints={}
        if epoch is None
        else {
            series.statistic_id: planner.CumulativeCheckpoint(
                epoch,
                None if epoch == START else verified,
                trusted_row,
            )
        },
    )[0]


class HourlyImportPlanTests(unittest.TestCase):
    def test_segment_identity_is_deterministic_and_preserves_initial_and_legacy_ids(
        self,
    ):
        initial = _series().statistic_id
        segment = planner.segment_id(initial, START)
        self.assertEqual(f"{initial}_e20260910t040000z", segment)
        self.assertEqual(initial, planner.hourly_base_id(initial))
        self.assertEqual(initial, planner.hourly_base_id(segment))
        offset_epoch = START.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(segment, planner.segment_id(initial, offset_epoch))
        series = _series()
        series = replace(series, metadata={**series.metadata, "statistic_id": segment})
        result = _plan(series, snapshot=_snapshot(series))
        self.assertEqual([1, 3], [row.sum for row in result.unblocked_rows])
        self.assertEqual("beestat:zone_fan_runtime_hours", result.legacy_statistic_id)

    def test_malformed_successor_and_segment_ids_are_rejected(self):
        initial = _series().statistic_id
        for invalid in (
            "beestat:legacy",
            "other:zone_hourly_v2",
            "beestat:_hourly_v2",
            "beestat:Zone_hourly_v2",
            "beestat:zone__fan_hourly_v2",
            initial + "_e20260910t040000Z",
            initial + "_e20260910T040000z",
            initial + "_e20260910t043000z",
            initial + "_e20260910t040001z",
            initial + "_e20260230t040000z",
            initial + "_e20261310t040000z",
            initial + "_e20260910t240000z",
            initial + "_e20260910t040000z_extra",
            initial + "_e20260910t040000z_e20260911t040000z",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    planner.hourly_base_id(invalid)
                series = _series()
                series = replace(
                    series, metadata={**series.metadata, "statistic_id": invalid}
                )
                with self.assertRaises(ValueError):
                    _plan(series)

    def test_segment_naming_requires_initial_id_and_aware_whole_hour(self):
        initial = _series().statistic_id
        with self.assertRaises(ValueError):
            planner.segment_id(planner.segment_id(initial, START), START)
        for epoch in (
            START.replace(tzinfo=None),
            START + timedelta(minutes=1),
            START + timedelta(seconds=1),
            START + timedelta(microseconds=1),
        ):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                planner.segment_id(initial, epoch)

    def test_segment_epoch_must_match_checkpoint_before_initial_or_continued_import(
        self,
    ):
        series = _series()
        segment = planner.segment_id(series.statistic_id, START - 5 * HOUR)
        series = replace(series, metadata={**series.metadata, "statistic_id": segment})
        seed = planner.HourlyStatisticRow(START - HOUR, state=12, sum=30)
        result = _plan(series, snapshot=_snapshot(series))
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
        self.assertFalse(result.calculated_rows)
        for epoch in (START, START - 6 * HOUR):
            with self.subTest(epoch=epoch):
                result = _plan(series, snapshot=_snapshot(series, (seed,)), epoch=epoch)
                self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
                self.assertFalse(result.calculated_rows)
        result = _plan(
            series, snapshot=_snapshot(series, (seed,)), epoch=START - 5 * HOUR
        )
        self.assertEqual([31, 33], [row.sum for row in result.unblocked_rows])

    def test_saved_exact_predecessor_must_match_native_values(self):
        series = _series()
        trusted = planner.HourlyStatisticRow(START - HOUR, state=12, sum=30)
        result = _plan(
            series,
            snapshot=_snapshot(series, (trusted,)),
            epoch=START - 5 * HOUR,
            trusted_row=trusted,
        )
        self.assertEqual([31, 33], [row.sum for row in result.unblocked_rows])
        for actual in (
            replace(trusted, state=11),
            replace(trusted, sum=29),
            planner.HourlyStatisticRow(trusted.start),
        ):
            with self.subTest(actual=actual):
                result = _plan(
                    series,
                    snapshot=_snapshot(series, (actual,)),
                    epoch=START - 5 * HOUR,
                    trusted_row=trusted,
                )
                self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
                self.assertFalse(result.unblocked_rows)

    def test_saved_predecessor_does_not_supply_missing_native_row(self):
        series = _series()
        trusted = planner.HourlyStatisticRow(START - HOUR, state=12, sum=30)
        result = _plan(
            series,
            snapshot=_snapshot(series),
            epoch=START - 5 * HOUR,
            trusted_row=trusted,
        )
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_saved_predecessor_can_precede_last_verified_hour_for_wider_replay(self):
        series = _series()
        trusted = planner.HourlyStatisticRow(START - HOUR, state=12, sum=30)
        result = _plan(
            series,
            snapshot=_snapshot(series, (trusted,)),
            epoch=START - 5 * HOUR,
            verified=START + HOUR,
            trusted_row=trusted,
        )
        self.assertEqual([31, 33], [row.sum for row in result.unblocked_rows])

    def test_saved_predecessor_must_match_window_and_be_verified(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=12, sum=30)
        for trusted, verified in (
            (replace(seed, start=START - 2 * HOUR), START - HOUR),
            (seed, START - 2 * HOUR),
            (seed, None),
            (replace(seed, sum=None), START - HOUR),
        ):
            with self.subTest(trusted=trusted, verified=verified):
                result = _plan(
                    series,
                    snapshot=_snapshot(series, (seed,)),
                    epoch=START - 5 * HOUR,
                    verified=verified,
                    trusted_row=trusted,
                )
                self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
                self.assertFalse(result.unblocked_rows)

    def test_explicit_epoch_and_verified_empty_snapshot_start_new_counter(self):
        series = _series()
        result = _plan(series, snapshot=_snapshot(series))
        self.assertFalse(result.blocking_reasons)
        self.assertEqual([1, 3], [row.sum for row in result.unblocked_rows])
        self.assertEqual(
            [START, START + HOUR], [row.start for row in result.unblocked_rows]
        )
        self.assertEqual("beestat:zone_fan_runtime_hours", result.legacy_statistic_id)

    def test_zero_is_an_observed_increment(self):
        series = _series((0.0, 0.0))
        result = _plan(series, snapshot=_snapshot(series))
        self.assertEqual([0, 0], [row.sum for row in result.unblocked_rows])

    def test_missing_snapshot_does_not_prove_new_id(self):
        result = _plan(_series())
        self.assertIn("incomplete_recorder_snapshot", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_incomplete_and_mismatched_metadata_hold_proposals(self):
        series = _series()
        for snapshot in (
            _snapshot(series, complete=False),
            planner.RecorderSnapshot(
                (), {**series.metadata, "unit_of_measurement": "s"}, True
            ),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertTrue(_plan(series, snapshot=snapshot).blocking_reasons)

    def test_epoch_is_not_automatically_allocated(self):
        series = _series()
        result = _plan(series, snapshot=_snapshot(series), epoch=None)
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
        self.assertFalse(result.calculated_rows)

    def test_precise_predecessor_preserves_state_and_sum_offsets(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=12.0, sum=30.0)
        result = _plan(
            series, snapshot=_snapshot(series, (seed,)), epoch=START - 5 * HOUR
        )
        self.assertEqual([13, 15], [row.state for row in result.unblocked_rows])
        self.assertEqual([31, 33], [row.sum for row in result.unblocked_rows])

    def test_older_seed_cannot_bridge_missing_hour(self):
        series = _series()
        old = planner.HourlyStatisticRow(START - 2 * HOUR, state=12.0, sum=12.0)
        result = _plan(
            series, snapshot=_snapshot(series, (old,)), epoch=START - 5 * HOUR
        )
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_existing_predecessor_prevents_silent_new_epoch_reset(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=12.0, sum=12.0)
        result = _plan(series, snapshot=_snapshot(series, (seed,)))
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)

    def test_gap_holds_prefix_and_reports_entire_surviving_cumulative_suffix(self):
        series = _series((1.0, None, 2.0))
        old = tuple(
            planner.HourlyStatisticRow(
                START + index * HOUR, state=index + 1.0, sum=index + 1.0
            )
            for index in range(5)
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertEqual([1.0], [row.sum for row in result.calculated_rows])
        self.assertEqual(START + HOUR, result.continuity_break)
        self.assertEqual(tuple(row.start for row in old[1:]), result.stale_starts)
        self.assertIn("surviving_stale_rows", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)
        self.assertEqual("missing_slots", result.coverage[1].reason)

    def test_correction_must_recalculate_later_retained_totals(self):
        series = _series((2.0,))
        old = tuple(
            planner.HourlyStatisticRow(
                START + index * HOUR, state=index + 1.0, sum=index + 1.0
            )
            for index in range(3)
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertEqual((START + HOUR, START + 2 * HOUR), result.stale_starts)
        self.assertFalse(result.unblocked_rows)

    def test_trailing_provisional_hours_admit_prefix_without_numeric_tail(self):
        series = _provisional(_series((1.0, None, None)), 1, 2)
        for retained in ((), (planner.HourlyStatisticRow(START + HOUR),)):
            with self.subTest(retained=retained):
                result = _plan(series, snapshot=_snapshot(series, retained))
                self.assertFalse(result.blocking_reasons)
                self.assertEqual([1.0], [row.sum for row in result.unblocked_rows])
                self.assertEqual(START + HOUR, result.continuity_break)
                self.assertEqual(
                    ["ready", "provisional", "provisional"],
                    [hour.reason for hour in result.coverage],
                )

    def test_provisional_tail_keeps_entire_native_suffix_stale(self):
        old = tuple(
            planner.HourlyStatisticRow(
                START + index * HOUR, state=index + 1.0, sum=index + 1.0
            )
            for index in range(4)
        )
        for prefix in (1.0, 2.0):
            with self.subTest(prefix=prefix):
                series = _provisional(_series((prefix, None)), 1)
                result = _plan(series, snapshot=_snapshot(series, old))
                self.assertEqual(("surviving_stale_rows",), result.blocking_reasons)
                self.assertEqual(
                    tuple(row.start for row in old[1:]), result.stale_starts
                )
                self.assertFalse(result.unblocked_rows)

    def test_provisional_exemption_cannot_skip_an_internal_gap(self):
        for series in (
            _provisional(_series((1.0, None, 2.0)), 1),
            _provisional(_series((1.0, None, None)), 2),
            _provisional(_series((1.0, None, None)), 1),
        ):
            with self.subTest(hours=series.hours):
                result = _plan(series, snapshot=_snapshot(series))
                self.assertIn("cumulative_source_gap", result.blocking_reasons)
                self.assertEqual(START + HOUR, result.continuity_break)
                self.assertFalse(result.unblocked_rows)

    def test_all_provisional_hours_do_not_invent_rows_or_bypass_basis(self):
        series = _provisional(_series((None, None)), 0, 1)
        result = _plan(series, snapshot=_snapshot(series))
        self.assertFalse(result.blocking_reasons)
        self.assertFalse(result.calculated_rows)
        self.assertEqual(START, result.continuity_break)
        unproven = _plan(series, snapshot=_snapshot(series), epoch=START - HOUR)
        self.assertIn("unproven_cumulative_basis", unproven.blocking_reasons)
        self.assertFalse(unproven.unblocked_rows)

    def test_inserting_a_previously_missing_hour_also_requires_future_suffix(self):
        series = _series((1.0,))
        future = planner.HourlyStatisticRow(START + HOUR, state=2.0, sum=2.0)
        result = _plan(series, snapshot=_snapshot(series, (future,)))
        self.assertEqual((future.start,), result.stale_starts)

    def test_complete_suffix_correction_is_calculated_without_adjustment_jump(self):
        series = _series((2.0, 1.0, 1.0))
        old = tuple(
            planner.HourlyStatisticRow(
                START + index * HOUR, state=index + 1.0, sum=index + 1.0
            )
            for index in range(3)
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertEqual([2, 3, 4], [row.sum for row in result.unblocked_rows])
        self.assertFalse(result.stale_starts)

    def test_unchanged_replay_does_not_require_rewriting_unaffected_future(self):
        series = _series((1.0,))
        old = tuple(
            planner.HourlyStatisticRow(
                START + index * HOUR, state=index + 1.0, sum=index + 1.0
            )
            for index in range(3)
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertFalse(result.blocking_reasons)
        self.assertEqual((old[0],), result.unblocked_rows)

    def test_deleted_measurement_exposes_surviving_row_but_not_unrelated_future(self):
        series = _series((70.0, None), cumulative=False)
        old = tuple(
            planner.HourlyStatisticRow(START + index * HOUR, mean=70, min=69, max=71)
            for index in range(3)
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertEqual((START + HOUR,), result.stale_starts)
        self.assertFalse(result.unblocked_rows)

    def test_missing_measurement_remains_visible_without_filling_the_gap(self):
        series = _series((70.0, None, 72.0), cumulative=False)
        result = _plan(series, snapshot=_snapshot(series))
        self.assertEqual(
            [START, START + 2 * HOUR], [row.start for row in result.unblocked_rows]
        )
        self.assertEqual(3, len(result.coverage))
        self.assertEqual(1, result.coverage[1].missing_slots)

    def test_invalid_or_duplicate_snapshot_rows_fail_closed(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=1.0, sum=1.0)
        for rows in (
            (seed, seed),
            (replace(seed, sum=float("inf")),),
            (replace(seed, sum=10**400),),
            (replace(seed, sum=None),),
        ):
            with self.subTest(rows=rows):
                result = _plan(
                    series, snapshot=_snapshot(series, rows), epoch=START - HOUR
                )
                self.assertIn("invalid_recorder_snapshot", result.blocking_reasons)
                self.assertFalse(result.unblocked_rows)

    def test_cleared_measurement_can_stay_missing_or_be_replaced_by_actual_data(self):
        series = _series((70.0, None), cumulative=False)
        cleared = planner.HourlyStatisticRow(START + HOUR)
        result = _plan(series, snapshot=_snapshot(series, (cleared,)))
        self.assertFalse(result.blocking_reasons)
        self.assertEqual([70], [row.mean for row in result.unblocked_rows])
        self.assertEqual("missing_slots", result.coverage[1].reason)
        restored = _series((70.0, 72.0), cumulative=False)
        result = _plan(restored, snapshot=_snapshot(restored, (cleared,)))
        self.assertEqual([70, 72], [row.mean for row in result.unblocked_rows])

    def test_cleared_cumulative_predecessor_is_valid_snapshot_but_never_a_seed(self):
        series = _series()
        cleared = planner.HourlyStatisticRow(START - HOUR)
        result = _plan(
            series, snapshot=_snapshot(series, (cleared,)), epoch=START - 5 * HOUR
        )
        self.assertNotIn("invalid_recorder_snapshot", result.blocking_reasons)
        self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_cleared_suffix_has_no_stale_numbers_but_does_not_restore_continuity(self):
        series = _series((1.0, None, 2.0))
        old = (
            planner.HourlyStatisticRow(START, state=1.0, sum=1.0),
            planner.HourlyStatisticRow(START + HOUR),
            planner.HourlyStatisticRow(START + 2 * HOUR),
        )
        result = _plan(series, snapshot=_snapshot(series, old))
        self.assertFalse(result.stale_starts)
        self.assertEqual(("cumulative_source_gap",), result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_accumulation_overflow_cannot_publish_a_nonfinite_total(self):
        series = _series((1e308, 1e308))
        result = _plan(series, snapshot=_snapshot(series))
        self.assertIn("cumulative_overflow", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_unplaced_source_rows_and_voc_hold_cannot_be_hidden(self):
        series = replace(_series((70.0,), cumulative=False), rejected_timestamps=1)
        result = _plan(series, snapshot=_snapshot(series))
        self.assertEqual(1, result.rejected_timestamps)
        self.assertIn("unplaced_source_rows", result.blocking_reasons)
        held = replace(series, blocked_reason="voc_unit_unresolved")
        self.assertFalse(_plan(held, snapshot=_snapshot(held)).calculated_rows)

    def test_duplicate_and_discontinuous_input_ids_or_hours_are_rejected(self):
        series = _series()
        with self.assertRaises(ValueError):
            planner.plan_hourly_import((series, series), snapshots={}, checkpoints={})
        discontinuous = replace(series, hours=(series.hours[1], series.hours[0]))
        with self.assertRaises(ValueError):
            _plan(discontinuous)

    def test_result_is_detached_from_mutable_source_values_and_metadata(self):
        series = _series((70.0,), cumulative=False)
        snapshot = _snapshot(series)
        result = _plan(series, snapshot=snapshot)
        series.hours[0].values["mean"] = 99.0
        series.metadata["statistic_id"] = "beestat:unrelated"
        self.assertEqual(70.0, result.unblocked_rows[0].mean)
        self.assertEqual("beestat:room_temperature_hourly_v2", result.statistic_id)

    def test_ready_label_cannot_override_incomplete_slot_evidence(self):
        series = _series((70.0,), cumulative=False)
        series = replace(series, hours=(replace(series.hours[0], valid_slots=11),))
        result = _plan(series, snapshot=_snapshot(series))
        self.assertIn("invalid_source_coverage", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)

    def test_native_predecessor_is_not_a_verified_continuity_checkpoint(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=12.0, sum=12.0)
        for verified in (None, START - 2 * HOUR):
            with self.subTest(verified=verified):
                result = _plan(
                    series,
                    snapshot=_snapshot(series, (seed,)),
                    epoch=START - timedelta(days=1000),
                    verified=verified,
                )
                self.assertIn("unproven_cumulative_basis", result.blocking_reasons)
                self.assertFalse(result.unblocked_rows)

    def test_expired_raw_prefix_does_not_change_epoch_or_block_a_trusted_seed(self):
        series = _series()
        seed = planner.HourlyStatisticRow(START - HOUR, state=12.0, sum=30.0)
        result = _plan(
            series,
            snapshot=_snapshot(series, (seed,)),
            epoch=START - timedelta(days=1000),
            verified=START - HOUR,
        )
        self.assertEqual([31, 33], [row.sum for row in result.unblocked_rows])

    def test_actual_builder_output_reconciles_and_deleted_slot_preserves_stale_evidence(
        self,
    ):
        config_model = sys.modules[f"{PACKAGE}.config_model"]
        config = config_model.BeestatConfig(
            thermostats=(config_model.ConfiguredThermostat(1, "zone", "Zone"),),
            sensors=(),
        )
        points = [
            {
                "thermostat_id": 1,
                "timestamp": (START + timedelta(minutes=5 * index)).isoformat(),
                "fan": 150,
            }
            for index in range(24)
        ]

        def produce():
            return next(
                item
                for item in builder.build_hourly_statistics(
                    {1: points},
                    {},
                    config,
                    start=START,
                    end=START + 2 * HOUR,
                    evaluated_at=START + 2 * HOUR,
                    source_end_by_thermostat={1: START + timedelta(minutes=115)},
                )
                if item.statistic_id == "beestat:zone_fan_runtime_hours_hourly_v2"
            )

        initial = produce()
        accepted = _plan(initial, snapshot=_snapshot(initial))
        self.assertEqual([0.5, 1.0], [row.sum for row in accepted.unblocked_rows])
        points.append({**points[12], "deleted": True})
        corrected = produce()
        result = _plan(
            corrected, snapshot=_snapshot(corrected, accepted.unblocked_rows)
        )
        self.assertEqual((START + HOUR,), result.stale_starts)
        self.assertEqual(1, result.coverage[1].invalid_slots)
        self.assertEqual(1, result.coverage[1].duplicate_slots)
        self.assertFalse(result.unblocked_rows)

    def test_invalid_late_concentration_correction_exposes_the_existing_hour(self):
        config_model = sys.modules[f"{PACKAGE}.config_model"]
        config = config_model.BeestatConfig(
            thermostats=(),
            sensors=(
                config_model.ConfiguredSensor(
                    sensor_id=10,
                    slug="room",
                    name="Room",
                    thermostat_id=1,
                    thermostat_slug="zone",
                    include_temperature=False,
                    include_air_quality=False,
                    include_co2=True,
                    include_voc=False,
                ),
            ),
        )
        points = [
            {
                "sensor_id": 10,
                "timestamp": (START + timedelta(minutes=5 * index)).isoformat(),
                "co2_concentration": 600,
            }
            for index in range(12)
        ]

        def produce():
            return next(
                item
                for item in builder.build_hourly_statistics(
                    {},
                    {10: points},
                    config,
                    start=START,
                    end=START + HOUR,
                    evaluated_at=START + HOUR,
                    source_end_by_thermostat={1: START + timedelta(minutes=55)},
                )
                if item.statistic_id == "beestat:room_co2_concentration_hourly_v2"
            )

        initial = produce()
        accepted = _plan(initial, snapshot=_snapshot(initial))
        self.assertEqual(600, accepted.unblocked_rows[0].mean)
        points.append({**points[0], "co2_concentration": -1})
        corrected = produce()
        result = _plan(
            corrected, snapshot=_snapshot(corrected, accepted.unblocked_rows)
        )
        self.assertEqual(11, result.coverage[0].valid_slots)
        self.assertEqual(1, result.coverage[0].invalid_slots)
        self.assertEqual((START,), result.stale_starts)
        self.assertIn("surviving_stale_rows", result.blocking_reasons)
        self.assertFalse(result.unblocked_rows)


if __name__ == "__main__":
    unittest.main()
