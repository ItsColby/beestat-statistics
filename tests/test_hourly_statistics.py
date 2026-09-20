"""Behavior checks for the pure, coverage-qualified hourly producer."""

from __future__ import annotations

import importlib.util
import math
import sys
import types
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_statistics_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
module_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_statistics", ROOT / "hourly_statistics.py"
)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError("Unable to load hourly statistics")
hourly = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = hourly
module_spec.loader.exec_module(hourly)
config_model = sys.modules[f"{PACKAGE}.config_model"]


class HourlyStatisticsTest(unittest.TestCase):
    """Exercise values, temporal coverage and source corrections together."""

    def setUp(self) -> None:
        self.start = datetime(2026, 7, 1, 4, tzinfo=UTC)
        self.end = self.start + timedelta(hours=1)
        self.config = config_model.BeestatConfig(
            thermostats=(config_model.ConfiguredThermostat(1, "zone_a", "Zone A"),),
            sensors=(
                config_model.ConfiguredSensor(
                    sensor_id=10,
                    slug="room_a",
                    name="Room A",
                    thermostat_id=1,
                    thermostat_slug="zone_a",
                    include_temperature=True,
                    include_air_quality=True,
                    include_co2=True,
                    include_voc=True,
                    occupancy_entity_id="binary_sensor.room_a_occupancy",
                ),
            ),
        )

    def thermostat_rows(self, start=None, count=12, **changes):
        start = start or self.start
        return [
            {
                "thermostat_id": 1,
                "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                "compressor_mode": "cool",
                "compressor_1": 90,
                "compressor_2": 30,
                "auxiliary_heat_1": 0,
                "auxiliary_heat_2": 0,
                "fan": 150,
                "accessory_type": "humidifier",
                "accessory": 10,
                "indoor_humidity": 45,
                "outdoor_humidity": 60,
                "outdoor_temperature": 89,
                "setpoint_heat": 68,
                "setpoint_cool": 74,
                **changes,
            }
            for index in range(count)
        ]

    def sensor_rows(self, start=None, count=12, **changes):
        start = start or self.start
        return [
            {
                "sensor_id": 10,
                "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                "temperature": 70 + index,
                "occupancy": index % 2 == 0,
                "air_quality": 30,
                "co2_concentration": 600,
                "voc_concentration": 75,
                **changes,
            }
            for index in range(count)
        ]

    def build(self, thermostat_rows=None, sensor_rows=None, **options):
        arguments = {
            "start": self.start,
            "end": self.end,
            "evaluated_at": self.end,
            "source_end_by_thermostat": {1: self.end - timedelta(minutes=5)},
            **options,
        }
        return hourly.build_hourly_statistics(
            {1: self.thermostat_rows() if thermostat_rows is None else thermostat_rows},
            {10: self.sensor_rows() if sensor_rows is None else sensor_rows},
            self.config,
            **arguments,
        )

    def item(self, series, suffix):
        return next(
            item
            for item in series
            if item.statistic_id == f"beestat:{suffix}_hourly_v2"
        )

    def test_adjacent_hours_preserve_samples_without_daily_averaging(self):
        rows = self.sensor_rows(temperature=70) + self.sensor_rows(
            self.end, temperature=80
        )
        stop = self.end + timedelta(hours=1)
        series = self.build(
            sensor_rows=rows,
            end=stop,
            evaluated_at=stop,
            source_end_by_thermostat={1: stop - timedelta(minutes=5)},
        )
        item = self.item(series, "room_a_temperature")
        self.assertEqual(
            [bucket.start for bucket in item.hours], [self.start, self.end]
        )
        self.assertEqual([bucket.values["mean"] for bucket in item.hours], [70, 80])
        self.assertTrue(all(bucket.reason == "ready" for bucket in item.hours))
        self.assertTrue(
            all(item.statistic_id.endswith("_hourly_v2") for item in series)
        )
        self.assertEqual(item.metadata["unit_of_measurement"], "°F")
        self.assertEqual(item.metadata["mean_type"], 1)

    def test_all_declared_families_and_units_are_present_when_retained(self):
        existing = tuple(
            f"beestat:zone_a_{slug}_runtime_hours_hourly_v2"
            for slug, _label, _field in hourly.DETAILED_RUNTIME_FIELDS
        )
        series = self.build(existing_statistic_ids=existing)
        self.assertEqual(len(series), 25)
        self.assertEqual(len({item.statistic_id for item in series}), 25)
        self.assertEqual(sum(item.metadata["has_sum"] for item in series), 15)
        self.assertEqual(
            self.item(series, "zone_a_fan_runtime_hours").metadata["unit_class"],
            "duration",
        )
        self.assertEqual(
            self.item(series, "room_a_air_quality").metadata["unit_of_measurement"], "%"
        )
        self.assertEqual(
            self.item(series, "room_a_co2_concentration").metadata[
                "unit_of_measurement"
            ],
            "ppm",
        )

    def test_runtime_preserves_exclusive_stage_seconds_and_accessory_routing(self):
        series = self.build()
        expected = {
            "cool": 0.4,
            "cool_stage_1": 0.3,
            "cool_stage_2": 0.1,
            "heat": 0,
            "fan": 0.5,
            "humidifier": 1 / 30,
        }
        for suffix, value in expected.items():
            with self.subTest(suffix=suffix):
                item = self.item(series, f"zone_a_{suffix}_runtime_hours")
                self.assertAlmostEqual(item.hours[0].values["increment"], value)
                self.assertEqual(set(item.hours[0].values), {"increment"})
                self.assertEqual(item.metadata["mean_type"], 0)
        self.assertNotIn(
            "beestat:zone_a_dehumidifier_runtime_hours_hourly_v2",
            {item.statistic_id for item in series},
        )

    def test_all_compressor_auxiliary_and_accessory_routes(self):
        for mode, expected_cool, expected_heat in (
            ("cool", 0.4, 0.1),
            ("heat", 0, 0.5),
            ("off", 0, 0.1),
        ):
            with self.subTest(mode=mode):
                rows = self.thermostat_rows(
                    compressor_mode=mode,
                    compressor_1=0 if mode == "off" else 90,
                    compressor_2=0 if mode == "off" else 30,
                    auxiliary_heat_1=20,
                    auxiliary_heat_2=10,
                )
                series = self.build(thermostat_rows=rows)
                self.assertAlmostEqual(
                    self.item(series, "zone_a_cool_runtime_hours")
                    .hours[0]
                    .values["increment"],
                    expected_cool,
                )
                self.assertAlmostEqual(
                    self.item(series, "zone_a_heat_runtime_hours")
                    .hours[0]
                    .values["increment"],
                    expected_heat,
                )
        for accessory in ("humidifier", "dehumidifier", "ventilator", "economizer"):
            with self.subTest(accessory=accessory):
                series = self.build(
                    thermostat_rows=self.thermostat_rows(accessory_type=accessory)
                )
                self.assertAlmostEqual(
                    self.item(series, f"zone_a_{accessory}_runtime_hours")
                    .hours[0]
                    .values["increment"],
                    1 / 30,
                )

    def test_missing_slots_are_not_zero_and_are_quantity_specific(self):
        rows = self.sensor_rows()[:-1]
        series = self.build(sensor_rows=rows)
        bucket = self.item(series, "room_a_temperature").hours[0]
        self.assertEqual(
            (bucket.valid_slots, bucket.missing_slots, bucket.invalid_slots), (11, 1, 0)
        )
        self.assertEqual(bucket.reason, "missing_slots")
        self.assertIsNone(bucket.values)
        self.assertEqual(
            self.item(series, "zone_a_fan_runtime_hours").hours[0].reason, "ready"
        )

    def test_last_offset_equivalent_correction_wins_even_when_invalid_or_deleted(self):
        for change in (
            {"temperature": 90},
            {"temperature": "NaN"},
            {"deleted": True},
            {"deleted": "true"},
        ):
            with self.subTest(change=change):
                rows = self.sensor_rows(temperature=70)
                rows.append(
                    {**rows[0], "timestamp": "2026-07-01T00:00:00-04:00", **change}
                )
                bucket = self.item(
                    self.build(sensor_rows=rows), "room_a_temperature"
                ).hours[0]
                self.assertEqual(bucket.duplicate_slots, 1)
                if change == {"temperature": 90}:
                    self.assertAlmostEqual(bucket.values["mean"], 70 + 20 / 12)
                else:
                    self.assertEqual(bucket.reason, "invalid_slots")
                    self.assertEqual(bucket.invalid_slots, 1)
                    self.assertIsNone(bucket.values)

    def test_correction_replay_can_restore_an_invalid_slot(self):
        rows = self.sensor_rows(temperature=70)
        rows[0]["temperature"] = None
        rows.append({**rows[0], "temperature": 70, "deleted": "false"})
        bucket = self.item(self.build(sensor_rows=rows), "room_a_temperature").hours[0]
        self.assertEqual(bucket.reason, "ready")
        self.assertEqual(bucket.values["mean"], 70)
        self.assertEqual(bucket.valid_slots, 12)

    def test_malformed_and_off_grid_timestamps_are_visible(self):
        rows = self.sensor_rows()
        rows.extend(
            {**rows[0], "timestamp": stamp}
            for stamp in (
                None,
                "bad",
                "2026-07-01T04:01:00Z",
                "2026-07-01T04:05:00.000001Z",
            )
        )
        item = self.item(self.build(sensor_rows=rows), "room_a_temperature")
        self.assertEqual(item.rejected_timestamps, 4)
        self.assertEqual(item.source_rows, 16)
        self.assertEqual(item.hours[0].reason, "ready")

    def test_outside_parseable_rows_are_filtered_before_quality_checks(self):
        rows = self.sensor_rows(temperature=70)
        for stamp in (
            self.start - timedelta(minutes=5),
            self.start - timedelta(seconds=1),
            self.end,
            self.end + timedelta(seconds=1),
        ):
            rows.extend(
                {
                    **rows[0],
                    "timestamp": stamp.isoformat(),
                    "sensor_id": identity,
                    "temperature": 99,
                }
                for identity in (10, 11)
            )
        item = self.item(self.build(sensor_rows=rows), "room_a_temperature")
        self.assertEqual(item.rejected_timestamps, 0)
        self.assertIsNone(item.blocked_reason)
        self.assertEqual(item.source_rows, 20)
        self.assertEqual(item.hours[0].values, {"mean": 70, "min": 70, "max": 70})
        self.assertEqual(item.hours[0].duplicate_slots, 0)

    def test_scoped_starts_isolate_quality_for_shared_resource_quantities(self):
        stop = self.end + timedelta(hours=1)
        starts = {
            f"beestat:{suffix}_hourly_v2": self.end
            for suffix in (
                "zone_a_cool_runtime_hours",
                "zone_a_indoor_humidity",
                "room_a_temperature",
            )
        }
        for off_grid in (False, True):
            with self.subTest(off_grid=off_grid):
                stamp = self.start + timedelta(minutes=1 if off_grid else 0)
                thermostat_rows = self.thermostat_rows(count=24)
                sensor_rows = self.sensor_rows(count=24)
                thermostat_rows.append(
                    {
                        **thermostat_rows[0],
                        "timestamp": stamp.isoformat(),
                        "thermostat_id": 1 if off_grid else 2,
                    }
                )
                sensor_rows.append(
                    {
                        **sensor_rows[0],
                        "timestamp": stamp.isoformat(),
                        "sensor_id": 10 if off_grid else 11,
                    }
                )
                series = self.build(
                    thermostat_rows=thermostat_rows,
                    sensor_rows=sensor_rows,
                    end=stop,
                    evaluated_at=stop,
                    start_by_statistic_id=starts,
                    source_end_by_thermostat={1: stop - timedelta(minutes=5)},
                )
                for suffix in (
                    "zone_a_cool_runtime_hours",
                    "zone_a_indoor_humidity",
                    "room_a_temperature",
                ):
                    item = self.item(series, suffix)
                    self.assertEqual([hour.start for hour in item.hours], [self.end])
                    self.assertEqual(item.hours[0].reason, "ready")
                    self.assertEqual(item.rejected_timestamps, 0)
                    self.assertEqual(item.source_rows, 25)
                    self.assertIsNone(item.blocked_reason)
                for suffix in ("zone_a_fan_runtime_hours", "room_a_air_quality"):
                    item = self.item(series, suffix)
                    if off_grid:
                        self.assertEqual(item.rejected_timestamps, 1)
                    else:
                        self.assertEqual(
                            item.blocked_reason, "resource_identity_mismatch"
                        )

    def test_scoped_start_preserves_in_window_and_unpositionable_rejections(self):
        stop = self.end + timedelta(hours=1)
        starts = {"beestat:room_a_temperature_hourly_v2": self.end}
        for stamp, identity, rejected, blocked in (
            ((self.end + timedelta(minutes=1)).isoformat(), 10, 1, None),
            (self.end.isoformat(), 11, 0, "resource_identity_mismatch"),
            (None, 10, 1, None),
            ("unpositionable", 10, 1, None),
        ):
            with self.subTest(stamp=stamp, identity=identity):
                rows = self.sensor_rows(count=24)
                rows.append({**rows[0], "timestamp": stamp, "sensor_id": identity})
                item = self.item(
                    self.build(
                        sensor_rows=rows,
                        end=stop,
                        evaluated_at=stop,
                        start_by_statistic_id=starts,
                        source_end_by_thermostat={1: stop - timedelta(minutes=5)},
                    ),
                    "room_a_temperature",
                )
                self.assertEqual(item.rejected_timestamps, rejected)
                self.assertEqual(item.blocked_reason, blocked)

    def test_optional_runtime_discovery_uses_its_scoped_points(self):
        stop = self.end + timedelta(hours=1)
        suffix = "zone_a_ventilator_runtime_hours"
        successor = f"beestat:{suffix}_hourly_v2"
        rows = self.thermostat_rows(accessory_type="ventilator")
        rows.extend(self.thermostat_rows(self.end, accessory_type="off", accessory=0))
        options = {
            "thermostat_rows": rows,
            "end": stop,
            "evaluated_at": stop,
            "start_by_statistic_id": {successor: self.end},
            "source_end_by_thermostat": {1: stop - timedelta(minutes=5)},
        }
        series = self.build(**options)
        self.assertNotIn(successor, {item.statistic_id for item in series})
        # Discovery of another quantity on the same resource keeps its own range.
        self.assertEqual(
            len(self.item(series, "zone_a_cool_stage_1_runtime_hours").hours), 2
        )
        for retained in (f"beestat:{suffix}", successor):
            with self.subTest(retained=retained):
                item = self.item(
                    self.build(**options, existing_statistic_ids=(retained,)), suffix
                )
                self.assertEqual([hour.start for hour in item.hours], [self.end])
                self.assertEqual(item.hours[0].values, {"increment": 0})

    def test_measurement_end_scopes_quality_without_shortening_cumulative(self):
        stop = self.end + timedelta(hours=1)
        for off_grid in (False, True):
            with self.subTest(off_grid=off_grid):
                stamp = self.end + timedelta(minutes=1 if off_grid else 0)
                thermostat_rows = self.thermostat_rows(count=24)
                sensor_rows = self.sensor_rows(count=24)
                thermostat_rows.append(
                    {
                        **thermostat_rows[0],
                        "timestamp": stamp.isoformat(),
                        "thermostat_id": 1 if off_grid else 2,
                    }
                )
                sensor_rows.append(
                    {
                        **sensor_rows[0],
                        "timestamp": stamp.isoformat(),
                        "sensor_id": 10 if off_grid else 11,
                    }
                )
                series = self.build(
                    thermostat_rows=thermostat_rows,
                    sensor_rows=sensor_rows,
                    end=stop,
                    evaluated_at=stop,
                    measurement_end=self.end,
                    source_end_by_thermostat={1: stop - timedelta(minutes=5)},
                )
                for suffix in ("zone_a_indoor_humidity", "room_a_temperature"):
                    item = self.item(series, suffix)
                    self.assertEqual([hour.start for hour in item.hours], [self.start])
                    self.assertEqual(item.hours[0].reason, "ready")
                    self.assertEqual(item.rejected_timestamps, 0)
                    self.assertIsNone(item.blocked_reason)
                runtime = self.item(series, "zone_a_fan_runtime_hours")
                if off_grid:
                    self.assertEqual(runtime.rejected_timestamps, 1)
                    self.assertEqual(len(runtime.hours), 2)
                else:
                    self.assertEqual(
                        runtime.blocked_reason, "resource_identity_mismatch"
                    )

    def test_empty_scopes_and_measurement_end_before_epoch_have_no_hours(self):
        stop = self.end + timedelta(hours=1)
        series = self.build(
            thermostat_rows=self.thermostat_rows(count=24),
            sensor_rows=self.sensor_rows(count=24),
            end=stop,
            evaluated_at=stop,
            measurement_end=self.end,
            start_by_statistic_id={
                "beestat:zone_a_fan_runtime_hours_hourly_v2": stop,
                "beestat:zone_a_indoor_humidity_hourly_v2": stop,
                "beestat:room_a_temperature_hourly_v2": self.end,
            },
            source_end_by_thermostat={1: stop - timedelta(minutes=5)},
        )
        for suffix in (
            "zone_a_fan_runtime_hours",
            "zone_a_indoor_humidity",
            "room_a_temperature",
        ):
            item = self.item(series, suffix)
            self.assertEqual(item.hours, ())
            self.assertEqual(item.source_rows, 24)
            self.assertEqual(item.rejected_timestamps, 0)
            self.assertIsNone(item.blocked_reason)
        self.assertEqual(len(self.item(series, "zone_a_cool_runtime_hours").hours), 2)
        self.assertEqual(len(self.item(series, "room_a_air_quality").hours), 1)

    def test_scoped_bounds_validate_acquisition_limits_and_utc_hours(self):
        statistic_id = "beestat:room_a_temperature_hourly_v2"
        for value in (
            self.start.replace(tzinfo=None),
            self.start - timedelta(hours=1),
            self.end + timedelta(hours=1),
            self.start + timedelta(minutes=1),
        ):
            for options in (
                {"start_by_statistic_id": {statistic_id: value}},
                {"measurement_end": value},
            ):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.build(**options)
        local_start = self.start.astimezone(ZoneInfo("America/New_York"))
        local_end = self.end.astimezone(ZoneInfo("America/New_York"))
        item = self.item(
            self.build(
                start_by_statistic_id={statistic_id: local_start},
                measurement_end=local_end,
            ),
            "room_a_temperature",
        )
        self.assertEqual(item.hours[0].start, self.start)
        self.assertEqual(item.hours[0].reason, "ready")

    def test_separate_thermostats_do_not_share_quality_cache(self):
        config = replace(
            self.config,
            thermostats=(
                *self.config.thermostats,
                config_model.ConfiguredThermostat(2, "zone_b", "Zone B"),
            ),
        )
        bad_rows = self.thermostat_rows()
        bad_rows.append({**bad_rows[0], "thermostat_id": 3})
        series = hourly.build_hourly_statistics(
            {1: bad_rows, 2: self.thermostat_rows(thermostat_id=2)},
            {},
            config,
            start=self.start,
            end=self.end,
            evaluated_at=self.end,
            source_end_by_thermostat={
                1: self.end - timedelta(minutes=5),
                2: self.end - timedelta(minutes=5),
            },
        )
        self.assertEqual(
            self.item(series, "zone_a_fan_runtime_hours").blocked_reason,
            "resource_identity_mismatch",
        )
        valid = self.item(series, "zone_b_fan_runtime_hours")
        self.assertIsNone(valid.blocked_reason)
        self.assertEqual(valid.hours[0].reason, "ready")
        self.assertEqual(valid.hours[0].values, {"increment": 0.5})

    def test_zero_samples_and_occupancy_are_valid_without_aqi_rescaling(self):
        series = self.build(
            sensor_rows=self.sensor_rows(
                temperature=0, air_quality=0, co2_concentration=0, occupancy=False
            )
        )
        for suffix in ("temperature", "air_quality", "co2_concentration", "occupancy"):
            bucket = self.item(series, f"room_a_{suffix}").hours[0]
            self.assertEqual(bucket.reason, "ready")
            self.assertEqual(bucket.values, {"mean": 0, "min": 0, "max": 0})
        series = self.build()
        self.assertEqual(
            self.item(series, "room_a_air_quality").hours[0].values["mean"], 30
        )
        self.assertEqual(
            self.item(series, "room_a_occupancy").hours[0].values["mean"], 50
        )

    def test_invalid_values_do_not_contaminate_other_quantities(self):
        for invalid in (None, True, "unknown", float("nan"), float("inf"), -460):
            with self.subTest(invalid=invalid):
                rows = self.sensor_rows()
                rows[0]["temperature"] = invalid
                series = self.build(sensor_rows=rows)
                bucket = self.item(series, "room_a_temperature").hours[0]
                self.assertEqual(bucket.reason, "invalid_slots")
                self.assertEqual(bucket.invalid_slots, 1)
                self.assertEqual(
                    self.item(series, "room_a_air_quality").hours[0].reason, "ready"
                )
        series = self.build(sensor_rows=self.sensor_rows(temperature=-459.7))
        self.assertEqual(
            self.item(series, "room_a_temperature").hours[0].reason, "ready"
        )

    def test_large_finite_temperature_mean_stays_finite(self):
        value = sys.float_info.max
        item = self.item(
            self.build(sensor_rows=self.sensor_rows(temperature=value)),
            "room_a_temperature",
        )
        self.assertEqual(item.hours[0].values["mean"], value)
        self.assertTrue(
            all(math.isfinite(value) for value in item.hours[0].values.values())
        )

    def test_invalid_counter_or_mode_blocks_only_affected_quantity(self):
        for invalid in (-1, 301, None, True, float("inf")):
            with self.subTest(invalid=invalid):
                rows = self.thermostat_rows()
                rows[0]["fan"] = invalid
                series = self.build(thermostat_rows=rows)
                self.assertEqual(
                    self.item(series, "zone_a_fan_runtime_hours").hours[0].reason,
                    "invalid_slots",
                )
                self.assertEqual(
                    self.item(series, "zone_a_cool_runtime_hours").hours[0].reason,
                    "ready",
                )
        rows = self.thermostat_rows(compressor_mode="invalid")
        series = self.build(thermostat_rows=rows)
        self.assertEqual(
            self.item(series, "zone_a_cool_runtime_hours").hours[0].reason,
            "invalid_slots",
        )
        self.assertEqual(
            self.item(series, "zone_a_fan_runtime_hours").hours[0].reason, "ready"
        )

    def test_missing_required_raw_field_is_not_an_invented_zero(self):
        rows = self.thermostat_rows()
        del rows[0]["fan"]
        bucket = self.item(
            self.build(thermostat_rows=rows), "zone_a_fan_runtime_hours"
        ).hours[0]
        self.assertEqual(bucket.reason, "invalid_slots")
        self.assertIsNone(bucket.values)

    def test_bad_routing_and_impossible_exclusive_pairs_are_not_zero(self):
        for changes in (
            {"compressor_mode": []},
            {"compressor_mode": None},
            {"compressor_mode": "off"},
            {"compressor_1": 200, "compressor_2": 200},
        ):
            with self.subTest(changes=changes):
                series = self.build(thermostat_rows=self.thermostat_rows(**changes))
                bucket = self.item(series, "zone_a_cool_runtime_hours").hours[0]
                self.assertEqual(bucket.reason, "invalid_slots")
                self.assertIsNone(bucket.values)
        for changes in ({"accessory_type": []}, {"accessory_type": "off"}):
            with self.subTest(changes=changes):
                series = self.build(
                    thermostat_rows=self.thermostat_rows(**changes),
                    existing_statistic_ids=("beestat:zone_a_humidifier_runtime_hours",),
                )
                self.assertEqual(
                    self.item(series, "zone_a_humidifier_runtime_hours")
                    .hours[0]
                    .reason,
                    "invalid_slots",
                )

    def test_current_hour_and_provider_tail_are_provisional(self):
        cases = (
            {"evaluated_at": self.end - timedelta(seconds=1)},
            {"source_end_by_thermostat": {1: self.end - timedelta(minutes=10)}},
            {"source_end_by_thermostat": {}},
        )
        for options in cases:
            with self.subTest(options=options):
                item = self.item(self.build(**options), "room_a_temperature")
                self.assertEqual(item.hours[0].reason, "provisional")
                self.assertEqual(item.hours[0].valid_slots, 12)
                self.assertIsNone(item.hours[0].values)
        item = self.item(self.build(source_end_by_thermostat={}), "room_a_temperature")
        self.assertEqual(item.blocked_reason, "source_horizon_unavailable")

    def test_voc_is_a_blocked_successor_without_trusted_values(self):
        item = self.item(self.build(), "room_a_voc_concentration")
        self.assertEqual(item.blocked_reason, "voc_unit_unresolved")
        self.assertEqual(item.hours, ())
        self.assertEqual(item.source_rows, 12)

    def test_legacy_detailed_hardware_survives_all_zero_correction(self):
        rows = self.thermostat_rows(compressor_1=0, compressor_2=0)
        series = self.build(
            thermostat_rows=rows,
            existing_statistic_ids=("beestat:zone_a_cool_stage_1_runtime_hours",),
        )
        item = self.item(series, "zone_a_cool_stage_1_runtime_hours")
        self.assertEqual(item.hours[0].values["increment"], 0)

    def test_corrected_away_activity_does_not_create_unobserved_hardware(self):
        rows = self.thermostat_rows(accessory_type="off", accessory=0)
        rows.insert(0, {**rows[0], "accessory_type": "ventilator", "accessory": 100})
        series = self.build(thermostat_rows=rows)
        self.assertNotIn(
            "beestat:zone_a_ventilator_runtime_hours_hourly_v2",
            {item.statistic_id for item in series},
        )

    def test_fall_back_hours_remain_distinct_utc_instants(self):
        start = datetime(2026, 11, 1, 5, tzinfo=UTC)
        stop = start + timedelta(hours=2)
        rows = self.sensor_rows(start, temperature=70) + self.sensor_rows(
            start + timedelta(hours=1), temperature=80
        )
        local_tz = ZoneInfo("America/New_York")
        for row in rows:
            row["timestamp"] = (
                datetime.fromisoformat(row["timestamp"])
                .astimezone(local_tz)
                .isoformat()
            )
        series = self.build(
            sensor_rows=rows,
            start=start,
            end=stop,
            evaluated_at=stop,
            source_end_by_thermostat={1: stop - timedelta(minutes=5)},
        )
        item = self.item(series, "room_a_temperature")
        self.assertEqual([bucket.values["mean"] for bucket in item.hours], [70, 80])
        self.assertEqual(
            [bucket.start.astimezone(local_tz).fold for bucket in item.hours], [0, 1]
        )

    def test_degree_days_use_fixed_288_denominator_on_dst_days(self):
        local_tz = ZoneInfo("America/New_York")
        for month, day, hours in ((3, 8, 23), (11, 1, 25)):
            with self.subTest(hours=hours):
                start = datetime(2026, month, day, tzinfo=local_tz).astimezone(UTC)
                stop = (
                    datetime(2026, month, day, tzinfo=local_tz) + timedelta(days=1)
                ).astimezone(UTC)
                rows = self.thermostat_rows(
                    start, count=hours * 12, outdoor_temperature=89
                )
                series = self.build(
                    thermostat_rows=rows,
                    start=start,
                    end=stop,
                    evaluated_at=stop,
                    source_end_by_thermostat={1: stop - timedelta(minutes=5)},
                )
                item = self.item(series, "zone_a_cooling_degree_days")
                self.assertEqual(len(item.hours), hours)
                self.assertAlmostEqual(
                    sum(bucket.values["increment"] for bucket in item.hours), hours
                )
        series = self.build(
            thermostat_rows=self.thermostat_rows(outdoor_temperature=41)
        )
        self.assertEqual(
            self.item(series, "zone_a_heating_degree_days")
            .hours[0]
            .values["increment"],
            1,
        )
        self.assertEqual(
            self.item(series, "zone_a_cooling_degree_days")
            .hours[0]
            .values["increment"],
            0,
        )

    def test_identity_mismatch_blocks_misrouted_resource(self):
        rows = self.sensor_rows()
        rows.append({**rows[0], "sensor_id": 11, "temperature": 999})
        item = self.item(self.build(sensor_rows=rows), "room_a_temperature")
        self.assertEqual(item.blocked_reason, "resource_identity_mismatch")
        self.assertEqual(item.hours, ())
        rows = self.sensor_rows()
        rows[0]["sensor_id"] = "10"
        self.assertEqual(
            self.item(self.build(sensor_rows=rows), "room_a_temperature")
            .hours[0]
            .reason,
            "ready",
        )

    def test_naive_source_timestamp_is_utc_but_naive_bounds_are_rejected(self):
        rows = self.sensor_rows()
        rows[0]["timestamp"] = "2026-07-01 04:00:00"
        self.assertEqual(
            self.item(self.build(sensor_rows=rows), "room_a_temperature")
            .hours[0]
            .reason,
            "ready",
        )
        for options in (
            {"start": self.start.replace(tzinfo=None)},
            {"evaluated_at": self.end.replace(tzinfo=None)},
            {"start": self.start + timedelta(minutes=1)},
            {"end": self.start},
            {"end": self.start + timedelta(days=367)},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.build(**options)

    def test_unmapped_sensor_cannot_claim_a_source_horizon(self):
        self.config = replace(
            self.config, sensors=(replace(self.config.sensors[0], thermostat_id=None),)
        )
        item = self.item(self.build(), "room_a_temperature")
        self.assertEqual(item.blocked_reason, "source_horizon_unavailable")
        self.assertIsNone(item.hours[0].values)

    def test_invalid_concentrations_and_percentages_are_not_complete_observations(self):
        for field, suffix, invalid_values in (
            ("co2_concentration", "room_a_co2_concentration", (-1,)),
            ("air_quality", "room_a_air_quality", (-1, 101)),
            ("indoor_humidity", "zone_a_indoor_humidity", (-1, 101)),
            ("outdoor_humidity", "zone_a_outdoor_humidity", (-1, 101)),
        ):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    series = self.build(
                        thermostat_rows=self.thermostat_rows(**{field: value}),
                        sensor_rows=self.sensor_rows(**{field: value}),
                    )
                    hour = self.item(series, suffix).hours[0]
                    self.assertEqual("invalid_slots", hour.reason)
                    self.assertEqual(12, hour.invalid_slots)
                    self.assertEqual(0, hour.valid_slots)
                    self.assertIsNone(hour.values)

    def test_percentage_bounds_and_zero_concentration_remain_valid(self):
        for value in (0, 100):
            with self.subTest(value=value):
                series = self.build(
                    thermostat_rows=self.thermostat_rows(
                        indoor_humidity=value,
                        outdoor_humidity=value,
                    ),
                    sensor_rows=self.sensor_rows(
                        air_quality=value, co2_concentration=value
                    ),
                )
                for suffix in (
                    "zone_a_indoor_humidity",
                    "zone_a_outdoor_humidity",
                    "room_a_air_quality",
                    "room_a_co2_concentration",
                ):
                    self.assertEqual("ready", self.item(series, suffix).hours[0].reason)
        high = self.item(
            self.build(sensor_rows=self.sensor_rows(co2_concentration=1e100)),
            "room_a_co2_concentration",
        )
        self.assertEqual("ready", high.hours[0].reason)


if __name__ == "__main__":
    unittest.main()
