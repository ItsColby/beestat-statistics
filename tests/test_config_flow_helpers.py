"""Tests for dependency-light config-flow helpers."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

if __package__:
    from ._module_loader import load_module
else:
    from _module_loader import load_module

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_config_flow_helper_test"


def _load_module(name: str):
    return load_module(ROOT, PACKAGE, name)


class ConfigFlowHelpersTest(unittest.TestCase):
    """Validate config-flow helpers without a Home Assistant test harness."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
                "homeassistant",
                "homeassistant.config_entries",
                "homeassistant.const",
                "homeassistant.core",
                "homeassistant.helpers",
                "homeassistant.helpers.aiohttp_client",
                "homeassistant.helpers.entity_registry",
                "homeassistant.helpers.issue_registry",
                "homeassistant.helpers.selector",
                "voluptuous",
            )
        }
        self._install_fake_modules()
        _load_module("const")
        _load_module("api")
        _load_module("config_payload")
        self.config_flow = _load_module("config_flow")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_account_fingerprint_uses_hashed_thermostat_anchors(self) -> None:
        fingerprint = self.config_flow._account_fingerprint(
            [
                {"id": 1001},
                {"thermostat_id": "2002"},
                {"id": None},
            ]
        )

        self.assertIsNotNone(fingerprint)
        text = repr(fingerprint)
        self.assertNotIn("1001", text)
        self.assertNotIn("2002", text)
        self.assertEqual(len(fingerprint["thermostat_id_hashes"]), 2)
        self.assertTrue(
            all(len(value) == 64 for value in fingerprint["thermostat_id_hashes"])
        )
        self.assertEqual(len(fingerprint["signature"]), 64)

    def test_account_anchors_normalize_exact_ids_and_reject_malformed_ids(self) -> None:
        expected = self.config_flow._account_fingerprint([{"id": 1}])
        self.assertEqual(
            self.config_flow._account_fingerprint(
                [{"id": "001"}, {"id": 1.0}, {"id": True}, {"id": 1.5}, {"id": 0}]
            ),
            expected,
        )
        self.assertIsNone(
            self.config_flow._account_fingerprint([{"id": True}, {"id": 1.5}])
        )

    def test_wrong_account_supports_legacy_signature_values(self) -> None:
        self.assertFalse(
            self.config_flow._wrong_account(
                {"account_fingerprint": "same-signature"},
                "same-signature",
            )
        )
        self.assertTrue(
            self.config_flow._wrong_account(
                {"account_fingerprint": "old-signature"},
                "new-signature",
            )
        )

    def test_same_connection_data_defaults_api_base(self) -> None:
        self.assertTrue(
            self.config_flow._same_connection_data(
                {"api_key": "key"},
                {
                    "api_key": "key",
                    "api_base": "https://api.beestat.io/",
                },
            )
        )
        self.assertFalse(
            self.config_flow._same_connection_data(
                {
                    "api_key": "key",
                    "api_base": "https://api.beestat.io/",
                },
                {
                    "api_key": "other-key",
                    "api_base": "https://api.beestat.io/",
                },
            )
        )

    def test_validated_connection_change_requires_known_same_account(self) -> None:
        current = {
            "account_fingerprint": {
                "thermostat_id_hashes": ["shared", "old"],
                "signature": "old-signature",
            }
        }

        self.assertTrue(
            self.config_flow._validated_connection_change_is_safe(
                current,
                {
                    "thermostat_id_hashes": ["shared", "new"],
                    "signature": "new-signature",
                },
            )
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe(
                current,
                {
                    "thermostat_id_hashes": ["other"],
                    "signature": "other-signature",
                },
            )
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe({}, current)
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe(current, None)
        )

    def test_source_scope_signature_includes_labels_and_activity_context(self) -> None:
        """Source drift evidence covers more than the discovered IDs."""

        self.assertEqual(
            self.config_flow._source_scope_signature(
                [{"value": "1", "label": "Zone A (1, inactive)"}],
                [{"value": "2", "label": "Room B (2)"}],
            ),
            (
                ("thermostat", "1", "Zone A (1, inactive)"),
                ("sensor", "2", "Room B (2)"),
            ),
        )

    def test_sensor_source_identity_excludes_parent_thermostat_id(self) -> None:
        entry = types.SimpleNamespace(
            data={},
            options={"sensors": [{"sensor_id": 2003, "thermostat_id": 1001}]},
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(
                    data=types.SimpleNamespace(
                        config=types.SimpleNamespace(sensors=()),
                        sensor_rows=(
                            {
                                "sensor_id": 2001,
                                "thermostat_id": 1001,
                                "name": "Room A",
                                "inactive": True,
                            },
                            {"id": 2002, "thermostat_id": 1001, "name": "Room B"},
                        ),
                    )
                )
            ),
        )
        choices = self.config_flow._sensor_options(entry)
        self.assertEqual({item["value"] for item in choices}, {"2001", "2002", "2003"})
        self.assertEqual(
            self.config_flow._inactive_resource_ids(
                entry, rows_attribute="sensor_rows", id_field="sensor_id"
            ),
            {2001},
        )

    def test_saved_sensor_mapping_identity_uses_override_id_not_parent(self) -> None:
        override = {
            "id": 2001,
            "thermostat_id": 1001,
            "temperature_entity_id": "sensor.selected",
        }
        self.assertEqual(
            self.config_flow._effective_override(
                {"sensors": [override]}, "sensors", 2001
            ),
            override,
        )

    def test_automatic_mapping_rechecks_current_registry_device_conflicts(self) -> None:
        first = types.SimpleNamespace(
            id="first-registry-id",
            entity_id="climate.first",
            domain="climate",
            platform="homekit_controller",
            unique_id="first-source",
            device_id="first-device",
        )
        second = types.SimpleNamespace(
            id="second-registry-id",
            entity_id="climate.second",
            domain="climate",
            platform="homekit_controller",
            unique_id="second-source",
            device_id="second-device",
        )
        entries = (first, second)
        registry = types.SimpleNamespace(
            async_get=lambda value: next(
                (item for item in entries if value in (item.id, item.entity_id)), None
            ),
            async_get_entity_id=lambda domain, platform, unique_id: next(
                (
                    item.entity_id
                    for item in entries
                    if (item.domain, item.platform, item.unique_id)
                    == (domain, platform, unique_id)
                ),
                None,
            ),
        )
        entry = types.SimpleNamespace(
            data={},
            options={
                "thermostats": [{"id": 1001, "climate_entity_id": first.entity_id}]
            },
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(
                    data=types.SimpleNamespace(
                        config=types.SimpleNamespace(
                            thermostats=(
                                types.SimpleNamespace(
                                    thermostat_id=1001,
                                    name="First",
                                    climate_entity_id=first.entity_id,
                                ),
                                types.SimpleNamespace(
                                    thermostat_id=1002,
                                    name="Second",
                                    climate_entity_id=second.entity_id,
                                ),
                            ),
                            sensors=(),
                        )
                    )
                )
            ),
        )
        self.assertIsNotNone(
            self.config_flow._automatic_mapping_options(entry, registry)
        )
        second.device_id = first.device_id
        self.assertIsNone(self.config_flow._automatic_mapping_options(entry, registry))
        self.assertEqual(
            entry.options,
            {"thermostats": [{"id": 1001, "climate_entity_id": first.entity_id}]},
        )

    def _install_fake_modules(self) -> None:
        aiohttp = types.ModuleType("aiohttp")
        homeassistant = types.ModuleType("homeassistant")
        config_entries = types.ModuleType("homeassistant.config_entries")
        const = types.ModuleType("homeassistant.const")
        core = types.ModuleType("homeassistant.core")
        helpers = types.ModuleType("homeassistant.helpers")
        aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
        issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
        entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        selector = types.ModuleType("homeassistant.helpers.selector")
        voluptuous = types.ModuleType("voluptuous")

        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        config_entries.ConfigFlow = _FakeConfigFlow
        config_entries.ConfigFlowResult = dict
        config_entries.ConfigEntry = object
        config_entries.OptionsFlow = object
        config_entries.OptionsFlowWithReload = object
        const.CONF_API_KEY = "api_key"
        core.HomeAssistant = object
        core.callback = lambda func: func
        aiohttp_client.async_get_clientsession = lambda _hass: object()
        issue_registry.IssueSeverity = types.SimpleNamespace(WARNING="warning")
        issue_registry.async_create_issue = lambda *args, **kwargs: None
        issue_registry.async_delete_issue = lambda *args, **kwargs: None
        entity_registry.async_get = lambda _hass: None
        selector.BooleanSelector = _NoopInit
        selector.EntitySelector = _NoopInit
        selector.EntitySelectorConfig = _NoopInit
        selector.NumberSelector = _NoopInit
        selector.NumberSelectorConfig = _NoopInit
        selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
        selector.SelectOptionDict = lambda **kwargs: dict(kwargs)
        selector.SelectSelector = _NoopInit
        selector.SelectSelectorConfig = _NoopInit
        selector.TextSelector = _NoopInit
        selector.TextSelectorConfig = _NoopInit
        selector.TextSelectorType = types.SimpleNamespace(
            PASSWORD="password",
            URL="url",
        )
        voluptuous.Schema = lambda schema, *args, **kwargs: schema
        voluptuous.Required = lambda key, **kwargs: _SchemaKey(key, **kwargs)
        voluptuous.Optional = lambda key, **kwargs: _SchemaKey(key, **kwargs)

        homeassistant.config_entries = config_entries
        homeassistant.const = const
        homeassistant.core = core
        helpers.aiohttp_client = aiohttp_client
        helpers.issue_registry = issue_registry
        helpers.entity_registry = entity_registry
        helpers.device_registry = device_registry
        helpers.selector = selector
        homeassistant.helpers = helpers

        sys.modules["aiohttp"] = aiohttp
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.config_entries"] = config_entries
        sys.modules["homeassistant.const"] = const
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
        sys.modules["homeassistant.helpers.issue_registry"] = issue_registry
        sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
        sys.modules["homeassistant.helpers.selector"] = selector
        sys.modules["voluptuous"] = voluptuous


class _FakeConfigFlow:
    def __init_subclass__(cls, **kwargs) -> None:
        return None


class _NoopInit:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _SchemaKey:
    def __init__(self, key, **kwargs) -> None:
        self.key = key
        self.kwargs = kwargs

    def __hash__(self) -> int:
        return hash((self.key, tuple(sorted(self.kwargs.items()))))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SchemaKey) and (
            self.key,
            self.kwargs,
        ) == (
            other.key,
            other.kwargs,
        )


if __name__ == "__main__":
    unittest.main()
