"""Tests for native binary sensor helper logic."""

from __future__ import annotations

from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_binary_sensor_test"


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


class BinarySensorHelpersTest(unittest.TestCase):
    """Validate dependency-light binary sensor behavior."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
                "homeassistant",
                "homeassistant.components",
                "homeassistant.components.binary_sensor",
                "homeassistant.config_entries",
                "homeassistant.const",
                "homeassistant.core",
                "homeassistant.exceptions",
                "homeassistant.helpers",
                "homeassistant.helpers.device_registry",
                "homeassistant.helpers.entity",
                "homeassistant.helpers.entity_platform",
                "homeassistant.helpers.update_coordinator",
            )
        }
        self._install_fake_homeassistant_modules()
        _load_module("const")
        self.config_model = _load_module("config_model")
        _load_module("api")
        self.coordinator = _load_module("coordinator")
        _load_module("entity")
        _load_module("filter_forecast")
        _load_module("runtime")
        self.binary_sensor = _load_module("binary_sensor")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_binary_sensors_separate_advisory_and_problem_states(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            device_identifiers=(("homekit_controller", "thermostat-device"),),
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
        )
        sensor = self.config_model.ConfiguredSensor(
            sensor_id=10,
            slug="room_sensor_c",
            name="Room Sensor C",
            thermostat_id=1,
            thermostat_slug="main",
            include_temperature=True,
            include_air_quality=False,
            include_co2=False,
            include_voc=False,
            device_identifiers=(("homekit_controller", "sensor-device"),),
        )
        data = self.coordinator.BeestatRuntimeData(
            config=self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(sensor,),
            ),
            fetched_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
            sync_success_at=None,
            metadata_sync_success_at=None,
            summary_rows=(),
            summary_rows_full=True,
            summary_window_start=None,
            summary_window_end=None,
            thermostat_rows=(),
            sensor_rows=(),
            summary_row_count=0,
            thermostats={
                1: self.coordinator.ThermostatRuntimeSummary(
                    thermostat_id=1,
                    slug="main",
                    label="Main",
                    latest_date=date(2026, 7, 3),
                    lag_days=2,
                    filter_changed_date=date(2026, 6, 18),
                    filter_changed_source="native",
                    filter_runtime_hours=200,
                    recent_runtime_hours_per_day=10,
                )
            },
            thermostat_metadata={
                1: self.coordinator.ThermostatMetadata(
                    thermostat_id=1,
                    slug="main",
                    label="Main",
                    data_begin=None,
                    data_end=datetime(2026, 7, 5, 11, tzinfo=timezone.utc),
                    data_lag_minutes=180,
                    current_climate_ref=None,
                    current_climate_name=None,
                    scheduled_climate_ref=None,
                    scheduled_climate_name=None,
                    next_scheduled_climate_ref=None,
                    next_scheduled_climate_name=None,
                    next_scheduled_at=None,
                    schedule_profiles=(),
                    active_sensor_count=1,
                    active_sensor_names=("Room Sensor C",),
                    current_profile_sensor_names=("Room Sensor C",),
                    active_alert_count=1,
                    active_alerts=(
                        {
                            "code": "filter",
                            "type": "thermostat",
                            "severity": None,
                            "text": "Replace your filter",
                        },
                    ),
                )
            },
            sensor_metadata={
                10: self.coordinator.SensorMetadata(
                    sensor_id=10,
                    thermostat_id=1,
                    name="Room Sensor C",
                    identifier=None,
                    sensor_type="ecobee_remote_sensor",
                    in_use=True,
                    inactive=False,
                    deleted=False,
                )
            },
        )
        fake_coordinator = _FakeCoordinator(data)

        entities = self.binary_sensor._build_entities(fake_coordinator)
        by_key = {
            entity._attr_translation_key: entity
            for entity in entities
            if getattr(entity, "_attr_translation_key", None)
        }

        self.assertTrue(by_key["active_alert"].is_on)
        self.assertFalse(by_key["equipment_alert"].is_on)
        self.assertFalse(by_key["filter_due"].is_on)
        self.assertTrue(by_key["filter_due_soon"].is_on)
        self.assertTrue(by_key["runtime_summary_stale"].is_on)
        self.assertTrue(by_key["cloud_data_stale"].is_on)
        self.assertFalse(by_key["homekit_mapping_incomplete"].is_on)

    def _install_fake_homeassistant_modules(self) -> None:
        aiohttp = types.ModuleType("aiohttp")
        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
        config_entries = types.ModuleType("homeassistant.config_entries")
        const = types.ModuleType("homeassistant.const")
        core = types.ModuleType("homeassistant.core")
        exceptions = types.ModuleType("homeassistant.exceptions")
        helpers = types.ModuleType("homeassistant.helpers")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        entity = types.ModuleType("homeassistant.helpers.entity")
        entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
        update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        binary_sensor.BinarySensorDeviceClass = types.SimpleNamespace(PROBLEM="problem")
        binary_sensor.BinarySensorEntity = object
        config_entries.ConfigEntry = _Subscriptable
        const.UnitOfTime = types.SimpleNamespace(DAYS="d")
        core.HomeAssistant = object
        core.callback = lambda func: func
        exceptions.ConfigEntryAuthFailed = type(
            "ConfigEntryAuthFailed",
            (Exception,),
            {},
        )
        device_registry.DeviceEntryType = types.SimpleNamespace(SERVICE="service")
        device_registry.async_get = lambda _hass: types.SimpleNamespace(
            async_get_or_create=lambda **_kwargs: None
        )
        entity.DeviceInfo = lambda **kwargs: kwargs
        entity.Entity = object
        entity.EntityCategory = types.SimpleNamespace(
            CONFIG="config",
            DIAGNOSTIC="diagnostic",
        )
        entity_platform.AddConfigEntryEntitiesCallback = object
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
        update_coordinator.CoordinatorEntity = _FakeCoordinatorEntity

        components.binary_sensor = binary_sensor
        helpers.device_registry = device_registry
        helpers.entity = entity
        helpers.entity_platform = entity_platform
        helpers.update_coordinator = update_coordinator
        homeassistant.components = components
        homeassistant.config_entries = config_entries
        homeassistant.const = const
        homeassistant.core = core
        homeassistant.exceptions = exceptions
        homeassistant.helpers = helpers

        sys.modules["aiohttp"] = aiohttp
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.components"] = components
        sys.modules["homeassistant.components.binary_sensor"] = binary_sensor
        sys.modules["homeassistant.config_entries"] = config_entries
        sys.modules["homeassistant.const"] = const
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.exceptions"] = exceptions
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.entity"] = entity
        sys.modules["homeassistant.helpers.entity_platform"] = entity_platform
        sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


class _Subscriptable:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


class _FakeDataUpdateCoordinator(_Subscriptable):
    def __init__(self, *args, **kwargs) -> None:
        self.data = None


class _FakeCoordinator:
    def __init__(self, data) -> None:
        self.data = data
        self.local_tz = ZoneInfo("America/New_York")
        self.last_import_partial = False
        self.last_import_skipped_windows = 0
        self.last_import_skipped_runtime_thermostat_windows = 0
        self.last_import_skipped_runtime_sensor_windows = 0


class _FakeCoordinatorEntity(_Subscriptable):
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return True


if __name__ == "__main__":
    unittest.main()
