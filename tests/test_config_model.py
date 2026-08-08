"""Tests for HomeKit-first Beestat mapping."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_config_model_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"{name}.py"
    )
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
    id: str = ""
    unique_id: str = ""
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

    def async_get(self, entity_id_or_uuid: str) -> FakeEntityEntry | None:
        return next(
            (
                entry
                for entry in self.entities.values()
                if entity_id_or_uuid in (entry.entity_id, entry.id)
            ),
            None,
        )

    def async_get_entity_id(
        self,
        domain: str,
        platform: str,
        unique_id: str,
    ) -> str | None:
        return next(
            (
                entry.entity_id
                for entry in self.entities.values()
                if entry.entity_id.startswith(f"{domain}.")
                and entry.platform == platform
                and entry.unique_id == unique_id
            ),
            None,
        )


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
        self._install_fake_homeassistant_modules(devices={}, entries=[])

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
        self.assertEqual(thermostat.device_id, "thermostat_zone_a")

        self.assertEqual(len(config.sensors), 2)
        thermostat_sensor = _sensor(config.sensors, 2001)
        self.assertEqual(thermostat_sensor.slug, "zone_a")
        self.assertEqual(thermostat_sensor.name, "Zone A")
        self.assertEqual(
            thermostat_sensor.temperature_entity_id,
            "sensor.zone_a_current_temperature",
        )
        self.assertEqual(thermostat_sensor.device_id, "thermostat_zone_a")

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
        self.assertEqual(
            room_sensor.motion_entity_id, "binary_sensor.room_sensor_a_motion"
        )
        self.assertEqual(room_sensor.device_id, "sensor_room_sensor_a")
        self.assertTrue(room_sensor.include_temperature)

    def test_stable_override_survives_rename_and_registry_recreation(self) -> None:
        reference = {
            "registry_entry_id": "registry-old",
            "domain": "climate",
            "platform": "homekit_controller",
            "unique_id": "source-climate",
        }
        config_data = {
            "thermostats": [
                {
                    "id": 1001,
                    "climate_entity_id": "climate.zone_a",
                    "climate_entity_ref": reference,
                }
            ]
        }
        devices = {
            "thermostat_zone_a": FakeDeviceEntry(
                name="Zone A",
                identifiers=(("homekit_controller", "zone-a-device"),),
            )
        }

        for registry_id, entity_id in (
            ("registry-old", "climate.zone_a_renamed"),
            ("registry-restored", "climate.zone_a_restored"),
        ):
            with self.subTest(registry_id=registry_id):
                self._install_fake_homeassistant_modules(
                    devices=devices,
                    entries=[
                        FakeEntityEntry(
                            entity_id,
                            "thermostat_zone_a",
                            id=registry_id,
                            unique_id="source-climate",
                        ),
                        FakeEntityEntry(
                            f"sensor.{registry_id}_temperature",
                            "thermostat_zone_a",
                            id=f"{registry_id}-temperature",
                            unique_id=f"{registry_id}-temperature",
                            original_device_class="temperature",
                        ),
                    ],
                )

                config = config_model.build_beestat_config(
                    FakeHass({}),
                    thermostat_rows=({"id": 1001, "name": "Zone A"},),
                    sensor_rows=(),
                    config_data=config_data,
                )

                self.assertEqual(config.thermostats[0].climate_entity_id, entity_id)
                self.assertEqual(
                    config.thermostats[0].device_id,
                    "thermostat_zone_a",
                )

    def test_unresolved_stable_override_does_not_fall_back_to_name(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                )
            },
            entries=[
                FakeEntityEntry(
                    "climate.zone_a",
                    "thermostat_zone_a",
                    id="different-registry-entry",
                    unique_id="different-source",
                ),
                FakeEntityEntry(
                    "sensor.zone_a_temperature",
                    "thermostat_zone_a",
                    id="temperature-registry-entry",
                    unique_id="temperature-source",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "climate_entity_id": "climate.zone_a",
                        "climate_entity_ref": {
                            "registry_entry_id": "removed-registry-entry",
                            "domain": "climate",
                            "platform": "homekit_controller",
                            "unique_id": "removed-source",
                        },
                    }
                ]
            },
        )

        self.assertIsNone(config.thermostats[0].climate_entity_id)
        self.assertIsNone(config.thermostats[0].device_id)

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

    def test_disabled_resources_are_excluded_from_runtime_and_repairs(self) -> None:
        config_data = {
            "thermostats": [
                {
                    "id": 1001,
                    "enabled": False,
                    "climate_entity_id": "sensor.wrong_domain",
                }
            ],
            "sensors": [
                {
                    "id": 2002,
                    "enabled": False,
                    "temperature_entity_id": "binary_sensor.wrong_domain",
                }
            ],
        }

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=({"id": 2002, "name": "Room Sensor A"},),
            config_data=config_data,
        )

        self.assertEqual(config.thermostats, ())
        self.assertEqual(config.sensors, ())
        self.assertEqual(config_model.configured_override_entity_ids(config_data), ())
        self.assertEqual(
            config_model.configured_override_entity_domain_errors(config_data),
            (),
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
        self.assertIsNone(
            config.thermostats[0].filter_change_day_runtime_baseline_seconds
        )

    def test_thermostat_override_can_set_filter_change_day_runtime_baseline(
        self,
    ) -> None:
        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_change_day_runtime_baseline_seconds": "28800",
                    }
                ]
            },
        )

        self.assertEqual(
            config.thermostats[0].filter_change_day_runtime_baseline_seconds,
            28800,
        )

    def test_thermostat_override_parses_exact_filter_boundary_metadata(self) -> None:
        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_changed_at": "2026-07-05T21:48:00+00:00",
                        "filter_change_day_runtime_baseline_seconds": 28800,
                        "filter_change_boundary_reconciled_at": (
                            "2026-07-05T22:05:00+00:00"
                        ),
                        "filter_change_boundary_source_data_end": (
                            "2026-07-05T21:50:00+00:00"
                        ),
                    }
                ]
            },
        )

        thermostat = config.thermostats[0]
        self.assertEqual(
            thermostat.filter_changed_at.isoformat(),
            "2026-07-05T21:48:00+00:00",
        )
        self.assertEqual(
            thermostat.filter_change_boundary_reconciled_at.isoformat(),
            "2026-07-05T22:05:00+00:00",
        )
        self.assertEqual(
            thermostat.filter_change_boundary_source_data_end.isoformat(),
            "2026-07-05T21:50:00+00:00",
        )

    def test_negative_filter_change_day_runtime_baseline_is_ignored(self) -> None:
        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_change_day_runtime_baseline_seconds": -1,
                    }
                ]
            },
        )

        self.assertIsNone(
            config.thermostats[0].filter_change_day_runtime_baseline_seconds
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

    def test_nonfinite_filter_values_fall_back_safely(self) -> None:
        """Non-finite persisted values must not poison filter projections."""

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_change_day_runtime_baseline_seconds": "Infinity",
                        "filter_lifetime_runtime_hours": "NaN",
                        "filter_max_age_days": "Infinity",
                        "filter_notice_days": float("nan"),
                    }
                ]
            },
        )

        thermostat = config.thermostats[0]
        self.assertIsNone(thermostat.filter_change_day_runtime_baseline_seconds)
        self.assertEqual(thermostat.filter_lifetime_runtime_hours, 250.0)
        self.assertEqual(thermostat.filter_max_age_days, 90)
        self.assertEqual(thermostat.filter_notice_days, 7)

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
        self.assertEqual(
            room_sensor.temperature_entity_id, "sensor.room_sensor_a_temperature"
        )
        self.assertEqual(
            room_sensor.motion_entity_id, "binary_sensor.room_sensor_a_motion"
        )

    def test_maps_ecobee_shaped_homekit_devices_when_manufacturer_is_missing(
        self,
    ) -> None:
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
        self.assertIsNone(config.thermostats[0].device_id)

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
        self.assertEqual(config.thermostats[0].device_id, "thermostat_homekit")

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
        self.assertEqual(room_sensor.device_id, "sensor_ecobee_room_sensor_c")
        self.assertEqual(
            room_sensor.temperature_entity_id,
            "sensor.ecobee_room_sensor_c_temperature",
        )

    def test_ambiguous_weak_homekit_name_matches_do_not_map_by_registry_order(
        self,
    ) -> None:
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
        self.assertIsNone(room_sensor.device_id)

    def test_multiple_thermostats_do_not_share_single_strong_automatic_match(
        self,
    ) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_temperature",
                    "thermostat_zone_a",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(
                {"id": 1001, "name": "Unknown One"},
                {"id": 1002, "name": "Unknown Two"},
            ),
            sensor_rows=(),
            config_data={},
        )

        self.assertTrue(
            all(thermostat.device_id is None for thermostat in config.thermostats)
        )
        self.assertTrue(
            all(
                thermostat.climate_entity_id is None
                for thermostat in config.thermostats
            )
        )

    def test_named_thermostat_match_wins_over_competing_strong_fallback(
        self,
    ) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_temperature",
                    "thermostat_zone_a",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(
                {"id": 1001, "name": "Zone A"},
                {"id": 1002, "name": "Unknown Two"},
            ),
            sensor_rows=(),
            config_data={},
        )

        thermostat_by_id = {
            thermostat.thermostat_id: thermostat for thermostat in config.thermostats
        }
        self.assertEqual(thermostat_by_id[1001].device_id, "thermostat_zone_a")
        self.assertIsNone(thermostat_by_id[1002].device_id)

    def test_duplicate_beestat_sensor_names_do_not_share_automatic_match(
        self,
    ) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "sensor_room_a": FakeDeviceEntry(
                    name="Room A",
                    identifiers=(("homekit_controller", "room-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry(
                    "sensor.room_a_temperature",
                    "sensor_room_a",
                    original_device_class="temperature",
                ),
                FakeEntityEntry(
                    "binary_sensor.room_a_occupancy",
                    "sensor_room_a",
                    original_device_class="occupancy",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(),
            sensor_rows=(
                {"id": 2001, "name": "Room A", "temperature": 70},
                {"id": 2002, "name": "Room A", "temperature": 71},
            ),
            config_data={},
        )

        self.assertTrue(all(sensor.device_id is None for sensor in config.sensors))
        self.assertTrue(
            all(sensor.temperature_entity_id is None for sensor in config.sensors)
        )

    def test_explicit_mapping_reserves_device_from_automatic_match(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_a_temperature",
                    "thermostat_zone_a",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(
                {"id": 1001, "name": "Explicit Zone"},
                {"id": 1002, "name": "Zone A"},
            ),
            sensor_rows=(),
            config_data={
                "thermostats": [{"id": 1001, "climate_entity_id": "climate.zone_a"}]
            },
        )

        thermostat_by_id = {
            thermostat.thermostat_id: thermostat for thermostat in config.thermostats
        }
        self.assertEqual(thermostat_by_id[1001].device_id, "thermostat_zone_a")
        self.assertIsNone(thermostat_by_id[1002].device_id)

    def test_explicit_mapping_across_devices_fails_device_linking_closed(self) -> None:
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
                "thermostat_zone_b": FakeDeviceEntry(
                    name="Zone B",
                    identifiers=(("homekit_controller", "zone-b-device"),),
                ),
            },
            entries=[
                FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
                FakeEntityEntry(
                    "sensor.zone_b_temperature",
                    "thermostat_zone_b",
                    original_device_class="temperature",
                ),
            ],
        )

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=({"id": 1001, "name": "Zone A"},),
            sensor_rows=(),
            config_data={
                "thermostats": [
                    {
                        "id": 1001,
                        "climate_entity_id": "climate.zone_a",
                        "temperature_entity_id": "sensor.zone_b_temperature",
                    }
                ]
            },
        )

        thermostat = config.thermostats[0]
        self.assertIsNone(thermostat.device_id)
        self.assertEqual(thermostat.climate_entity_id, "climate.zone_a")
        self.assertEqual(
            thermostat.temperature_entity_id,
            "sensor.zone_b_temperature",
        )

    def test_duplicate_explicit_device_claims_fail_linking_closed(self) -> None:
        entries = [
            FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
            FakeEntityEntry("climate.zone_a_secondary", "thermostat_zone_a"),
        ]
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
            },
            entries=entries,
        )

        config_data = {
            "thermostats": [
                {"id": 1001, "climate_entity_id": "climate.zone_a"},
                {
                    "id": 1002,
                    "climate_entity_id": "climate.zone_a_secondary",
                },
            ]
        }
        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(
                {"id": 1001, "name": "First Zone"},
                {"id": 1002, "name": "Second Zone"},
            ),
            sensor_rows=(),
            config_data=config_data,
        )

        self.assertTrue(all(item.device_id is None for item in config.thermostats))
        conflicts = config_model.configured_mapping_device_conflicts(
            config_data,
            FakeEntityRegistry(entries),
        )
        self.assertEqual(
            conflicts,
            (
                config_model.MappingDeviceConflict(
                    resource_type="thermostat",
                    resource_ids=(1001, 1002),
                    reason="duplicate_device",
                ),
            ),
        )

    def test_reports_cross_device_explicit_mapping_conflict(self) -> None:
        entries = [
            FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
            FakeEntityEntry(
                "sensor.zone_b_temperature",
                "thermostat_zone_b",
                original_device_class="temperature",
            ),
        ]

        conflicts = config_model.configured_mapping_device_conflicts(
            {
                "thermostats": [
                    {
                        "id": 1001,
                        "climate_entity_id": "climate.zone_a",
                        "temperature_entity_id": "sensor.zone_b_temperature",
                    }
                ]
            },
            FakeEntityRegistry(entries),
        )

        self.assertEqual(
            conflicts,
            (
                config_model.MappingDeviceConflict(
                    resource_type="thermostat",
                    resource_ids=(1001,),
                    reason="cross_device",
                ),
            ),
        )

    def test_cross_device_mapping_participates_in_duplicate_claims(self) -> None:
        entries = [
            FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
            FakeEntityEntry(
                "sensor.zone_b_temperature",
                "thermostat_zone_b",
                original_device_class="temperature",
            ),
            FakeEntityEntry("climate.zone_a_secondary", "thermostat_zone_a"),
        ]
        self._install_fake_homeassistant_modules(
            devices={
                "thermostat_zone_a": FakeDeviceEntry(
                    name="Zone A",
                    identifiers=(("homekit_controller", "zone-a-device"),),
                ),
                "thermostat_zone_b": FakeDeviceEntry(
                    name="Zone B",
                    identifiers=(("homekit_controller", "zone-b-device"),),
                ),
            },
            entries=entries,
        )
        config_data = {
            "thermostats": [
                {
                    "id": 1001,
                    "climate_entity_id": "climate.zone_a",
                    "temperature_entity_id": "sensor.zone_b_temperature",
                },
                {
                    "id": 1002,
                    "climate_entity_id": "climate.zone_a_secondary",
                },
            ]
        }

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(
                {"id": 1001, "name": "First Zone"},
                {"id": 1002, "name": "Second Zone"},
            ),
            sensor_rows=(),
            config_data=config_data,
        )

        self.assertTrue(all(item.device_id is None for item in config.thermostats))
        self.assertEqual(
            config_model.configured_mapping_device_conflicts(
                config_data,
                FakeEntityRegistry(entries),
            ),
            (
                config_model.MappingDeviceConflict(
                    resource_type="thermostat",
                    resource_ids=(1001,),
                    reason="cross_device",
                ),
                config_model.MappingDeviceConflict(
                    resource_type="thermostat",
                    resource_ids=(1001, 1002),
                    reason="duplicate_device",
                ),
            ),
        )

    def test_repeated_override_id_uses_effective_last_row_for_conflicts(self) -> None:
        entries = [
            FakeEntityEntry("climate.zone_a", "thermostat_zone_a"),
            FakeEntityEntry("climate.zone_b", "thermostat_zone_b"),
        ]

        conflicts = config_model.configured_mapping_device_conflicts(
            {
                "thermostats": [
                    {"id": 1001, "climate_entity_id": "climate.zone_a"},
                    {"id": 1001, "climate_entity_id": "climate.zone_b"},
                ]
            },
            FakeEntityRegistry(entries),
        )

        self.assertEqual(conflicts, ())

    def test_repeated_override_id_uses_effective_last_row_for_repairs(self) -> None:
        config_data = {
            "thermostats": [
                {
                    "id": 1001,
                    "climate_entity_id": "sensor.shadowed_wrong_domain",
                },
                {
                    "id": 1001,
                    "climate_entity_id": "climate.effective",
                },
            ]
        }

        self.assertEqual(
            config_model.configured_override_entity_ids(config_data),
            ("climate.effective",),
        )
        self.assertEqual(
            config_model.configured_override_entity_domain_errors(config_data),
            (),
        )

    def test_duplicate_explicit_sensor_device_claims_fail_linking_closed(self) -> None:
        entries = [
            FakeEntityEntry(
                "sensor.room_a_temperature",
                "sensor_room_a",
                original_device_class="temperature",
            ),
            FakeEntityEntry(
                "sensor.room_a_temperature_secondary",
                "sensor_room_a",
                original_device_class="temperature",
            ),
        ]
        self._install_fake_homeassistant_modules(
            devices={
                "sensor_room_a": FakeDeviceEntry(
                    name="Room A",
                    identifiers=(("homekit_controller", "room-a-device"),),
                )
            },
            entries=entries,
        )
        config_data = {
            "sensors": [
                {
                    "id": 2001,
                    "temperature_entity_id": "sensor.room_a_temperature",
                },
                {
                    "id": 2002,
                    "temperature_entity_id": "sensor.room_a_temperature_secondary",
                },
            ]
        }

        config = config_model.build_beestat_config(
            FakeHass({}),
            thermostat_rows=(),
            sensor_rows=(
                {"id": 2001, "name": "Room A", "temperature": 70},
                {"id": 2002, "name": "Room B", "temperature": 71},
            ),
            config_data=config_data,
        )

        self.assertTrue(all(item.device_id is None for item in config.sensors))
        self.assertEqual(
            config_model.configured_mapping_device_conflicts(
                config_data,
                FakeEntityRegistry(entries),
            ),
            (
                config_model.MappingDeviceConflict(
                    resource_type="sensor",
                    resource_ids=(2001, 2002),
                    reason="duplicate_device",
                ),
            ),
        )

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
                (
                    "thermostat 1 climate_entity_id: "
                    "sensor.zone_a_temperature (expected climate)"
                ),
                (
                    "thermostat 1 filter_changed_entity_id: "
                    "sensor.filter_changed (expected input_datetime)"
                ),
                (
                    "sensor 2 temperature_entity_id: "
                    "binary_sensor.guest_motion (expected sensor)"
                ),
                (
                    "sensor 2 motion_entity_id: "
                    "sensor.guest_temperature (expected binary_sensor)"
                ),
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
