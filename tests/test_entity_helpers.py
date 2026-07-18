"""Tests for shared Beestat entity helpers."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_entity_test"


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


@dataclass
class FakeEntity:
    unique_id: str | None


class FakeCoordinator:
    def __init__(self) -> None:
        self.entities = [FakeEntity("one"), FakeEntity("two")]
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None


class EntityHelpersTest(unittest.TestCase):
    """Validate shared Home Assistant entity helpers with lightweight stubs."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "homeassistant",
                "homeassistant.helpers",
                "homeassistant.helpers.device_registry",
                "homeassistant.helpers.entity",
                "homeassistant.helpers.entity_platform",
            )
        }
        self._install_fake_homeassistant_modules()
        _load_module("const")
        self.config_model = _load_module("config_model")
        self.entity = _load_module("entity")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_dynamic_entity_adds_only_new_unique_ids(self) -> None:
        coordinator = FakeCoordinator()
        added: list[str | None] = []

        self.entity.async_add_new_entities(
            coordinator,
            lambda entities: added.extend(entity.unique_id for entity in entities),
            lambda item: item.entities,
            lambda remove: None,
        )
        self.assertEqual(added, ["one", "two"])
        self.assertEqual(len(coordinator.listeners), 1)

        coordinator.entities = [FakeEntity("two"), FakeEntity("three")]
        coordinator.listeners[0]()

        self.assertEqual(added, ["one", "two", "three"])

    def test_fallback_devices_are_via_beestat_service(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
        )
        sensor = self.config_model.ConfiguredSensor(
            sensor_id=2,
            slug="room_sensor_c",
            name="Room Sensor C",
            thermostat_id=1,
            thermostat_slug="main",
            include_temperature=True,
            include_air_quality=False,
            include_co2=False,
            include_voc=False,
        )

        self.assertEqual(
            self.entity.thermostat_device_info(thermostat)["via_device"],
            ("beestat_statistics", "service"),
        )
        self.assertEqual(
            self.entity.room_sensor_device_info(sensor)["via_device"],
            ("beestat_statistics", "service"),
        )

    def test_service_device_is_registered_before_children_reference_it(self) -> None:
        entry = types.SimpleNamespace(entry_id="entry-1")

        self.entity.async_register_service_device(object(), entry)

        self.assertEqual(
            self.fake_device_registry.calls,
            [
                {
                    "config_entry_id": "entry-1",
                    "identifiers": {("beestat_statistics", "service")},
                    "name": "Beestat Statistics",
                    "manufacturer": "Beestat",
                    "entry_type": "service",
                    "configuration_url": "https://app.beestat.io/",
                }
            ],
        )

    def test_homekit_devices_keep_homekit_identity(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            device_identifiers=(("homekit_controller", "thermostat-device"),),
        )
        sensor = self.config_model.ConfiguredSensor(
            sensor_id=2,
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

        thermostat_info = self.entity.thermostat_device_info(thermostat)
        sensor_info = self.entity.room_sensor_device_info(sensor)

        self.assertEqual(
            thermostat_info["identifiers"],
            {("homekit_controller", "thermostat-device")},
        )
        self.assertEqual(
            sensor_info["identifiers"],
            {("homekit_controller", "sensor-device")},
        )
        for info in (thermostat_info, sensor_info):
            self.assertNotIn("name", info)
            self.assertNotIn("manufacturer", info)
            self.assertNotIn("model", info)
            self.assertNotIn("configuration_url", info)

    def test_thermostat_suggested_object_id_uses_fallback_only_for_beestat_devices(
        self,
    ) -> None:
        fallback = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
        )
        homekit = self.config_model.ConfiguredThermostat(
            thermostat_id=2,
            slug="mapped",
            name="Mapped",
            device_identifiers=(("homekit_controller", "thermostat-device"),),
        )

        self.assertEqual(
            self.entity.thermostat_suggested_object_id(fallback, "filter_due"),
            "beestat_main_filter_due",
        )
        self.assertIsNone(
            self.entity.thermostat_suggested_object_id(homekit, "filter_due")
        )

    def _install_fake_homeassistant_modules(self) -> None:
        homeassistant = types.ModuleType("homeassistant")
        helpers = types.ModuleType("homeassistant.helpers")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        entity = types.ModuleType("homeassistant.helpers.entity")
        entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
        self.fake_device_registry = types.SimpleNamespace(calls=[])

        def async_get(_hass):
            return types.SimpleNamespace(
                async_get_or_create=lambda **kwargs: (
                    self.fake_device_registry.calls.append(kwargs)
                )
            )

        device_registry.DeviceEntryType = types.SimpleNamespace(SERVICE="service")
        device_registry.async_get = async_get
        entity.DeviceInfo = lambda **kwargs: kwargs
        entity.Entity = object
        entity_platform.AddConfigEntryEntitiesCallback = object

        helpers.device_registry = device_registry
        helpers.entity = entity
        helpers.entity_platform = entity_platform
        homeassistant.helpers = helpers
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.entity"] = entity
        sys.modules["homeassistant.helpers.entity_platform"] = entity_platform


if __name__ == "__main__":
    unittest.main()
