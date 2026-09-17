"""Behavior of independent v3 mean rows and stable physical bindings."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_history_values_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
module_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_history_values", ROOT / "hourly_history_values.py"
)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError("Unable to load hourly history values")
values = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = values
module_spec.loader.exec_module(values)
hourly = sys.modules[f"{PACKAGE}.hourly_statistics"]
config_model = sys.modules[f"{PACKAGE}.config_model"]


class HourlyHistoryValuesTest(unittest.TestCase):
    """Convert real builder output without changing source validity rules."""

    def setUp(self):
        self.start = datetime(2026, 7, 1, tzinfo=UTC)
        self.end = self.start + timedelta(hours=1)
        self.config = config_model.BeestatConfig(
            thermostats=(config_model.ConfiguredThermostat(1, "zone", "Zone"),),
            sensors=(
                config_model.ConfiguredSensor(
                    sensor_id=10,
                    slug="room",
                    name="Room",
                    thermostat_id=1,
                    thermostat_slug="zone",
                    include_temperature=True,
                    include_air_quality=True,
                    include_co2=True,
                    include_voc=True,
                    occupancy_entity_id="binary_sensor.room_occupancy",
                ),
            ),
        )

    def thermostat_rows(self, *, start=None, thermostat_id=1, **changes):
        return [
            {
                "timestamp": (
                    (start or self.start) + timedelta(minutes=5 * i)
                ).isoformat(),
                "thermostat_id": thermostat_id,
                "compressor_mode": "cool",
                "compressor_1": 90,
                "compressor_2": 0,
                "auxiliary_heat_1": 30,
                "auxiliary_heat_2": 0,
                "fan": 150,
                "accessory_type": "off",
                "accessory": 0,
                "indoor_humidity": 45,
                "outdoor_humidity": 60,
                "outdoor_temperature": 89,
                "setpoint_heat": 68,
                "setpoint_cool": 74,
                **changes,
            }
            for i in range(12)
        ]

    def sensor_rows(self, *, sensor_id=10, **changes):
        return [
            {
                "timestamp": (self.start + timedelta(minutes=5 * i)).isoformat(),
                "sensor_id": sensor_id,
                "temperature": 70 + i,
                "occupancy": i % 2 == 0,
                "air_quality": 30,
                "co2_concentration": 600,
                "voc_concentration": 75,
                **changes,
            }
            for i in range(12)
        ]

    def build(self, *, rows=None, sensors=None, end=None, config=None):
        config = config or self.config
        end = end or self.end
        result = hourly.build_hourly_statistics(
            {1: self.thermostat_rows()} if rows is None else rows,
            {10: self.sensor_rows()} if sensors is None else sensors,
            config,
            start=self.start,
            end=end,
            evaluated_at=end,
            source_end_by_thermostat={
                item.thermostat_id: end - timedelta(minutes=5)
                for item in config.thermostats
            },
        )
        resources = {}
        for item in result:
            base = item.statistic_id.removesuffix("_hourly_v2")
            thermostat = next(
                (
                    item
                    for item in config.thermostats
                    if base.startswith(f"beestat:{item.slug}_")
                ),
                None,
            )
            if thermostat is not None:
                resources[item.statistic_id] = {
                    "thermostat_id": thermostat.thermostat_id,
                    "sensor_id": None,
                    "quantity": base.removeprefix(f"beestat:{thermostat.slug}_"),
                }
            else:
                sensor = next(
                    item
                    for item in config.sensors
                    if base.startswith(f"beestat:{item.slug}_")
                )
                resources[item.statistic_id] = {
                    "thermostat_id": sensor.thermostat_id,
                    "sensor_id": sensor.sensor_id,
                    "quantity": base.removeprefix(f"beestat:{sensor.slug}_"),
                }
        return result, {"resources": resources}

    def convert(self, **kwargs):
        series, identity = self.build(**kwargs)
        return values.build_history_series(series, identity)

    def item(self, result, quantity, *, sensor=False):
        return next(
            item
            for item in result
            if item.descriptor["quantity"] == quantity
            and (item.descriptor["sensor_id"] is not None) == sensor
        )

    def test_first_ready_hour_after_gap_has_its_own_amount_without_predecessor(self):
        rows = self.thermostat_rows()[:-1] + self.thermostat_rows(start=self.end)
        item = self.item(
            self.convert(rows={1: rows}, end=self.end + timedelta(hours=1)),
            "fan_runtime_hours",
        )
        self.assertEqual(
            [bucket.reason for bucket in item.hours], ["missing_slots", "ready"]
        )
        self.assertIsNone(item.hours[0].values)
        self.assertEqual(item.hours[1].values, {"mean": 50.0})
        self.assertEqual(
            item.hours[1].values["mean"]
            * item.descriptor["representation"]["logical_multiplier"],
            0.5,
        )
        self.assertFalse(item.metadata["has_sum"])
        self.assertEqual(item.metadata["mean_type"], 1)

    def test_heat_can_exceed_one_hour_and_zero_is_a_valid_observation(self):
        result = self.convert(
            rows={
                1: self.thermostat_rows(
                    compressor_mode="heat",
                    compressor_1=300,
                    auxiliary_heat_1=150,
                    fan=0,
                )
            }
        )
        heat = self.item(result, "heat_runtime_hours")
        self.assertEqual(heat.hours[0].values, {"mean": 150.0})
        self.assertEqual(heat.statistic_id, "beestat:zone_heat_runtime_rate_hourly_v3")
        self.assertEqual(heat.metadata["unit_of_measurement"], "%")
        self.assertEqual(heat.metadata["unit_class"], "unitless")
        self.assertEqual(
            self.item(result, "fan_runtime_hours").hours[0].values, {"mean": 0.0}
        )
        self.assertEqual(
            self.item(result, "cool_runtime_hours").hours[0].values, {"mean": 0.0}
        )

    def test_temperature_departure_is_delta_and_degree_day_math_is_unchanged(self):
        for temperature, quantity, departure in (
            (89, "cooling_degree_days", 24),
            (41, "heating_degree_days", 24),
            (65, "heating_degree_days", 0),
        ):
            with self.subTest(temperature=temperature):
                source, identity = self.build(
                    rows={1: self.thermostat_rows(outdoor_temperature=temperature)}
                )
                converted = self.item(
                    values.build_history_series(source, identity), quantity
                )
                before = next(
                    item
                    for item in source
                    if item.statistic_id.endswith(f"_{quantity}_hourly_v2")
                )
                self.assertEqual(converted.hours[0].values, {"mean": departure})
                self.assertEqual(converted.metadata["unit_class"], "temperature_delta")
                self.assertEqual(converted.metadata["unit_of_measurement"], "°F")
                self.assertEqual(converted.descriptor["logical_unit"], "°F·day")
                self.assertAlmostEqual(
                    converted.hours[0].values["mean"] / 24,
                    before.hours[0].values["increment"],
                )
                self.assertTrue(
                    converted.statistic_id.endswith("_temperature_departure_hourly_v3")
                )

    def test_measurement_extrema_and_quantity_specific_invalidity_are_preserved(self):
        rows = self.thermostat_rows()
        rows[-1]["fan"] = True
        result = self.convert(rows={1: rows})
        self.assertEqual(
            self.item(result, "temperature", sensor=True).hours[0].values,
            {"mean": 75.5, "min": 70.0, "max": 81.0},
        )
        fan = self.item(result, "fan_runtime_hours").hours[0]
        self.assertEqual(
            (fan.valid_slots, fan.invalid_slots, fan.reason), (11, 1, "invalid_slots")
        )
        self.assertIsNone(fan.values)
        self.assertEqual(
            self.item(result, "cool_runtime_hours").hours[0].reason, "ready"
        )

    def test_41_quantity_denominator_keeps_voc_blocked_and_optional_rules_unchanged(
        self,
    ):
        config = config_model.BeestatConfig(
            thermostats=tuple(
                config_model.ConfiguredThermostat(i, f"zone{i}", f"Zone {i}")
                for i in (1, 2)
            ),
            sensors=tuple(
                config_model.ConfiguredSensor(
                    sensor_id=i,
                    slug=f"room{i}",
                    name=f"Room {i}",
                    thermostat_id=1 if i < 14 else 2,
                    thermostat_slug="zone1" if i < 14 else "zone2",
                    include_temperature=True,
                    include_air_quality=i == 10,
                    include_co2=i == 10,
                    include_voc=i == 10,
                    occupancy_entity_id=f"binary_sensor.room{i}_occupancy",
                )
                for i in range(10, 17)
            ),
        )
        source, identity = self.build(
            config=config,
            rows={i: self.thermostat_rows(thermostat_id=i) for i in (1, 2)},
            sensors={i: self.sensor_rows(sensor_id=i) for i in range(10, 17)},
        )
        preimage = deepcopy(source)
        result = values.build_history_series(source, identity)
        self.assertEqual(len(result), 41)
        self.assertEqual(
            sum(item.descriptor["admission"] == "eligible" for item in result), 40
        )
        voc = self.item(result, "voc_concentration", sensor=True)
        self.assertEqual(voc.hours, ())
        self.assertEqual(voc.blocked_reason, "voc_unit_unresolved")
        self.assertEqual(voc.descriptor["admission"], "blocked")
        self.assertTrue(
            all(
                not item.metadata["has_sum"] and item.metadata["mean_type"] == 1
                for item in result
            )
        )
        self.assertEqual(source, preimage)
        self.assertFalse(
            any(
                item.descriptor["quantity"] == "cool_stage_2_runtime_hours"
                for item in result
            )
        )

    def test_saved_physical_owner_keeps_original_ids_and_aliases_after_rename(self):
        source, identity = self.build()
        previous = values.build_history_series(source, identity)
        saved = {item.descriptor["quantity_id"]: item.descriptor for item in previous}
        before = deepcopy(saved)
        renamed = tuple(
            replace(
                item,
                metadata={
                    **item.metadata,
                    "statistic_id": item.statistic_id.replace(
                        "beestat:zone_", "beestat:renamed_"
                    ),
                },
            )
            for item in source
        )
        renamed_identity = {
            "resources": {
                key.replace("beestat:zone_", "beestat:renamed_"): resource
                for key, resource in identity["resources"].items()
            }
        }
        current = values.build_history_series(renamed, renamed_identity, saved=saved)
        for old, new in zip(previous, current, strict=True):
            self.assertEqual(new.statistic_id, old.statistic_id)
            self.assertEqual(
                new.descriptor["representation"], old.descriptor["representation"]
            )
            self.assertTrue(
                set(old.descriptor["legacy_statistic_ids"])
                <= set(new.descriptor["legacy_statistic_ids"])
            )
        self.assertIn(
            "beestat:renamed_fan_runtime_hours",
            self.item(current, "fan_runtime_hours").descriptor["legacy_statistic_ids"],
        )
        self.assertEqual(saved, before)

    def test_inventory_only_descriptors_do_not_claim_admission_or_create_rows(self):
        source, identity = self.build()
        inventory = tuple(replace(item, hours=(), source_rows=0) for item in source)
        result = values.build_history_series(inventory, identity)
        self.assertEqual(len(result), len(source))
        self.assertTrue(
            all(not item.hours and item.source_rows == 0 for item in result)
        )
        self.assertTrue(
            all(item.descriptor["writer_status"] == "unselected" for item in result)
        )
        self.assertEqual(
            self.item(result, "fan_runtime_hours").descriptor["admission"], "eligible"
        )
        self.assertEqual(
            self.item(result, "voc_concentration", sensor=True).descriptor["admission"],
            "blocked",
        )

    def test_physical_parent_unit_and_alias_collisions_fail_closed(self):
        source, identity = self.build()
        saved = {
            item.descriptor["quantity_id"]: item.descriptor
            for item in values.build_history_series(source, identity)
        }
        with self.assertRaisesRegex(ValueError, "identity_ambiguous"):
            values.build_history_series((source[0], source[0]), identity)
        changed = deepcopy(identity)
        changed["resources"]["beestat:room_temperature_hourly_v2"]["thermostat_id"] = 2
        with self.assertRaisesRegex(ValueError, "sensor_parent_changed"):
            values.build_history_series(source, changed, saved=saved)
        changed_saved = deepcopy(saved)
        changed_saved["thermostat:1:fan_runtime_hours"]["logical_unit"] = "min"
        with self.assertRaisesRegex(ValueError, "quantity_contract_changed"):
            values.build_history_series(source, identity, saved=changed_saved)
        changed_identity = deepcopy(identity)
        for resource in changed_identity["resources"].values():
            resource["thermostat_id"] = 2
        only_thermostats = tuple(
            item
            for item in source
            if identity["resources"][item.statistic_id]["sensor_id"] is None
        )
        with self.assertRaisesRegex(ValueError, "statistic_identity_collision"):
            values.build_history_series(only_thermostats, changed_identity, saved=saved)


if __name__ == "__main__":
    unittest.main()
