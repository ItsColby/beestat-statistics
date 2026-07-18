"""Tests for the read-only local configuration response."""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_configuration_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ConfigurationResponseTest(unittest.TestCase):
    """Validate the exact local response without using private fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        _load_module("const")
        cls.config_model = _load_module("config_model")
        cls.configuration = _load_module("configuration")

    def test_response_includes_saved_and_effective_configuration(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1001,
            slug="zone_a",
            name="Zone A",
            climate_entity_id="climate.zone_a",
            temperature_entity_id="sensor.zone_a_temperature",
            filter_changed_date=date(2026, 7, 1),
        )
        sensor = self.config_model.ConfiguredSensor(
            sensor_id=2002,
            slug="room_sensor_a",
            name="Room Sensor A",
            thermostat_id=1001,
            thermostat_slug="zone_a",
            include_temperature=True,
            include_air_quality=False,
            include_co2=False,
            include_voc=False,
            temperature_entity_id="sensor.room_sensor_a_temperature",
            occupancy_entity_id="binary_sensor.room_sensor_a_occupancy",
        )
        response = self.configuration.configuration_response(
            entry_id="entry-1",
            entry_data={
                "api_key": "not-returned",
                "api_base": "https://private.invalid/",
                "thermostats": [{"id": 1001, "name": "old value"}],
            },
            entry_options={
                "thermostats": [
                    {"id": 1001, "climate_entity_id": "climate.zone_a"}
                ],
            },
            config=self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(sensor,),
            ),
            point_lookback_days=45,
            scan_interval_seconds=21600,
        )

        self.assertEqual(
            response["timing"],
            {"point_lookback_days": 45, "scan_interval_seconds": 21600},
        )
        self.assertEqual(
            response["saved_overrides"]["thermostats"],
            {
                "source": "options",
                "items": [
                    {"id": 1001, "climate_entity_id": "climate.zone_a"}
                ],
            },
        )
        self.assertEqual(
            response["saved_overrides"]["sensors"],
            {"source": "automatic", "items": []},
        )
        self.assertEqual(
            response["effective_configuration"]["thermostats"][0][
                "filter_changed_date"
            ],
            "2026-07-01",
        )
        self.assertEqual(
            response["effective_configuration"]["sensors"][0][
                "occupancy_entity_id"
            ],
            "binary_sensor.room_sensor_a_occupancy",
        )
        serialized = repr(response)
        self.assertNotIn("not-returned", serialized)
        self.assertNotIn("private.invalid", serialized)


if __name__ == "__main__":
    unittest.main()
