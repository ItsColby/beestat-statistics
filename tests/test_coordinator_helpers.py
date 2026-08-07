"""Tests for Beestat coordinator interpretation helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_coordinator_test"


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
                "homeassistant.helpers.device_registry",
                "homeassistant.helpers.entity_registry",
                "homeassistant.helpers.event",
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

    def test_runtime_summary_helpers_ignore_bad_dates_and_accumulate_fan_hours(
        self,
    ) -> None:
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

    def test_runtime_hours_subtracts_click_baseline_only_from_change_day(self) -> None:
        rows = [
            {"date": "2026-07-05", "sum_fan": 36000},
            {"date": "2026-07-06", "sum_fan": 7200},
        ]

        self.assertEqual(
            self.coordinator._runtime_hours_since(
                rows,
                date(2026, 7, 5),
                change_day_baseline_seconds=28800,
            ),
            4.0,
        )

    def test_runtime_hours_clamps_corrected_change_day_below_click_baseline(
        self,
    ) -> None:
        rows = [
            {"date": "2026-07-05", "sum_fan": 18000},
            {"date": "2026-07-06", "sum_fan": 7200},
        ]

        self.assertEqual(
            self.coordinator._runtime_hours_since(
                rows,
                date(2026, 7, 5),
                change_day_baseline_seconds=28800,
            ),
            2.0,
        )

    def test_runtime_seconds_on_date_filters_thermostat_and_date(self) -> None:
        rows = (
            {"thermostat_id": 1001, "date": "2026-07-05", "sum_fan": 3600},
            {"thermostat_id": 1002, "date": "2026-07-05", "sum_fan": 7200},
            {"thermostat_id": 1001, "date": "2026-07-06", "sum_fan": 10800},
            {"thermostat_id": 1001, "date": "bad", "sum_fan": 9999},
        )

        self.assertEqual(
            self.coordinator._runtime_seconds_on_date(
                rows,
                thermostat_id=1001,
                target_date=date(2026, 7, 5),
            ),
            3600,
        )

    def test_runtime_seconds_on_date_distinguishes_missing_row_from_zero(self) -> None:
        rows = ({"thermostat_id": 1001, "date": "2026-07-05", "sum_fan": 0},)

        self.assertEqual(
            self.coordinator._runtime_seconds_on_date(
                rows,
                thermostat_id=1001,
                target_date=date(2026, 7, 5),
            ),
            0,
        )
        self.assertIsNone(
            self.coordinator._runtime_seconds_on_date(
                rows,
                thermostat_id=1001,
                target_date=date(2026, 7, 6),
            )
        )

    def test_raw_filter_boundary_rounds_to_nearest_five_minute_interval(self) -> None:
        changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")
        rows = [
            {"timestamp": "2026-07-05T21:40:00+00:00", "fan": 300},
            {"timestamp": "2026-07-05T21:45:00+00:00", "fan": 180},
            {"timestamp": "2026-07-05T21:50:00+00:00", "fan": 120},
        ]

        boundary = self.coordinator._raw_filter_boundary(rows, changed_at)

        self.assertIsNotNone(boundary)
        self.assertEqual(boundary.baseline_seconds, 480)
        self.assertEqual(
            boundary.source_data_end,
            datetime.fromisoformat("2026-07-05T21:50:00+00:00"),
        )
        self.assertEqual(
            boundary.effective_at,
            datetime.fromisoformat("2026-07-05T21:50:00+00:00"),
        )

    def test_raw_filter_boundary_remains_pending_until_click_bucket_exists(
        self,
    ) -> None:
        changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")

        self.assertIsNone(
            self.coordinator._raw_filter_boundary(
                [
                    {"timestamp": "2026-07-05T21:40:00+00:00", "fan": 300},
                ],
                changed_at,
            )
        )

    def test_filter_boundary_status_distinguishes_pending_and_legacy_records(
        self,
    ) -> None:
        configured = self.config_model.ConfiguredThermostat

        self.assertEqual(
            self.config_model.filter_boundary_status(
                configured(
                    thermostat_id=1,
                    slug="zone_a",
                    name="Zone A",
                    filter_changed_date=date(2026, 7, 5),
                    filter_changed_at=datetime.fromisoformat(
                        "2026-07-05T21:48:00+00:00"
                    ),
                )
            ),
            "pending_data",
        )
        self.assertEqual(
            self.config_model.filter_boundary_status(
                configured(
                    thermostat_id=1,
                    slug="zone_a",
                    name="Zone A",
                    filter_changed_date=date(2026, 7, 5),
                )
            ),
            "legacy_date_only",
        )

    def test_pending_click_boundary_starts_new_filter_runtime_at_zero(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 7, 5),
            filter_changed_at=datetime.fromisoformat("2026-07-05T21:48:00+00:00"),
        )

        self.assertEqual(
            self.coordinator._filter_runtime_hours(
                [{"date": "2026-07-05", "sum_fan": 14400}],
                date(2026, 7, 5),
                thermostat,
                "home_assistant",
            ),
            0.0,
        )

    def test_filter_boundary_fast_retry_window_is_bounded(self) -> None:
        now = datetime.fromisoformat("2026-07-06T03:00:00+00:00")

        self.assertTrue(
            self.coordinator._filter_boundary_fast_retry_due(
                now - timedelta(hours=5, minutes=59),
                now,
            )
        )
        self.assertFalse(
            self.coordinator._filter_boundary_fast_retry_due(
                now - timedelta(hours=6, minutes=1),
                now,
            )
        )
        self.assertFalse(
            self.coordinator._filter_boundary_fast_retry_due(
                now + timedelta(minutes=1),
                now,
            )
        )

    def test_filter_boundary_scheduler_skips_expired_pending_click(self) -> None:
        coordinator = object.__new__(self.coordinator.BeestatRuntimeDataCoordinator)
        coordinator.hass = types.SimpleNamespace()
        coordinator._cancel_filter_boundary_retry = None
        coordinator.config_entry = types.SimpleNamespace(
            data={},
            options={
                "thermostats": [
                    {
                        "id": 1,
                        "filter_changed_at": (
                            datetime.now(UTC) - timedelta(hours=7)
                        ).isoformat(),
                    }
                ]
            },
        )
        coordinator.data = types.SimpleNamespace(
            config=self.config_model.BeestatConfig(
                thermostats=(
                    self.config_model.ConfiguredThermostat(
                        thermostat_id=1,
                        slug="zone_a",
                        name="Zone A",
                        filter_changed_at=datetime.now(UTC) - timedelta(hours=7),
                    ),
                ),
                sensors=(),
            )
        )

        coordinator.async_schedule_filter_boundary_reconcile()

        self.assertIsNone(coordinator._cancel_filter_boundary_retry)

        recent_config = self.config_model.BeestatConfig(
            thermostats=(
                self.config_model.ConfiguredThermostat(
                    thermostat_id=1,
                    slug="zone_a",
                    name="Zone A",
                    filter_changed_at=datetime.now(UTC) - timedelta(hours=1),
                ),
            ),
            sensors=(),
        )
        coordinator.config_entry.options["thermostats"][0]["filter_changed_at"] = (
            datetime.now(UTC) - timedelta(hours=1)
        ).isoformat()
        coordinator.async_schedule_filter_boundary_reconcile(recent_config)

        self.assertIsNotNone(coordinator._cancel_filter_boundary_retry)

    def test_pending_click_boundary_counts_complete_later_days(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 7, 5),
            filter_changed_at=datetime.fromisoformat("2026-07-05T21:48:00+00:00"),
        )

        self.assertEqual(
            self.coordinator._filter_runtime_hours(
                [
                    {"date": "2026-07-05", "sum_fan": 14400},
                    {"date": "2026-07-06", "sum_fan": 7200},
                ],
                date(2026, 7, 5),
                thermostat,
                "home_assistant",
            ),
            2.0,
        )

    def test_click_boundary_with_unverified_baseline_remains_pending(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 7, 5),
            filter_changed_at=datetime.fromisoformat("2026-07-05T21:48:00+00:00"),
            filter_change_day_runtime_baseline_seconds=14400,
        )

        self.assertEqual(
            self.config_model.filter_boundary_status(thermostat),
            "pending_data",
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
                            "sensors": [
                                {"name": "Room Sensor C"},
                                {"name": "Room Sensor B"},
                            ],
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
            datetime(2026, 7, 1, 13, 15, tzinfo=UTC),
            ZoneInfo("America/New_York"),
        )

        self.assertEqual(snapshot["scheduled_ref"], "sleep")
        self.assertEqual(snapshot["scheduled_name"], "Sleep")
        self.assertEqual(snapshot["next_ref"], "home")
        self.assertEqual(snapshot["next_name"], "Home")
        self.assertEqual(snapshot["next_at"].isoformat(), "2026-07-01T14:00:00+00:00")
        self.assertEqual(
            [
                (profile.ref, profile.name, profile.is_occupied)
                for profile in snapshot["profiles"]
            ],
            [("sleep", "Sleep", False), ("home", "Home", True)],
        )

    def test_schedule_snapshot_skips_nonexistent_spring_forward_slots(self) -> None:
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[0][4] = "home"
        schedule[0][5] = "home"
        schedule[0][8] = "away"

        snapshot = self.coordinator._schedule_snapshot(
            {
                "timezone": "America/New_York",
                "program": {
                    "climates": [
                        {"climateRef": "sleep", "name": "Sleep"},
                        {"climateRef": "home", "name": "Home"},
                        {"climateRef": "away", "name": "Away"},
                    ],
                    "schedule": schedule,
                },
            },
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
            ZoneInfo("America/New_York"),
        )

        self.assertEqual(snapshot["scheduled_ref"], "sleep")
        self.assertEqual(snapshot["next_ref"], "away")
        self.assertEqual(snapshot["next_at"], datetime(2026, 3, 8, 8, tzinfo=UTC))

    def test_next_local_midnight_tracks_dst_day_lengths(self) -> None:
        local_tz = ZoneInfo("America/New_York")

        before_spring = datetime(2026, 3, 8, 5, tzinfo=UTC)
        before_fall = datetime(2026, 11, 1, 4, tzinfo=UTC)

        self.assertEqual(
            self.coordinator._next_local_midnight(before_spring, local_tz),
            datetime(2026, 3, 9, 4, tzinfo=UTC),
        )
        self.assertEqual(
            self.coordinator._next_local_midnight(before_fall, local_tz),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
        )

    def test_thermostat_metadata_filters_inactive_sensors_and_active_alerts(
        self,
    ) -> None:
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
            datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
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
        coordinator._async_record_error = lambda err: (
            self.coordinator.BeestatRuntimeDataCoordinator._async_record_error(
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
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
        event = types.ModuleType("homeassistant.helpers.event")
        update_coordinator = types.ModuleType(
            "homeassistant.helpers.update_coordinator"
        )
        aiohttp = types.ModuleType("aiohttp")

        core.HomeAssistant = object
        core.callback = lambda func: func
        exceptions.ConfigEntryAuthFailed = type(
            "ConfigEntryAuthFailed", (Exception,), {}
        )
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        event.async_call_later = lambda *_args, **_kwargs: lambda: None
        event.async_track_point_in_utc_time = lambda *_args, **_kwargs: lambda: None
        device_registry.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _device_id: None
        )
        entity_registry.async_get = lambda _hass: types.SimpleNamespace(entities={})
        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object

        helpers.device_registry = device_registry
        helpers.entity_registry = entity_registry
        helpers.update_coordinator = update_coordinator
        helpers.event = event
        homeassistant.core = core
        homeassistant.exceptions = exceptions
        homeassistant.helpers = helpers

        sys.modules["aiohttp"] = aiohttp
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.exceptions"] = exceptions
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
        sys.modules["homeassistant.helpers.event"] = event
        sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


class CoordinatorBoundaryReconcileTest(unittest.IsolatedAsyncioTestCase):
    """Validate pending boundary persistence across the real coordinator method."""

    setUp = CoordinatorHelpersTest.setUp
    tearDown = CoordinatorHelpersTest.tearDown
    _install_fake_homeassistant_modules = (
        CoordinatorHelpersTest._install_fake_homeassistant_modules
    )

    def _cached_coordinator(
        self,
        *,
        evaluated_at: datetime,
        data_end: datetime | None = None,
        latest_date: date | None = None,
        schedule: list[list[str]] | None = None,
    ):
        entry = types.SimpleNamespace(data={}, options={})
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=lambda _entity_id: None)
        )
        coordinator = object.__new__(self.coordinator.BeestatRuntimeDataCoordinator)
        coordinator.hass = hass
        coordinator.config_entry = entry
        coordinator._beestat_config_entry = entry
        coordinator._local_tz = ZoneInfo("America/New_York")
        coordinator._cancel_projection_boundary = None
        coordinator._client = types.SimpleNamespace(calls=[])
        coordinator.listener_updates = 0
        coordinator.async_update_listeners = lambda: setattr(
            coordinator,
            "listener_updates",
            coordinator.listener_updates + 1,
        )
        thermostat_row: dict[str, object] = {
            "id": 1,
            "name": "Zone A",
        }
        if data_end is not None:
            thermostat_row["data_end"] = data_end.isoformat()
        if schedule is not None:
            thermostat_row["timezone"] = "America/New_York"
            thermostat_row["program"] = {
                "currentClimateRef": "hold",
                "climates": [
                    {"climateRef": "hold", "name": "Hold"},
                    {"climateRef": "sleep", "name": "Sleep"},
                    {"climateRef": "home", "name": "Home"},
                ],
                "schedule": schedule,
            }
        summary_rows = (
            [
                {
                    "thermostat_id": 1,
                    "date": latest_date.isoformat(),
                    "sum_fan": 3600,
                }
            ]
            if latest_date is not None
            else []
        )
        coordinator.data = (
            self.coordinator.BeestatRuntimeDataCoordinator._build_runtime_data(
                coordinator,
                summary_rows,
                [thermostat_row],
                [],
                evaluated_at,
                evaluated_at,
                True,
                None,
                None,
                evaluated_at=evaluated_at,
                fetched_at=evaluated_at,
            )
        )
        return coordinator

    async def test_cached_projection_crosses_schedule_boundary_without_io(self) -> None:
        before = datetime(2026, 7, 1, 13, 55, tzinfo=UTC)
        boundary = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[3][20] = "home"
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            schedule=schedule,
        )
        old_fetched_at = coordinator.data.fetched_at
        self.assertEqual(
            coordinator.data.thermostat_metadata[1].current_climate_name,
            "Hold",
        )
        self.assertEqual(
            coordinator.data.thermostat_metadata[1].scheduled_climate_name,
            "Sleep",
        )

        self.coordinator.BeestatRuntimeDataCoordinator._async_rebuild_projection_from_cached(
            coordinator,
            boundary,
        )

        metadata = coordinator.data.thermostat_metadata[1]
        self.assertEqual(metadata.current_climate_name, "Hold")
        self.assertEqual(metadata.scheduled_climate_name, "Home")
        self.assertEqual(coordinator.data.fetched_at, old_fetched_at)
        self.assertEqual(coordinator.data.projected_at, boundary)
        self.assertEqual(coordinator._client.calls, [])
        self.assertEqual(coordinator.listener_updates, 1)

    async def test_cached_projection_crosses_cloud_stale_threshold(self) -> None:
        data_end = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        before = data_end + timedelta(minutes=120)
        boundary = data_end + timedelta(minutes=120, seconds=30, microseconds=1)
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            data_end=data_end,
        )
        self.assertEqual(
            coordinator.data.thermostat_metadata[1].data_lag_minutes,
            120,
        )

        self.coordinator.BeestatRuntimeDataCoordinator._async_rebuild_projection_from_cached(
            coordinator,
            boundary,
        )

        self.assertEqual(
            coordinator.data.thermostat_metadata[1].data_lag_minutes,
            121,
        )
        self.assertEqual(coordinator._client.calls, [])
        self.assertEqual(coordinator.listener_updates, 1)

    async def test_scheduler_selects_earliest_cached_projection_boundary(self) -> None:
        before = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[3][20] = "home"
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            data_end=datetime(2026, 7, 1, 11, 29, 59, 999999, tzinfo=UTC),
            schedule=schedule,
        )

        deadline = self.coordinator._next_projection_deadline(
            coordinator.data,
            before,
            coordinator._local_tz,
        )

        self.assertEqual(deadline, datetime(2026, 7, 1, 13, 30, 30, tzinfo=UTC))

    async def test_cached_projection_crosses_local_date_boundary(self) -> None:
        before = datetime(2026, 7, 6, 3, 59, tzinfo=UTC)
        midnight = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            latest_date=date(2026, 7, 4),
        )
        self.assertEqual(coordinator.data.thermostats[1].lag_days, 1)

        self.coordinator.BeestatRuntimeDataCoordinator._async_rebuild_projection_from_cached(
            coordinator,
            midnight,
        )

        self.assertEqual(coordinator.data.thermostats[1].lag_days, 2)
        self.assertEqual(coordinator.data.projected_at, midnight)
        self.assertEqual(coordinator._client.calls, [])
        self.assertEqual(coordinator.listener_updates, 1)

    async def test_unchanged_cached_projection_does_not_dispatch(self) -> None:
        before = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
        coordinator = self._cached_coordinator(evaluated_at=before)

        self.coordinator.BeestatRuntimeDataCoordinator._async_rebuild_projection_from_cached(
            coordinator,
            before + timedelta(minutes=5),
        )

        self.assertEqual(coordinator.data.projected_at, before)
        self.assertEqual(coordinator._client.calls, [])
        self.assertEqual(coordinator.listener_updates, 0)

    async def test_late_projection_callback_uses_actual_evaluation_time(self) -> None:
        before = datetime(2026, 7, 5, 3, 59, tzinfo=UTC)
        scheduled = datetime(2026, 7, 5, 4, 0, tzinfo=UTC)
        actual = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            latest_date=date(2026, 7, 3),
        )

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return actual.replace(tzinfo=None)
                return actual.astimezone(tz)

        original = self.coordinator.datetime
        self.coordinator.datetime = FrozenDateTime
        try:
            self.coordinator.BeestatRuntimeDataCoordinator._async_handle_projection_boundary(
                coordinator,
                scheduled,
            )
        finally:
            self.coordinator.datetime = original

        self.assertEqual(coordinator.data.projected_at, actual)
        self.assertEqual(coordinator.data.thermostats[1].lag_days, 3)

    async def test_source_refresh_replaces_projection_timer(self) -> None:
        before = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
        coordinator = self._cached_coordinator(evaluated_at=before)
        scheduled: list[datetime] = []
        cancelled: list[datetime] = []

        def track_point(_hass, _action, point_in_time):
            scheduled.append(point_in_time)

            def cancel() -> None:
                cancelled.append(point_in_time)

            return cancel

        original = self.coordinator.async_track_point_in_utc_time
        self.coordinator.async_track_point_in_utc_time = track_point
        try:
            self.coordinator.BeestatRuntimeDataCoordinator.async_set_updated_data(
                coordinator,
                coordinator.data,
            )
            self.coordinator.BeestatRuntimeDataCoordinator.async_set_updated_data(
                coordinator,
                coordinator.data,
            )
        finally:
            self.coordinator.async_track_point_in_utc_time = original

        self.assertEqual(len(scheduled), 2)
        self.assertEqual(cancelled, [scheduled[0]])

    async def test_projection_timer_is_registered_for_entry_unload(self) -> None:
        unload_callbacks = []
        entry = types.SimpleNamespace(
            async_on_unload=unload_callbacks.append,
        )
        coordinator = self.coordinator.BeestatRuntimeDataCoordinator(
            types.SimpleNamespace(),
            entry,
            types.SimpleNamespace(),
            local_tz=ZoneInfo("America/New_York"),
        )
        cancelled = []
        coordinator._cancel_projection_boundary = lambda: cancelled.append(True)

        projection_cleanup = next(
            callback
            for callback in unload_callbacks
            if callback.__name__ == "_async_cancel_projection_boundary"
        )
        projection_cleanup()

        self.assertEqual(cancelled, [True])
        self.assertIsNone(coordinator._cancel_projection_boundary)

    async def test_manual_refresh_sanitizes_unexpected_update_error(self) -> None:
        secret = "private-response-detail"
        recorded_errors: list[Exception] = []

        async def fetch_runtime_data(**_kwargs):
            raise RuntimeError(secret)

        coordinator = types.SimpleNamespace(
            _async_fetch_runtime_data=fetch_runtime_data,
            _client=types.SimpleNamespace(
                redact_error=lambda err: (
                    f"Unexpected integration error ({type(err).__name__})"
                )
            ),
            async_set_update_error=recorded_errors.append,
        )
        update_failed = sys.modules[
            "homeassistant.helpers.update_coordinator"
        ].UpdateFailed

        with self.assertRaises(update_failed) as raised:
            await self.coordinator.BeestatRuntimeDataCoordinator.async_refresh_runtime(
                coordinator
            )

        self.assertEqual(
            str(raised.exception),
            "Unexpected integration error (RuntimeError)",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(recorded_errors, [raised.exception])
        self.assertNotIn(secret, str(recorded_errors[0]))

    async def test_pending_boundary_finalizes_from_raw_runtime_rows(self) -> None:
        changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1001,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 7, 5),
            filter_changed_at=changed_at,
        )
        entry = types.SimpleNamespace(
            data={},
            options={
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_changed_at": changed_at.isoformat(),
                    }
                ]
            },
        )

        def update_entry(_entry, *, options):
            entry.options = options

        async def read_runtime(*_args):
            entry.options = {**entry.options, "concurrent_option": "preserved"}
            return [
                {"timestamp": "2026-07-05T21:40:00+00:00", "fan": 300},
                {"timestamp": "2026-07-05T21:45:00+00:00", "fan": 180},
                {"timestamp": "2026-07-05T21:50:00+00:00", "fan": 0},
            ]

        coordinator = types.SimpleNamespace(
            _client=types.SimpleNamespace(
                async_read_runtime_thermostat=read_runtime,
                redact_error=lambda err: str(err),
            ),
            _local_tz=ZoneInfo("America/New_York"),
            config_entry=entry,
            hass=types.SimpleNamespace(
                config_entries=types.SimpleNamespace(async_update_entry=update_entry)
            ),
            last_filter_boundary_pending_count=0,
            last_filter_boundary_reconciled_count=0,
            last_filter_boundary_reconcile_error=None,
            last_filter_boundary_reconcile_attempt_at=None,
            async_schedule_filter_boundary_reconcile=lambda *_args: None,
            _async_cancel_filter_boundary_retry=lambda: None,
        )

        await self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_pending_filter_boundaries(
            coordinator,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(),
            ),
        )

        saved = entry.options["thermostats"][0]
        self.assertEqual(saved["filter_change_day_runtime_baseline_seconds"], 480)
        self.assertEqual(
            saved["filter_change_boundary_source_data_end"],
            "2026-07-05T21:50:00+00:00",
        )
        self.assertEqual(entry.options["concurrent_option"], "preserved")
        self.assertEqual(coordinator.last_filter_boundary_reconciled_count, 1)
        self.assertEqual(coordinator.last_filter_boundary_pending_count, 0)

    async def test_slow_reconciliation_does_not_overwrite_newer_click(self) -> None:
        changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")
        newer_changed_at = datetime.fromisoformat("2026-07-05T22:12:00+00:00")
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1001,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 7, 5),
            filter_changed_at=changed_at,
        )
        entry = types.SimpleNamespace(
            data={},
            options={
                "unrelated_option": "preserve-me",
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_changed_at": changed_at.isoformat(),
                    }
                ],
            },
        )

        async def read_runtime(*_args):
            entry.options = {
                "unrelated_option": "newer-value",
                "thermostats": [
                    {
                        "id": 1001,
                        "filter_changed_date": "2026-07-05",
                        "filter_changed_at": newer_changed_at.isoformat(),
                    }
                ],
            }
            return [
                {"timestamp": "2026-07-05T21:45:00+00:00", "fan": 180},
                {"timestamp": "2026-07-05T21:50:00+00:00", "fan": 0},
            ]

        def update_entry(_entry, *, options):
            entry.options = options

        coordinator = types.SimpleNamespace(
            _client=types.SimpleNamespace(
                async_read_runtime_thermostat=read_runtime,
                redact_error=lambda err: str(err),
            ),
            _local_tz=ZoneInfo("America/New_York"),
            config_entry=entry,
            hass=types.SimpleNamespace(
                config_entries=types.SimpleNamespace(async_update_entry=update_entry)
            ),
            last_filter_boundary_pending_count=0,
            last_filter_boundary_reconciled_count=0,
            last_filter_boundary_reconcile_error=None,
            last_filter_boundary_reconcile_attempt_at=None,
            async_schedule_filter_boundary_reconcile=lambda *_args: None,
            _async_cancel_filter_boundary_retry=lambda: None,
        )

        await self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_pending_filter_boundaries(
            coordinator,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(),
            ),
        )

        saved = entry.options["thermostats"][0]
        self.assertEqual(saved["filter_changed_at"], newer_changed_at.isoformat())
        self.assertNotIn("filter_change_day_runtime_baseline_seconds", saved)
        self.assertNotIn("filter_change_boundary_reconciled_at", saved)
        self.assertEqual(entry.options["unrelated_option"], "newer-value")
        self.assertEqual(coordinator.last_filter_boundary_reconciled_count, 0)
        self.assertEqual(coordinator.last_filter_boundary_pending_count, 1)


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
