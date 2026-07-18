"""Tests for Beestat coordinator interpretation helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_coordinator_test"


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


class CoordinatorHelpersTest(unittest.TestCase):
    """Validate pure coordinator helpers without a Home Assistant runtime."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
                "homeassistant",
                "homeassistant.core",
                "homeassistant.exceptions",
                "homeassistant.helpers",
                "homeassistant.helpers.update_coordinator",
            )
        }
        self._install_fake_homeassistant_modules()
        _load_module("const")
        self.config_model = _load_module("config_model")
        self.coordinator = _load_module("coordinator")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_runtime_summary_helpers_ignore_bad_dates_and_accumulate_fan_hours(self) -> None:
        rows = [
            {"date": "2026-07-01", "sum_fan": 3600},
            {"date": "bad", "sum_fan": 9999},
            {"date": "2026-07-03", "sum_fan": 1800},
        ]

        self.assertEqual(self.coordinator._latest_row_date(rows), date(2026, 7, 3))
        self.assertEqual(
            self.coordinator._runtime_hours_since(rows, date(2026, 7, 2)),
            0.5,
        )
        self.assertEqual(
            self.coordinator._recent_runtime_hours_per_day(
                rows,
                date(2026, 7, 4),
            ),
            0.75,
        )

    def test_filter_changed_date_walks_nested_filter_payloads(self) -> None:
        self.assertEqual(
            self.coordinator._beestat_filter_changed_date(
                {
                    "filters": {
                        "primary": {"changed": "2026-06-01"},
                        "secondary": [{"last_changed": "2026-07-02 12:00:00"}],
                    }
                }
            ),
            date(2026, 7, 2),
        )

    def test_home_assistant_filter_changed_date_overrides_helper_and_beestat(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_changed_entity_id="input_datetime.main_hvac_filter_changed",
            filter_changed_date=date(2026, 7, 5),
        )
        coordinator = types.SimpleNamespace(
            hass=types.SimpleNamespace(
                states=types.SimpleNamespace(
                    get=lambda _entity_id: types.SimpleNamespace(state="2026-06-18")
                )
            )
        )

        changed_date, source = (
            self.coordinator.BeestatRuntimeDataCoordinator._filter_changed_date(
                coordinator,
                thermostat,
                {"filters": {"primary": {"changed": "2026-06-01"}}},
            )
        )

        self.assertEqual(changed_date, date(2026, 7, 5))
        self.assertEqual(source, "home_assistant")

    def test_current_profile_uses_ecobee_program_sensor_names(self) -> None:
        current_ref, current_name, sensor_names = self.coordinator._current_profile(
            {
                "program": {
                    "currentClimateRef": "home",
                    "climates": [
                        {"climateRef": "away", "name": "Away"},
                        {
                            "climateRef": "home",
                            "name": "Home",
                            "sensors": [{"name": "Room Sensor C"}, {"name": "Room Sensor B"}],
                        },
                    ],
                }
            }
        )

        self.assertEqual(current_ref, "home")
        self.assertEqual(current_name, "Home")
        self.assertEqual(sensor_names, ("Room Sensor C", "Room Sensor B"))

    def test_schedule_snapshot_finds_current_and_next_profile(self) -> None:
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[3][20] = "home"
        snapshot = self.coordinator._schedule_snapshot(
            {
                "timezone": "America/New_York",
                "program": {
                    "climates": [
                        {"climateRef": "sleep", "name": "Sleep", "isOccupied": False},
                        {"climateRef": "home", "name": "Home", "isOccupied": True},
                    ],
                    "schedule": schedule,
                },
            },
            datetime(2026, 7, 1, 13, 15, tzinfo=timezone.utc),
            ZoneInfo("America/New_York"),
        )

        self.assertEqual(snapshot["scheduled_ref"], "sleep")
        self.assertEqual(snapshot["scheduled_name"], "Sleep")
        self.assertEqual(snapshot["next_ref"], "home")
        self.assertEqual(snapshot["next_name"], "Home")
        self.assertEqual(snapshot["next_at"].isoformat(), "2026-07-01T14:00:00+00:00")
        self.assertEqual(
            [(profile.ref, profile.name, profile.is_occupied) for profile in snapshot["profiles"]],
            [("sleep", "Sleep", False), ("home", "Home", True)],
        )

    def test_thermostat_metadata_filters_inactive_sensors_and_active_alerts(self) -> None:
        sensor_metadata = {
            10: self.coordinator.SensorMetadata(
                sensor_id=10,
                thermostat_id=1,
                name="Room Sensor B",
                identifier="room_sensor_b",
                sensor_type="ecobee3_remote_sensor",
                in_use=True,
                inactive=False,
                deleted=False,
            ),
            11: self.coordinator.SensorMetadata(
                sensor_id=11,
                thermostat_id=1,
                name="Room Sensor C",
                identifier="room_sensor_c",
                sensor_type="ecobee3_remote_sensor",
                in_use=True,
                inactive=True,
                deleted=False,
            ),
        }
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
        )

        metadata = self.coordinator._build_thermostat_metadata(
            (
                {
                    "id": 1,
                    "data_begin": "2026-07-01 00:00:00",
                    "data_end": "2026-07-01 12:00:00",
                    "alerts": [
                        {
                            "code": "filter",
                            "notificationType": "maintenance",
                            "severity": "low",
                            "text": "Replace filter",
                        },
                        {"code": "dismissed", "dismissed": True},
                    ],
                },
            ),
            sensor_metadata,
            datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
            ZoneInfo("America/New_York"),
            (thermostat,),
        )[1]

        self.assertEqual(metadata.active_sensor_count, 1)
        self.assertEqual(metadata.active_sensor_names, ("Room Sensor B",))
        self.assertEqual(metadata.data_lag_minutes, 60)
        self.assertEqual(metadata.active_alert_count, 1)
        self.assertEqual(metadata.active_alerts[0]["code"], "filter")

    def test_filter_alert_guids_selects_active_filter_alerts_only(self) -> None:
        row = {
            "alerts": [
                {"guid": "main", "text": "Replace filter"},
                {"guid": "dismissed", "text": "Replace filter", "dismissed": True},
                {"guid": "maintenance", "text": "Schedule tune up"},
                {"guid": "code", "alertNumber": 3137, "text": "Reminder"},
                {"guid": "main", "text": "Replace filter"},
                {"text": "Replace filter"},
            ]
        }

        self.assertEqual(
            self.coordinator._filter_alert_guids(row),
            ("main", "code"),
        )

    def test_summary_window_start_covers_recent_runtime_and_filter_change(self) -> None:
        config = self.config_model.BeestatConfig(
            thermostats=(
                self.config_model.ConfiguredThermostat(
                    thermostat_id=1,
                    slug="main",
                    name="Main",
                ),
            ),
            sensors=(),
        )

        def filter_changed_date(_thermostat, _row):
            return date(2026, 6, 1), "beestat"

        coordinator = types.SimpleNamespace(_filter_changed_date=filter_changed_date)

        start = self.coordinator.BeestatRuntimeDataCoordinator._summary_window_start(
            coordinator,
            config,
            ({"id": 1},),
            date(2026, 7, 5),
        )

        self.assertEqual(start, date(2026, 6, 1))

    def test_import_error_marks_summary_attempt_failed(self) -> None:
        coordinator = types.SimpleNamespace(
            _client=types.SimpleNamespace(redact_error=lambda err: str(err)),
            async_update_listeners=lambda: None,
        )
        coordinator._async_record_error = (
            lambda err: self.coordinator.BeestatRuntimeDataCoordinator._async_record_error(
                coordinator,
                err,
            )
        )

        self.coordinator.BeestatRuntimeDataCoordinator.async_record_import_error(
            coordinator,
            RuntimeError("boom"),
        )

        self.assertEqual(coordinator.last_error, "boom")
        self.assertEqual(coordinator.last_import_summary_mode, "failed")
        self.assertIsNone(coordinator.last_import_summary_window_start)
        self.assertEqual(
            coordinator.last_import_summary_fallback_reason,
            "import_failed",
        )

    def _install_fake_homeassistant_modules(self) -> None:
        homeassistant = types.ModuleType("homeassistant")
        core = types.ModuleType("homeassistant.core")
        exceptions = types.ModuleType("homeassistant.exceptions")
        helpers = types.ModuleType("homeassistant.helpers")
        update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
        aiohttp = types.ModuleType("aiohttp")

        core.HomeAssistant = object
        core.callback = lambda func: func
        exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object

        helpers.update_coordinator = update_coordinator
        homeassistant.core = core
        homeassistant.exceptions = exceptions
        homeassistant.helpers = helpers

        sys.modules["aiohttp"] = aiohttp
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.exceptions"] = exceptions
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


class _FakeDataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, *args, **kwargs) -> None:
        self.data = None

    def async_set_update_error(self, err: Exception) -> None:
        self.error = err

    def async_set_updated_data(self, data) -> None:
        self.data = data

    def async_update_listeners(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
