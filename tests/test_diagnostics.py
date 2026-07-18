"""Tests for Beestat diagnostics output redaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_diagnostics_test"


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
class FakeEntry:
    data: dict
    options: dict
    runtime_data: object


class DiagnosticsTest(unittest.TestCase):
    """Validate diagnostics are useful without leaking local identifiers."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
                "homeassistant",
                "homeassistant.components",
                "homeassistant.components.diagnostics",
                "homeassistant.config_entries",
                "homeassistant.const",
                "homeassistant.core",
                "homeassistant.exceptions",
                "homeassistant.helpers",
                "homeassistant.helpers.update_coordinator",
            )
        }
        self._install_fake_homeassistant_modules()
        _load_module("const")
        _load_module("api")
        self.config_model = _load_module("config_model")
        self.coordinator = _load_module("coordinator")
        _load_module("runtime")
        self.diagnostics = _load_module("diagnostics")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_diagnostics_redact_local_mapping_identifiers(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1001,
            slug="zone_a",
            name="Zone A",
            climate_entity_id="climate.zone_a",
            temperature_entity_id="sensor.zone_a_temperature",
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
            motion_entity_id="binary_sensor.room_sensor_a_motion",
        )
        summary = self.coordinator.ThermostatRuntimeSummary(
            thermostat_id=1001,
            slug="zone_a",
            label="Zone A",
            latest_date=None,
            lag_days=None,
            filter_changed_date=None,
            filter_changed_source=None,
            filter_runtime_hours=None,
            recent_runtime_hours_per_day=None,
        )
        metadata = self.coordinator.ThermostatMetadata(
            thermostat_id=1001,
            slug="zone_a",
            label="Zone A",
            data_begin=None,
            data_end=None,
            data_lag_minutes=None,
            current_climate_ref="home",
            current_climate_name="Home",
            scheduled_climate_ref="home",
            scheduled_climate_name="Home",
            next_scheduled_climate_ref=None,
            next_scheduled_climate_name=None,
            next_scheduled_at=None,
            schedule_profiles=(),
            active_sensor_count=1,
            active_sensor_names=("Room Sensor A",),
            current_profile_sensor_names=("Room Sensor A",),
            active_alert_count=0,
            active_alerts=(),
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
            thermostats={1001: summary},
            thermostat_metadata={1001: metadata},
            sensor_metadata={},
        )
        runtime = types.SimpleNamespace(
            coordinator=types.SimpleNamespace(
                data=data,
                status="ok",
                last_error=(
                    "GET https://api.example.test/?api_key=secret-key failed"
                ),
                last_error_at=None,
                last_import_success_at=None,
                last_imported_series=None,
                last_imported_rows=None,
                last_import_source_rows=None,
                last_import_partial=False,
                last_import_skipped_windows=0,
                last_import_skipped_runtime_thermostat_windows=0,
                last_import_skipped_runtime_sensor_windows=0,
                last_import_summary_mode="windowed",
                last_import_summary_window_start="2026-06-28",
                last_import_summary_window_end="2026-07-05",
                last_import_summary_overlap_days=7,
                last_import_summary_fallback_reason=None,
                last_import_cumulative_seed_count=5,
                last_filter_alert_dismiss_attempt_at=None,
                last_filter_alert_dismiss_thermostat_id=1001,
                last_filter_alert_dismiss_matched=1,
                last_filter_alert_dismissed=1,
                last_filter_alert_dismiss_error=None,
            )
        )
        entry = FakeEntry(
            data={
                "api_key": "secret-key",
                "api_base": "https://api.example.test/",
                "account_fingerprint": "fingerprint-secret",
                "thermostats": [
                    {
                        "id": 1001,
                        "climate_entity_id": "climate.zone_a",
                    }
                ],
            },
            options={},
            runtime_data=runtime,
        )

        result = asyncio.run(
            self.diagnostics.async_get_config_entry_diagnostics(object(), entry)
        )
        text = repr(result)

        self.assertIsInstance(result["beestat_data"]["thermostats"], list)
        self.assertIsInstance(result["beestat_data"]["sensors"], list)
        self.assertNotIn("secret-key", text)
        self.assertNotIn("fingerprint-secret", text)
        self.assertNotIn("https://api.example.test/", text)
        self.assertNotIn("https://api.example.test", text)
        self.assertNotIn("climate.zone_a", text)
        self.assertNotIn("sensor.room_sensor_a_temperature", text)
        self.assertNotIn("binary_sensor.room_sensor_a_occupancy", text)
        self.assertIn("REDACTED", text)

    def _install_fake_homeassistant_modules(self) -> None:
        aiohttp = types.ModuleType("aiohttp")
        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        diagnostics = types.ModuleType("homeassistant.components.diagnostics")
        config_entries = types.ModuleType("homeassistant.config_entries")
        const = types.ModuleType("homeassistant.const")
        core = types.ModuleType("homeassistant.core")
        exceptions = types.ModuleType("homeassistant.exceptions")
        helpers = types.ModuleType("homeassistant.helpers")
        update_coordinator = types.ModuleType(
            "homeassistant.helpers.update_coordinator"
        )

        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        diagnostics.async_redact_data = _redact_data
        config_entries.ConfigEntry = _Generic
        const.CONF_API_KEY = "api_key"
        core.HomeAssistant = object
        core.callback = lambda func: func
        exceptions.ConfigEntryAuthFailed = type(
            "ConfigEntryAuthFailed",
            (Exception,),
            {},
        )
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})

        components.diagnostics = diagnostics
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
        sys.modules["homeassistant.components.diagnostics"] = diagnostics
        sys.modules["homeassistant.config_entries"] = config_entries
        sys.modules["homeassistant.const"] = const
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.exceptions"] = exceptions
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


class _Generic:
    def __class_getitem__(cls, _item):
        return cls


class _FakeDataUpdateCoordinator:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __class_getitem__(cls, _item):
        return cls


def _redact_data(value, to_redact):
    if isinstance(value, dict):
        return {
            key: "REDACTED" if key in to_redact else _redact_data(item, to_redact)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_data(item, to_redact) for item in value]
    return value


if __name__ == "__main__":
    unittest.main()
