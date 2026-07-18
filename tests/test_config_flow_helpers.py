"""Tests for dependency-light config-flow helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_config_flow_helper_test"


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

    def test_wrong_account_allows_overlapping_thermostat_anchor(self) -> None:
        current = {
            "account_fingerprint": {
                "thermostat_id_hashes": ["shared", "old"],
                "signature": "old-signature",
            }
        }

        self.assertFalse(
            self.config_flow._wrong_account(
                current,
                {
                    "thermostat_id_hashes": ["shared", "new"],
                    "signature": "new-signature",
                },
            )
        )
        self.assertTrue(
            self.config_flow._wrong_account(
                current,
                {
                    "thermostat_id_hashes": ["other"],
                    "signature": "other-signature",
                },
            )
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

    def _install_fake_modules(self) -> None:
        aiohttp = types.ModuleType("aiohttp")
        homeassistant = types.ModuleType("homeassistant")
        config_entries = types.ModuleType("homeassistant.config_entries")
        const = types.ModuleType("homeassistant.const")
        core = types.ModuleType("homeassistant.core")
        helpers = types.ModuleType("homeassistant.helpers")
        aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
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
        helpers.selector = selector
        homeassistant.helpers = helpers

        sys.modules["aiohttp"] = aiohttp
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.config_entries"] = config_entries
        sys.modules["homeassistant.const"] = const
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
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
