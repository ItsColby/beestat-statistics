"""Tests for native sensor helper logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_sensor_test"


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


@dataclass(frozen=True, kw_only=True)
class FakeSensorEntityDescription:
    key: str
    name: str | None = None
    translation_key: str | None = None
    device_class: object | None = None
    native_unit_of_measurement: str | None = None
    state_class: object | None = None
    entity_category: object | None = None
    entity_registry_enabled_default: bool = True


class SensorHelpersTest(unittest.TestCase):
    """Validate dependency-light sensor helper behavior."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
                "homeassistant",
                "homeassistant.components",
                "homeassistant.components.sensor",
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
        _load_module("coordinator")
        _load_module("entity")
        _load_module("runtime")
        self.sensor = _load_module("sensor")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_mapping_summary_counts_homekit_backed_and_fallback_devices(self) -> None:
        data = types.SimpleNamespace(
            config=self.config_model.BeestatConfig(
                thermostats=(
                    self.config_model.ConfiguredThermostat(
                        thermostat_id=1,
                        slug="mapped",
                        name="Mapped",
                        device_identifiers=(("homekit_controller", "thermostat"),),
                    ),
                    self.config_model.ConfiguredThermostat(
                        thermostat_id=2,
                        slug="fallback",
                        name="Fallback",
                    ),
                ),
                sensors=(
                    self.config_model.ConfiguredSensor(
                        sensor_id=10,
                        slug="mapped_room",
                        name="Mapped Room",
                        thermostat_id=1,
                        thermostat_slug="mapped",
                        include_temperature=True,
                        include_air_quality=False,
                        include_co2=False,
                        include_voc=False,
                        device_connections=(("mac", "00:11:22:33:44:55"),),
                    ),
                    self.config_model.ConfiguredSensor(
                        sensor_id=11,
                        slug="fallback_room",
                        name="Fallback Room",
                        thermostat_id=2,
                        thermostat_slug="fallback",
                        include_temperature=True,
                        include_air_quality=False,
                        include_co2=False,
                        include_voc=False,
                    ),
                ),
            )
        )

        self.assertEqual(
            self.sensor._mapping_summary_attributes(data),
            {
                "thermostat_count": 2,
                "mapped_thermostat_count": 1,
                "unmapped_thermostat_count": 1,
                "local_thermostat_count": 0,
                "room_sensor_count": 2,
                "mapped_room_sensor_count": 1,
                "unmapped_room_sensor_count": 1,
                "local_room_sensor_count": 0,
            },
        )

    def test_mapping_summary_uses_none_when_runtime_data_is_not_ready(self) -> None:
        self.assertEqual(
            self.sensor._mapping_summary_attributes(None),
            {
                "thermostat_count": None,
                "mapped_thermostat_count": None,
                "unmapped_thermostat_count": None,
                "local_thermostat_count": None,
                "room_sensor_count": None,
                "mapped_room_sensor_count": None,
                "unmapped_room_sensor_count": None,
                "local_room_sensor_count": None,
            },
        )

    def test_filter_forecast_uses_runtime_and_max_age_thresholds(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 6, 18),
            filter_changed_source="native",
            filter_runtime_hours=200,
            recent_runtime_hours_per_day=10,
        )

        forecast = self.sensor.build_filter_forecast(
            thermostat,
            summary,
            today=date(2026, 7, 5),
        )

        self.assertEqual(forecast.remaining_runtime_hours, 50.0)
        self.assertEqual(forecast.runtime_due_date, date(2026, 7, 10))
        self.assertEqual(forecast.max_age_due_date, date(2026, 9, 16))
        self.assertEqual(forecast.due_date, date(2026, 7, 10))
        self.assertEqual(forecast.days_remaining, 5)
        self.assertFalse(forecast.due)
        self.assertTrue(forecast.due_soon)

    def test_filter_forecast_resets_runtime_on_replacement_date(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 7, 5),
            filter_changed_source="native",
            filter_runtime_hours=276.6,
            recent_runtime_hours_per_day=15.4,
        )

        forecast = self.sensor.build_filter_forecast(
            thermostat,
            summary,
            today=date(2026, 7, 5),
        )

        self.assertEqual(forecast.runtime_hours, 0.0)
        self.assertEqual(forecast.remaining_runtime_hours, 250.0)
        self.assertEqual(forecast.runtime_due_date, date(2026, 7, 21))
        self.assertFalse(forecast.due)
        self.assertFalse(forecast.due_soon)

    def test_active_alert_category_separates_maintenance_from_equipment(self) -> None:
        self.assertEqual(
            self.sensor._classify_active_alerts(
                (
                    {
                        "code": "3140",
                        "type": "thermostat",
                        "text": "It is time to have your HVAC system inspected.",
                    },
                )
            ),
            "maintenance",
        )
        self.assertEqual(
            self.sensor._classify_active_alerts(
                ({"text": "System fault: not cooling"},)
            ),
            "equipment",
        )
        self.assertEqual(self.sensor._classify_active_alerts(()), "none")

    def _install_fake_homeassistant_modules(self) -> None:
        aiohttp = types.ModuleType("aiohttp")
        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        sensor = types.ModuleType("homeassistant.components.sensor")
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
        config_entries.ConfigEntry = _Subscriptable
        const.UnitOfTime = types.SimpleNamespace(
            DAYS="d",
            HOURS="h",
            MINUTES="min",
        )
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
        entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
        entity_platform.AddConfigEntryEntitiesCallback = object
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
        update_coordinator.CoordinatorEntity = _FakeCoordinatorEntity
        sensor.SensorDeviceClass = types.SimpleNamespace(
            DATE="date",
            DURATION="duration",
            TIMESTAMP="timestamp",
        )
        sensor.SensorEntity = object
        sensor.SensorEntityDescription = FakeSensorEntityDescription
        sensor.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")

        components.sensor = sensor
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
        sys.modules["homeassistant.components.sensor"] = sensor
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


class _FakeCoordinatorEntity(_Subscriptable):
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return True


if __name__ == "__main__":
    unittest.main()
