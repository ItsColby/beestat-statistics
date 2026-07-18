"""Tests for HomeKit-first Beestat mapping."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_config_model_test"


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


_load_module("const")
config_model = _load_module("config_model")


@dataclass
class FakeEntityEntry:
    entity_id: str
    device_id: str
    platform: str = "homekit_controller"
    disabled_by: str | None = None
    original_device_class: object | None = None
    device_class: object | None = None


@dataclass(frozen=True)
class FakeDeviceClass:
    value: str


@dataclass
class FakeDeviceEntry:
    name: str
    manufacturer: str = "Ecobee"
    model: str | None = None
    model_id: str | None = None
    disabled_by: str | None = None
    identifiers: tuple[tuple[str, str], ...] = ()
    connections: tuple[tuple[str, str], ...] = ()
    name_by_user: str | None = None
    default_name: str | None = None


class FakeEntityRegistry:
    def __init__(self, entries: list[FakeEntityEntry]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}


class FakeDeviceRegistry:
    def __init__(self, devices: dict[str, FakeDeviceEntry]) -> None:
        self._devices = devices

    def async_get(self, device_id: str) -> FakeDeviceEntry | None:
        return self._devices.get(device_id)


class FakeState:
    def __init__(self, friendly_name: str) -> None:
        self.attributes = {"friendly_name": friendly_name}


class FakeStates:
    def __init__(self, friendly_names: dict[str, str]) -> None:
        self._friendly_names = friendly_names

    def get(self, entity_id: str) -> FakeState | None:
        if entity_id not in self._friendly_names:
            return None
        return FakeState(self._friendly_names[entity_id])


class FakeHass:
    def __init__(self, friendly_names: dict[str, str]) -> None:
        self.states = FakeStates(friendly_names)


class ConfigModelTest(unittest.TestCase):
    """Validate generic mapping from HA HomeKit devices to Beestat rows."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "homeassistant",
                "homeassistant.helpers",
                "homeassistant.helpers.device_registry",
                "homeassistant.helpers.entity_registry",
            )
        }

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_maps_beestat_rows_to_homekit_devices_by_name(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
                "sensor_room_sensor_a": FakeDeviceEntry(
                    name="Ecobee Room Sensor A Temperature",
                    identifiers=(("homekit_controller", "room-sensor-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_current_temperature",
                    "thermostat_zone_a",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.zone_a_occupancy",
                    "thermostat_zone_a",
                    original_device_class="occupancy",
                ),
                FakeEntityEntry(
                    "binary_sensor.zone_a_motion",
                    "thermostat_zone_a",
                    original_device_class="motion",
                ),
                FakeEntityEntry(
                    "sensor.room_sensor_a_temperature",
                    "sensor_room_sensor_a",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.room_sensor_a_occupancy",
                    "sensor_room_sensor_a",
                    original_device_class="occupancy",
                ),
                FakeEntityEntry(
                    "binary_sensor.room_sensor_a_motion",
                    "sensor_room_sensor_a",
                    device_class="motion",
                ),
            ],
        )
        hass = FakeHass(
            {
                "sensor.room_sensor_a_temperature": "Ecobee Room Sensor A Temperature",
            }
        )

        config = config_model.build_beestat_config(
            hass,
            thermostat_rows=(
                {
                    "id": 1001,
                    "name": "Zone A",
                },
            ),
            sensor_rows=(
                {
                    "id": 2001,
                    "thermostat_id": 1001,
                    "name": "Zone A",
                    "type": "thermostat",
                    "capability": [{"type": "temperature"}],
                },
                {
                    "id": 2002,
                    "thermostat_id": 1001,
                    "name": "Room Sensor A",
                    "capability": [{"type": "temperature"}],
                },
            ),
            config_data={},
        )

        self.assertEqual(len(config.thermostats), 1)
        thermostat = config.thermostats[0]
        self.assertEqual(thermostat.slug, "zone_a")
        self.assertEqual(thermostat.climate_entity_id, "climate.zone_a")
        self.assertEqual(
            thermostat.temperature_entity_id,
            "sensor.zone_a_current_temperature",
        )
        self.assertEqual(
            thermostat.device_identifiers,
            (("homekit_controller", "zone-a-device"),),
        )

        self.assertEqual(len(config.sensors), 2)
        thermostat_sensor = _sensor(config.sensors, 2001)
        self.assertEqual(thermostat_sensor.slug, "zone_a")
        self.assertEqual(thermostat_sensor.name, "Zone A")
        self.assertEqual(
            thermostat_sensor.temperature_entity_id,
            "sensor.zone_a_current_temperature",
        )
        self.assertEqual(
            thermostat_sensor.device_identifiers,
            (("homekit_controller", "zone-a-device"),),
        )

        room_sensor = _sensor(config.sensors, 2002)
        self.assertEqual(room_sensor.slug, "room_sensor_a")
        self.assertEqual(room_sensor.name, "Room Sensor A")
        self.assertEqual(
            room_sensor.temperature_entity_id,
            "sensor.room_sensor_a_temperature",
        )
        self.assertEqual(
            room_sensor.occupancy_entity_id,
            "binary_sensor.room_sensor_a_occupancy",
        )
        self.assertEqual(room_sensor.motion_entity_id, "binary_sensor.room_sensor_a_motion")
        self.assertTrue(room_sensor.include_temperature)

    def test_reports_explicit_override_entity_references(self) -> None:
        references = config_model.configured_override_entity_ids(
            {
                "thermostats": [
                    {
                        "id": 1,
                        "climate_entity_id": "climate.zone_a",
                        "temperature_entity_id": "sensor.zone_a_temperature",
                        "occupancy_entity_id": "binary_sensor.zone_a_occupancy",
                        "motion_entity_id": "binary_sensor.zone_a_motion",
                        "filter_changed_entity_id": (
                            "input_datetime.zone_a_filter_changed"
                        ),
                    }
                ],
                "sensors": [
                    {
                        "id": 2,
                        "temperature_entity_id": "sensor.room_sensor_b_temperature",
                        "occupancy_entity_id": "binary_sensor.room_sensor_b_occupancy",
                        "motion_entity_id": "binary_sensor.room_sensor_b_motion",
                    },
                    {
                        "id": 3,
                        "temperature_entity_id": "sensor.room_sensor_b_temperature",
                    },
                ],
            }
        )

        self.assertEqual(
            references,
            (
                "climate.zone_a",
                "sensor.zone_a_temperature",
                "binary_sensor.zone_a_occupancy",
                "binary_sensor.zone_a_motion",
                "input_datetime.zone_a_filter_changed",
                "sensor.room_sensor_b_temperature",
                "binary_sensor.room_sensor_b_occupancy",
                "binary_sensor.room_sensor_b_motion",
            ),
        )

    def test_thermostat_override_can_set_native_filter_changed_date(self) -> None:
        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                    }
                ]
            },
        )

        self.assertEqual(
            config.thermostats[0].filter_changed_date.isoformat(),
            "2026-07-05",
        )

    def test_thermostat_filter_forecast_options_default_and_override(self) -> None:
        default_config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={},
        )

        self.assertEqual(
            default_config.thermostats[0].filter_lifetime_runtime_hours,
            250.0,
        )
        self.assertEqual(default_config.thermostats[0].filter_max_age_days, 90)
        self.assertEqual(default_config.thermostats[0].filter_notice_days, 7)

        override_config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_lifetime_runtime_hours": "300",
                        "filter_max_age_days": "120",
                        "filter_notice_days": "14",
                    }
                ]
            },
        )

        thermostat = override_config.thermostats[0]
        self.assertEqual(thermostat.filter_lifetime_runtime_hours, 300.0)
        self.assertEqual(thermostat.filter_max_age_days, 120)
        self.assertEqual(thermostat.filter_notice_days, 14)

    def test_maps_homekit_entities_with_enum_like_device_classes(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
                "sensor_room_sensor_a": FakeDeviceEntry(
                    name="Room Sensor A",
                    identifiers=(("homekit_controller", "room-sensor-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_current_temperature",
                    "thermostat_zone_a",
                    original_device_class=FakeDeviceClass("temperature"),
                ),
                FakeEntityEntry(
                    "binary_sensor.zone_a_occupancy",
                    "thermostat_zone_a",
                    original_device_class=FakeDeviceClass("occupancy"),
                ),
                FakeEntityEntry(
                    "sensor.room_sensor_a_temperature",
                    "sensor_room_sensor_a",
                    original_device_class=FakeDeviceClass("temperature"),
                ),
                FakeEntityEntry(
                    "binary_sensor.room_sensor_a_motion",
                    "sensor_room_sensor_a",
                    device_class=FakeDeviceClass("motion"),
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(
                {
                    "id": 2002,
                    "thermostat_id": 1001,
                    "name": "Room Sensor A",
                    "capability": [{"type": "temperature"}],
                },
            ),
            config_data={},
        )

        thermostat = config.thermostats[0]
        self.assertEqual(
            thermostat.temperature_entity_id,
            "sensor.zone_a_current_temperature",
        )
        self.assertEqual(
            thermostat.occupancy_entity_id,
            "binary_sensor.zone_a_occupancy",
        )
        room_sensor = _sensor(config.sensors, 2002)
        self.assertEqual(room_sensor.temperature_entity_id, "sensor.room_sensor_a_temperature")
        self.assertEqual(room_sensor.motion_entity_id, "binary_sensor.room_sensor_a_motion")

    def test_maps_ecobee_shaped_homekit_devices_when_manufacturer_is_missing(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    manufacturer="",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
                "sensor_room_sensor_a": FakeDeviceEntry(
                    name="Room Sensor A",
                    manufacturer="",
                    identifiers=(("homekit_controller", "room-sensor-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_temperature",
                    "thermostat_zone_a",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "sensor.room_sensor_a_temperature",
                    "sensor_room_sensor_a",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.room_sensor_a_occupancy",
                    "sensor_room_sensor_a",
                    original_device_class="occupancy",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(
                {
                    "id": 2002,
                    "thermostat_id": 1001,
                    "name": "Room Sensor A",
                    "capability": [{"type": "temperature"}],
                },
            ),
            config_data={},
        )

        self.assertEqual(config.thermostats[0].climate_entity_id, "climate.zone_a")
        self.assertEqual(
            _sensor(config.sensors, 2002).temperature_entity_id,
            "sensor.room_sensor_a_temperature",
        )

    def test_weak_homekit_thermostat_candidate_does_not_single_fallback(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_other_zone": FakeDeviceEntry(
                    name="Other Zone",
                    manufacturer="",
                    identifiers=(("homekit_controller", "other-zone-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.other_zone", "thermostat_other_zone"),
                FakeEntityEntry(
                    "sensor.other_zone_temperature",
                    "thermostat_other_zone",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={},
        )

        self.assertEqual(config.thermostats[0].name, "Zone A")
        self.assertIsNone(config.thermostats[0].climate_entity_id)
        self.assertEqual(config.thermostats[0].device_identifiers, ())

    def test_single_fallback_accepts_ecobee_signal_from_device_name(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_homekit": FakeDeviceEntry(
                    name="Ecobee HomeKit Thermostat",
                    manufacturer="",
                    identifiers=(("homekit_controller", "thermostat-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.homekit_thermostat", "thermostat_homekit"),
                FakeEntityEntry(
                    "sensor.homekit_thermostat_temperature",
                    "thermostat_homekit",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={},
        )

        self.assertEqual(
            config.thermostats[0].climate_entity_id,
            "climate.homekit_thermostat",
        )
        self.assertEqual(
            config.thermostats[0].device_identifiers,
            (("homekit_controller", "thermostat-device"),),
        )

    def test_name_matching_prefers_strong_ecobee_signal_over_weak_shape(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "sensor_weak_room_sensor_c": FakeDeviceEntry(
                    name="Room Sensor C",
                    manufacturer="",
                    identifiers=(("homekit_controller", "weak-room_sensor_c"),),
                ),
                "sensor_ecobee_room_sensor_c": FakeDeviceEntry(
                    name="Room Sensor C",
                    identifiers=(("homekit_controller", "ecobee-room_sensor_c"),),
                ),
            },
            entries=[
                FakeEntityEntry(
                    "sensor.room_sensor_c_temperature",
                    "sensor_weak_room_sensor_c",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.room_sensor_c_occupancy",
                    "sensor_weak_room_sensor_c",
                    original_device_class="occupancy",
                ),
                FakeEntityEntry(
                    "sensor.ecobee_room_sensor_c_temperature",
                    "sensor_ecobee_room_sensor_c",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(),
            sensor_rows=(
                {
                    "id": 2002,
                    "thermostat_id": 1001,
                    "name": "Room Sensor C",
                    "capability": [{"type": "temperature"}],
                },
            ),
            config_data={},
        )

        room_sensor = _sensor(config.sensors, 2002)
        self.assertEqual(
            room_sensor.device_identifiers,
            (("homekit_controller", "ecobee-room_sensor_c"),),
        )
        self.assertEqual(
            room_sensor.temperature_entity_id,
            "sensor.ecobee_room_sensor_c_temperature",
        )

    def test_ambiguous_weak_homekit_name_matches_do_not_map_by_registry_order(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "sensor_first_room_sensor_c": FakeDeviceEntry(
                    name="Room Sensor C",
                    manufacturer="",
                    identifiers=(("homekit_controller", "first-room_sensor_c"),),
                ),
                "sensor_second_room_sensor_c": FakeDeviceEntry(
                    name="Room Sensor C",
                    manufacturer="",
                    identifiers=(("homekit_controller", "second-room_sensor_c"),),
                ),
            },
            entries=[
                FakeEntityEntry(
                    "sensor.first_room_sensor_c_temperature",
                    "sensor_first_room_sensor_c",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.first_room_sensor_c_occupancy",
                    "sensor_first_room_sensor_c",
                    original_device_class="occupancy",
                ),
                FakeEntityEntry(
                    "sensor.second_room_sensor_c_temperature",
                    "sensor_second_room_sensor_c",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.second_room_sensor_c_occupancy",
                    "sensor_second_room_sensor_c",
                    original_device_class="occupancy",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(),
            sensor_rows=(
                {
                    "id": 2002,
                    "thermostat_id": 1001,
                    "name": "Room Sensor C",
                    "capability": [{"type": "temperature"}],
                },
            ),
            config_data={},
        )

        room_sensor = _sensor(config.sensors, 2002)
        self.assertIsNone(room_sensor.temperature_entity_id)
        self.assertEqual(room_sensor.device_identifiers, ())

    def test_reports_override_entity_domain_errors(self) -> None:
        errors = config_model.configured_override_entity_domain_errors(
            {
                "thermostats": [
                    {
                        "id": 1,
                        "climate_entity_id": "sensor.zone_a_temperature",
                        "filter_changed_entity_id": "sensor.filter_changed",
                    }
                ],
                "sensors": [
                    {
                        "id": 2,
                        "temperature_entity_id": "binary_sensor.guest_motion",
                        "motion_entity_id": "sensor.guest_temperature",
                    }
                ],
            }
        )

        self.assertEqual(
            errors,
            (
                "thermostat 1 climate_entity_id: "
                "sensor.zone_a_temperature (expected climate)",
                "thermostat 1 filter_changed_entity_id: "
                "sensor.filter_changed (expected input_datetime)",
                "sensor 2 temperature_entity_id: "
                "binary_sensor.guest_motion (expected sensor)",
                "sensor 2 motion_entity_id: "
                "sensor.guest_temperature (expected binary_sensor)",
            ),
        )

    def _install_fake_homeassistant_modules(
        self,
        *,
        devices: dict[str, FakeDeviceEntry],
        entries: list[FakeEntityEntry],
    ) -> None:
        homeassistant = types.ModuleType("homeassistant")
        helpers = types.ModuleType("homeassistant.helpers")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

        fake_device_registry = FakeDeviceRegistry(devices)
        fake_entity_registry = FakeEntityRegistry(entries)
        device_registry.async_get = lambda _hass: fake_device_registry
        entity_registry.async_get = lambda _hass: fake_entity_registry

        helpers.device_registry = device_registry
        helpers.entity_registry = entity_registry
        homeassistant.helpers = helpers
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.entity_registry"] = entity_registry


def _sensor(sensors, sensor_id: int):
    for sensor in sensors:
        if sensor.sensor_id == sensor_id:
            return sensor
    raise AssertionError(f"Missing sensor {sensor_id}")


if __name__ == "__main__":
    unittest.main()
