"""Tests for Beestat coordinator interpretation helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_coordinator_test"


class _FakeTranslatedHomeAssistantError(Exception):
    """Minimal translated Home Assistant exception used by pure unit tests."""

    def __init__(
        self,
        *args: object,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(*args or ((translation_key,) if translation_key else ()))
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


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
            self.coordinator._sum_fan_seconds(
                [
                    {"sum_fan": 3600},
                    {"sum_fan": "NaN"},
                    {"sum_fan": "Infinity"},
                ]
            ),
            3600,
        )

    def test_runtime_projections_reject_unrepresentable_finite_totals(self) -> None:
        """Finite source fields cannot overflow cached runtime projections."""

        rows = [
            {"date": "2026-07-01", "sum_fan": 1e308},
            {"date": "2026-07-02", "sum_fan": 1e308},
        ]

        self.assertIsNone(self.coordinator._sum_fan_seconds(rows))

    def test_negative_or_boolean_runtime_cannot_reduce_filter_usage(self) -> None:
        self.assertEqual(
            self.coordinator._sum_fan_seconds(
                [
                    {"sum_fan": 3600},
                    {"sum_fan": -1800},
                    {"sum_fan": "-7200"},
                    {"sum_fan": True},
                ]
            ),
            3600,
        )

    def test_profile_room_spread_uses_mapped_local_values_and_rejects_unknown(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            temperature_entity_id="sensor.zone_a_temperature",
        )
        sensors = (
            self.config_model.ConfiguredSensor(
                sensor_id=10,
                slug="zone_a_sensor",
                name="Zone A (HomeKit)",
                thermostat_id=1,
                thermostat_slug="zone_a",
                include_temperature=True,
                include_air_quality=False,
                include_co2=False,
                include_voc=False,
                temperature_entity_id="sensor.zone_a_temperature",
            ),
            self.config_model.ConfiguredSensor(
                sensor_id=11,
                slug="room_a",
                name="Room A (HomeKit)",
                thermostat_id=1,
                thermostat_slug="zone_a",
                include_temperature=True,
                include_air_quality=False,
                include_co2=False,
                include_voc=False,
                temperature_entity_id="sensor.room_a_temperature",
            ),
            self.config_model.ConfiguredSensor(
                sensor_id=12,
                slug="room_b",
                name="Room B",
                thermostat_id=1,
                thermostat_slug="zone_a",
                include_temperature=True,
                include_air_quality=False,
                include_co2=False,
                include_voc=False,
                temperature_entity_id="sensor.room_b_temperature",
            ),
        )
        state_values = {
            "sensor.zone_a_temperature": types.SimpleNamespace(
                state="76",
                attributes={"unit_of_measurement": "°F"},
            ),
            "sensor.room_a_temperature": types.SimpleNamespace(
                state="24",
                attributes={"unit_of_measurement": "°C"},
            ),
            "sensor.room_b_temperature": types.SimpleNamespace(
                state="unknown",
                attributes={"unit_of_measurement": "°F"},
            ),
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=state_values.get),
            config=types.SimpleNamespace(
                units=types.SimpleNamespace(temperature_unit="°F")
            ),
        )
        metadata = {
            1: types.SimpleNamespace(
                current_profile_sensors=(
                    self.coordinator.ProfileSensorReference("ei:0:1", "Zone A"),
                    self.coordinator.ProfileSensorReference("rs:101:1", "Room A"),
                    self.coordinator.ProfileSensorReference("rs:101:1", "Renamed"),
                    self.coordinator.ProfileSensorReference("rs:102:1", "Room B"),
                )
            )
        }
        sensor_metadata = {
            10: self.coordinator.SensorMetadata(
                sensor_id=10,
                thermostat_id=1,
                name="Zone A",
                identifier="ei:0",
                sensor_type="thermostat",
                in_use=True,
                inactive=False,
                deleted=False,
            ),
            11: self.coordinator.SensorMetadata(
                sensor_id=11,
                thermostat_id=1,
                name="Room A",
                identifier="rs:101",
                sensor_type="ecobee3_remote_sensor",
                in_use=True,
                inactive=False,
                deleted=False,
            ),
            12: self.coordinator.SensorMetadata(
                sensor_id=12,
                thermostat_id=1,
                name="Room B",
                identifier="rs:102",
                sensor_type="ecobee3_remote_sensor",
                in_use=False,
                inactive=False,
                deleted=False,
            ),
        }

        projections = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            metadata,
            sensor_metadata,
        )

        projection = projections[1]
        self.assertEqual(projection.value, 0.8)
        self.assertEqual(projection.valid_sensor_count, 2)
        self.assertEqual(projection.participating_sensor_count, 3)
        self.assertEqual(projection.unavailable_sensor_names, ("Room B",))
        self.assertEqual(projection.hottest_sensor_name, "Zone A (HomeKit)")
        self.assertEqual(projection.coldest_sensor_name, "Room A (HomeKit)")
        self.assertEqual(
            projection.participating_sensor_names,
            ("Zone A (HomeKit)", "Room A (HomeKit)", "Room B"),
        )

        for device_class in ("temperature_delta", "humidity", None, ""):
            with self.subTest(device_class=device_class):
                state_values["sensor.room_b_temperature"] = types.SimpleNamespace(
                    state="3",
                    attributes={
                        "device_class": device_class,
                        "unit_of_measurement": "°C",
                    },
                )
                invalid_class = self.coordinator._build_room_temperature_spreads(
                    hass,
                    self.config_model.BeestatConfig(
                        thermostats=(thermostat,),
                        sensors=sensors,
                    ),
                    metadata,
                    sensor_metadata,
                )[1]
                self.assertEqual(invalid_class, projection)

        state_values["sensor.room_b_temperature"] = types.SimpleNamespace(
            state="78",
            attributes={"unit_of_measurement": "°F"},
        )
        recovered = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            metadata,
            sensor_metadata,
        )[1]
        self.assertEqual(recovered.value, 2.8)
        self.assertEqual(recovered.valid_sensor_count, 3)
        self.assertEqual(recovered.unavailable_sensor_names, ())

    def test_temperature_state_accepts_absolute_and_classless_supported_units(
        self,
    ) -> None:
        for value, source_unit, target_unit, expected in (
            (22, "°C", "°F", 71.6),
            (71.6, "°F", "°C", 22),
            (295.15, "K", "°C", 22),
        ):
            for class_attributes in ({}, {"device_class": "temperature"}):
                with self.subTest(unit=source_unit, attributes=class_attributes):
                    state = types.SimpleNamespace(
                        state=str(value),
                        attributes={
                            **class_attributes,
                            "unit_of_measurement": source_unit,
                        },
                    )
                    reading = self.coordinator._temperature_state_value(
                        state, target_unit
                    )
                    self.assertIsNotNone(reading)
                    self.assertAlmostEqual(reading[0], expected)
                    self.assertEqual(reading[1], target_unit)

    def test_temperature_state_rejects_overflow_after_unit_conversion(self) -> None:
        state = types.SimpleNamespace(
            state="1e308",
            attributes={"unit_of_measurement": "°C"},
        )

        self.assertIsNone(self.coordinator._temperature_state_value(state, "°F"))
        self.assertEqual(
            self.coordinator._temperature_state_value(state, "°C"),
            (1e308, "°C"),
        )

    def test_absolute_temperature_bounds_preserve_roundoff_and_valid_extremes(
        self,
    ) -> None:
        for unit, zero, negative in (
            ("K", 0.0, None),
            ("°C", -273.15, -40),
            ("°F", -459.67, -40),
        ):
            for value in (zero, zero - 5e-10, zero + 5e-10, negative, 1e308):
                if value is None:
                    continue
                with self.subTest(unit=unit, valid=value):
                    state = types.SimpleNamespace(
                        state=str(value), attributes={"unit_of_measurement": unit}
                    )
                    self.assertEqual(
                        self.coordinator._temperature_state_value(state, unit),
                        (value, unit),
                    )
            for value in (zero - 1, zero - 1e-8, float("inf"), float("nan")):
                with self.subTest(unit=unit, invalid=value):
                    state = types.SimpleNamespace(
                        state=str(value), attributes={"unit_of_measurement": unit}
                    )
                    self.assertIsNone(
                        self.coordinator._temperature_state_value(state, "°F")
                    )
        state = types.SimpleNamespace(
            state="-459.7", attributes={"unit_of_measurement": "°F"}
        )
        self.assertIsNone(self.coordinator._temperature_state_value(state, "°F"))

    def test_profile_room_spread_preserves_equal_names_and_fails_ambiguous_identity(
        self,
    ) -> None:
        sensors = tuple(
            self.config_model.ConfiguredSensor(
                sensor_id=sensor_id,
                slug=f"room_{sensor_id}",
                name="Shared name",
                thermostat_id=1,
                thermostat_slug="zone",
                include_temperature=True,
                include_air_quality=False,
                include_co2=False,
                include_voc=False,
                temperature_entity_id=f"sensor.room_{sensor_id}",
            )
            for sensor_id in (10, 11)
        )
        state_values = {
            "sensor.room_10": types.SimpleNamespace(
                state="70",
                attributes={"unit_of_measurement": "°F"},
            ),
            "sensor.room_11": types.SimpleNamespace(
                state="74",
                attributes={"unit_of_measurement": "°F"},
            ),
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=state_values.get),
            config=types.SimpleNamespace(
                units=types.SimpleNamespace(temperature_unit="°F")
            ),
        )
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone",
            name="Zone",
        )
        thermostat_metadata = {
            1: types.SimpleNamespace(
                current_profile_sensors=(
                    self.coordinator.ProfileSensorReference("rs:10:1", "First"),
                    self.coordinator.ProfileSensorReference("rs:11:1", "Second"),
                )
            )
        }
        sensor_metadata = {
            sensor_id: self.coordinator.SensorMetadata(
                sensor_id=sensor_id,
                thermostat_id=1,
                name="Shared name",
                identifier=f"rs:{sensor_id}",
                sensor_type="ecobee3_remote_sensor",
                in_use=True,
                inactive=False,
                deleted=False,
            )
            for sensor_id in (10, 11)
        }

        projection = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            thermostat_metadata,
            sensor_metadata,
        )[1]

        self.assertEqual(projection.value, 4)
        self.assertEqual(projection.participating_sensor_count, 2)
        self.assertEqual(projection.valid_sensor_count, 2)

        state_values["sensor.room_10"].state = "-1e308"
        state_values["sensor.room_11"].state = "1e308"
        invalid_temperature = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            thermostat_metadata,
            sensor_metadata,
        )[1]
        self.assertEqual(invalid_temperature.valid_sensor_count, 1)
        self.assertEqual(invalid_temperature.unavailable_sensor_names, ("Shared name",))
        self.assertIsNone(invalid_temperature.value)
        state_values["sensor.room_10"].state = "70"
        state_values["sensor.room_11"].state = "74"

        sensor_metadata[20] = self.coordinator.SensorMetadata(
            sensor_id=20,
            thermostat_id=2,
            name="Other thermostat room",
            identifier="rs:10",
            sensor_type="ecobee3_remote_sensor",
            in_use=True,
            inactive=False,
            deleted=False,
        )
        cross_thermostat_collision = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            thermostat_metadata,
            sensor_metadata,
        )[1]
        self.assertEqual(cross_thermostat_collision.value, 4)
        self.assertEqual(cross_thermostat_collision.valid_sensor_count, 2)

        sensor_metadata[12] = self.coordinator.SensorMetadata(
            sensor_id=12,
            thermostat_id=1,
            name="Ambiguous",
            identifier="rs:10",
            sensor_type="ecobee3_remote_sensor",
            in_use=True,
            inactive=False,
            deleted=False,
        )
        ambiguous = self.coordinator._build_room_temperature_spreads(
            hass,
            self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=sensors,
            ),
            thermostat_metadata,
            sensor_metadata,
        )[1]
        self.assertEqual(ambiguous.valid_sensor_count, 1)
        self.assertIsNone(ambiguous.value)
        self.assertEqual(ambiguous.unavailable_sensor_names, ("First",))

    def test_cached_rows_use_one_last_effective_identity(self) -> None:
        """Duplicate cloud identities cannot create duplicate entities or totals."""

        thermostat_rows = self.coordinator._effective_resource_rows(
            [
                {"id": 1, "name": "Old"},
                {"id": "invalid", "name": "Ignored"},
                {"id": float("inf"), "name": "Unrepresentable"},
                {"id": 1, "name": "Current"},
                {"id": 2, "name": "Removed"},
                {"id": 2, "deleted": True},
                {"id": 3, "deleted": True},
                {"id": 3, "name": "Restored"},
            ],
            "thermostat_id",
            "id",
        )
        summary_rows = self.coordinator._effective_summary_rows(
            [
                {"thermostat_id": 1, "date": "2026-07-01", "sum_fan": 3600},
                {"thermostat_id": 1, "date": "2026-07-01", "sum_fan": 7200},
                {"thermostat_id": 2, "date": "2026-07-01", "sum_fan": 3600},
                {"thermostat_id": 2, "date": "2026-07-01", "deleted": True},
                {"thermostat_id": 3, "date": "2026-07-01", "deleted": True},
                {"thermostat_id": 3, "date": "2026-07-01", "sum_fan": 1800},
                {"thermostat_id": 1, "date": "invalid", "sum_fan": 9999},
            ]
        )

        self.assertEqual(
            thermostat_rows,
            ({"id": 1, "name": "Current"}, {"id": 3, "name": "Restored"}),
        )
        self.assertEqual(
            summary_rows,
            (
                {"thermostat_id": 1, "date": "2026-07-01", "sum_fan": 7200},
                {"thermostat_id": 3, "date": "2026-07-01", "sum_fan": 1800},
            ),
        )

    def test_malformed_source_ids_cannot_replace_real_resource_rows(self) -> None:
        rows = [{"id": 1, "name": "Real"}]
        rows.extend({"id": value, "name": "Malformed"} for value in (True, 1.5, 0, -1))
        self.assertEqual(
            self.coordinator._effective_resource_rows(rows, "id"),
            ({"id": 1, "name": "Real"},),
        )
        self.assertEqual(self.coordinator._row_int({"id": 1.0}, "id"), 1)
        self.assertEqual(
            self.coordinator._row_int(
                {"thermostat_id": True, "id": "2"}, "thermostat_id", "id"
            ),
            2,
        )

    def test_projection_change_ignores_local_date_without_sensitive_state(self) -> None:
        projected_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
        config = self.config_model.BeestatConfig(thermostats=(), sensors=())
        current = types.SimpleNamespace(
            config=config,
            thermostats={},
            thermostat_metadata={},
            projected_at=projected_at,
        )
        projected = types.SimpleNamespace(
            config=config,
            thermostats={},
            thermostat_metadata={},
            projected_at=projected_at,
        )

        self.assertFalse(
            self.coordinator._projection_changed(
                current,
                projected,
                ZoneInfo("America/New_York"),
                ZoneInfo("Europe/London"),
            )
        )
        self.assertFalse(
            self.coordinator._projection_changed(
                current,
                projected,
                ZoneInfo("Europe/London"),
                ZoneInfo("Europe/Paris"),
            )
        )

    def test_projection_change_compares_filter_forecasts_across_dates(self) -> None:
        projected_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            filter_changed_date=date(2026, 6, 1),
            filter_max_age_days=30,
        )
        config = self.config_model.BeestatConfig(
            thermostats=(thermostat,),
            sensors=(),
        )
        summary = self.coordinator.ThermostatRuntimeSummary(
            thermostat_id=1,
            slug="zone_a",
            label="Zone A",
            latest_date=None,
            lag_days=None,
            filter_changed_date=date(2026, 6, 1),
            filter_changed_source="home_assistant",
            filter_runtime_hours=0.0,
            recent_runtime_hours_per_day=None,
        )
        current = types.SimpleNamespace(
            config=config,
            thermostats={1: summary},
            thermostat_metadata={},
            projected_at=projected_at,
        )

        self.assertTrue(
            self.coordinator._projection_changed(
                current,
                current,
                ZoneInfo("America/New_York"),
                ZoneInfo("Europe/London"),
            )
        )

    def test_runtime_time_zone_update_cancels_rebuilds_and_ignores_noop(self) -> None:
        calls: list[object] = []
        coordinator = types.SimpleNamespace(
            _local_tz=ZoneInfo("America/New_York"),
            _timezone_revision=0,
            _async_cancel_projection_boundary=lambda: calls.append("cancel"),
            _async_rebuild_projection_from_cached=lambda now, **kwargs: calls.append(
                (now.tzinfo, kwargs)
            ),
        )

        self.coordinator.BeestatRuntimeDataCoordinator.async_update_local_timezone(
            coordinator,
            ZoneInfo("Europe/London"),
        )
        self.coordinator.BeestatRuntimeDataCoordinator.async_update_local_timezone(
            coordinator,
            ZoneInfo("Europe/London"),
        )

        self.assertEqual(coordinator._local_tz, ZoneInfo("Europe/London"))
        self.assertEqual(coordinator._timezone_revision, 1)
        self.assertEqual(calls[0], "cancel")
        self.assertEqual(calls[1][0], UTC)
        self.assertEqual(
            calls[1][1],
            {"previous_local_tz": ZoneInfo("America/New_York")},
        )
        self.assertEqual(len(calls), 2)

    def test_summary_refresh_retries_when_timezone_changes_local_day(
        self,
    ) -> None:
        evaluated_at = datetime(2026, 7, 1, 1, 0, tzinfo=UTC)
        summary_calls: list[tuple[str, str]] = []
        built: list[tuple[date | None, ZoneInfo, datetime]] = []
        coordinator = object.__new__(self.coordinator.BeestatRuntimeDataCoordinator)
        coordinator._refresh_lock = asyncio.Lock()
        coordinator._closed = False
        coordinator.data = None
        coordinator._local_tz = ZoneInfo("America/New_York")
        coordinator._timezone_revision = 0
        coordinator._beestat_config_entry = types.SimpleNamespace(data={}, options={})
        coordinator.config_entry = coordinator._beestat_config_entry
        coordinator.hass = types.SimpleNamespace()

        async def read_id(_resource):
            return []

        async def read_summary(start, end):
            summary_calls.append((start, end))
            if len(summary_calls) == 1:
                coordinator._local_tz = ZoneInfo("Europe/London")
                coordinator._timezone_revision += 1
            return []

        async def reconcile(_config, *_args):
            coordinator._local_tz = ZoneInfo("Asia/Tokyo")
            coordinator._timezone_revision += 1

        def build_runtime_data(
            _self,
            _rows,
            _thermostat_rows,
            _sensor_rows,
            _sync_success_at,
            _metadata_sync_success_at,
            _summary_rows_full,
            _summary_window_start,
            summary_window_end,
            *,
            temporal_context,
            fetched_at=None,
            thermostat_settings=None,
        ):
            built.append(
                (
                    summary_window_end,
                    temporal_context.local_tz,
                    temporal_context.evaluated_at,
                )
            )
            return types.SimpleNamespace(summary_window_end=summary_window_end)

        coordinator._client = types.SimpleNamespace(
            async_read_id=read_id,
            async_read_runtime_thermostat_summary=read_summary,
        )
        coordinator._async_reconcile_pending_filter_boundaries = reconcile
        coordinator._summary_window_start = lambda _config, _rows, today: today
        coordinator._build_runtime_data = types.MethodType(
            build_runtime_data,
            coordinator,
        )

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return evaluated_at.replace(tzinfo=None)
                return evaluated_at.astimezone(tz)

        original_datetime = self.coordinator.datetime
        original_build_config = self.coordinator.build_beestat_config
        self.coordinator.datetime = FrozenDateTime
        self.coordinator.build_beestat_config = lambda *_args, **_kwargs: (
            self.config_model.BeestatConfig(thermostats=(), sensors=())
        )
        try:
            result = asyncio.run(
                self.coordinator.BeestatRuntimeDataCoordinator._async_fetch_runtime_data(
                    coordinator,
                    skip_sync=True,
                    summary_window=True,
                )
            )
        finally:
            self.coordinator.datetime = original_datetime
            self.coordinator.build_beestat_config = original_build_config

        self.assertEqual(
            summary_calls,
            [("2026-06-30", "2026-06-30"), ("2026-07-01", "2026-07-01")],
        )
        self.assertEqual(result.summary_window_end, date(2026, 7, 1))
        self.assertEqual(
            built,
            [(date(2026, 7, 1), ZoneInfo("Asia/Tokyo"), evaluated_at)],
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

    def test_unrepresentable_utc_timestamps_do_not_break_source_projection(
        self,
    ) -> None:
        for value in (
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.coordinator._parse_datetime(value))

    def test_invalid_timezone_keys_use_configured_fallback(self) -> None:
        fallback = ZoneInfo("America/New_York")
        for value in ("/etc/localtime", "../UTC", "missing/timezone", "UTC/../UTC"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.coordinator._row_timezone({"timezone": value}, fallback),
                    fallback,
                )

    def test_exact_boundary_date_tracks_timezone_without_mutating_saved_date(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone",
            name="Zone",
            filter_changed_date=date(2026, 7, 4),
            filter_changed_at=datetime(2026, 7, 5, 1, tzinfo=UTC),
        )
        coordinator = types.SimpleNamespace(_local_tz=ZoneInfo("Asia/Tokyo"))
        self.assertEqual(
            self.coordinator.BeestatRuntimeDataCoordinator._filter_changed_date(
                coordinator, thermostat, {}
            ),
            (date(2026, 7, 5), "home_assistant"),
        )
        self.assertEqual(thermostat.filter_changed_date, date(2026, 7, 4))

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

    def test_current_profile_uses_ecobee_program_sensor_identity(self) -> None:
        current_ref, current_name, sensors = self.coordinator._current_profile(
            {
                "program": {
                    "currentClimateRef": "home",
                    "climates": [
                        {"climateRef": "away", "name": "Away"},
                        {
                            "climateRef": "home",
                            "name": "Home",
                            "sensors": [
                                {"id": "rs:103:1", "name": "Room Sensor C"},
                                {"id": "rs:104:1", "name": "Room Sensor B"},
                            ],
                        },
                    ],
                }
            }
        )

        self.assertEqual(current_ref, "home")
        self.assertEqual(current_name, "Home")
        self.assertEqual(
            sensors,
            (
                self.coordinator.ProfileSensorReference("rs:103:1", "Room Sensor C"),
                self.coordinator.ProfileSensorReference("rs:104:1", "Room Sensor B"),
            ),
        )

    def test_schedule_snapshot_finds_current_and_next_profile(self) -> None:
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[2][20] = "home"
        snapshot = self.coordinator._schedule_snapshot(
            {
                "timezone": "America/New_York",
                "program": {
                    "climates": [
                        {
                            "climateRef": "sleep",
                            "name": "Sleep",
                            "isOccupied": False,
                            "heatTemp": 67.0,
                            "coolTemp": 74.0,
                            "heatFan": "on",
                            "coolFan": "auto",
                            "isOptimized": True,
                            "sensors": [{"name": "Bedroom"}],
                        },
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
        sleep = snapshot["profiles"][0]
        self.assertEqual(sleep.heat_temperature, 67.0)
        self.assertEqual(sleep.cool_temperature, 74.0)
        self.assertEqual(sleep.heat_fan, "on")
        self.assertEqual(sleep.cool_fan, "auto")
        self.assertTrue(sleep.is_optimized)
        self.assertEqual(sleep.sensors, ("Bedroom",))

    def test_duplicate_profiles_share_last_row_for_current_and_schedule(self) -> None:
        profiles = [
            {
                "climateRef": "home",
                "name": "Earlier Home",
                "sensors": [{"id": "rs:10:1", "name": "Earlier Room"}],
            },
            {
                "climateRef": "home",
                "name": "Later Home",
                "sensors": [
                    {"id": "rs:11:1", "name": "Same Room"},
                    {"id": "rs:12:1", "name": "Same Room"},
                    {"id": "rs:13:1"},
                    {"name": "Named Room"},
                    {},
                    None,
                ],
            },
        ]
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1, slug="zone_a", name="Zone A"
        )
        for climates in (profiles, list(reversed(profiles))):
            winner = climates[-1]
            with self.subTest(winner=winner["name"]):
                row = {
                    "thermostat_id": 1,
                    "program": {
                        "currentClimateRef": "home",
                        "climates": [None, {}, *climates],
                        "schedule": [["home"] * 48 for _ in range(7)],
                    },
                }
                metadata = self.coordinator._build_thermostat_metadata(
                    (row,),
                    {},
                    datetime(2026, 7, 1, 13, 15, tzinfo=UTC),
                    ZoneInfo("UTC"),
                    (thermostat,),
                )[1]
                expected_sensors = tuple(
                    self.coordinator.ProfileSensorReference(
                        sensor.get("id"), sensor.get("name")
                    )
                    for sensor in winner["sensors"]
                    if isinstance(sensor, dict) and sensor
                )
                self.assertEqual(metadata.current_climate_name, winner["name"])
                self.assertEqual(metadata.scheduled_climate_name, winner["name"])
                self.assertEqual(metadata.current_profile_sensors, expected_sensors)
                self.assertEqual(len(metadata.schedule_profiles), 1)
                self.assertEqual(
                    metadata.schedule_profiles[0].sensor_references, expected_sensors
                )
                self.assertEqual(
                    metadata.schedule_profiles[0].sensors,
                    tuple(sensor.name for sensor in expected_sensors if sensor.name),
                )

    def test_current_profile_preserves_unknown_reference_fallback(self) -> None:
        for program in (
            {"currentClimateRef": "unavailable"},
            {"currentClimateRef": "unavailable", "climates": []},
            {"currentClimateRef": "unavailable", "climates": [None, {}]},
        ):
            with self.subTest(program=program):
                self.assertEqual(
                    self.coordinator._current_profile({"program": program}),
                    ("unavailable", "unavailable", ()),
                )

    def test_schedule_snapshot_skips_nonexistent_spring_forward_slots(self) -> None:
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[6][4] = "home"
        schedule[6][5] = "home"
        schedule[6][8] = "away"

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

    def test_schedule_snapshot_does_not_invent_fall_back_transition(self) -> None:
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[6][2] = "home"
        schedule[6][3] = "home"
        schedule[6][4] = "away"

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
            datetime(2026, 11, 1, 5, 15, tzinfo=UTC),
            ZoneInfo("America/New_York"),
        )

        self.assertEqual(snapshot["scheduled_ref"], "home")
        self.assertEqual(snapshot["next_ref"], "away")
        self.assertEqual(snapshot["next_at"], datetime(2026, 11, 1, 7, tzinfo=UTC))

    def test_cloud_stale_rounding_boundary_is_exact(self) -> None:
        data_end = datetime(2026, 7, 1, 12, tzinfo=UTC)
        threshold = timedelta(minutes=120, seconds=30)

        self.assertEqual(
            self.coordinator._lag_minutes(data_end + threshold, data_end),
            120,
        )
        self.assertEqual(
            self.coordinator._lag_minutes(
                data_end + threshold + timedelta(microseconds=1), data_end
            ),
            121,
        )
        self.assertEqual(
            self.coordinator._cloud_data_stale_deadline(data_end, 120),
            data_end + threshold + timedelta(microseconds=1),
        )

        self.assertEqual(
            self.coordinator.cloud_data_stale_threshold_minutes(21600),
            420,
        )
        self.assertEqual(
            self.coordinator.cloud_data_stale_threshold_minutes(300),
            120,
        )

    def test_extreme_future_source_timestamp_cannot_overflow_stale_deadline(self):
        self.assertEqual(
            self.coordinator._cloud_data_stale_deadline(
                datetime.max.replace(tzinfo=UTC), 120
            ),
            datetime.max.replace(tzinfo=UTC),
        )

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

    def test_next_local_midnight_uses_first_valid_instant_after_midnight_gap(
        self,
    ) -> None:
        local_tz = ZoneInfo("Africa/Cairo")

        boundary = self.coordinator._next_local_midnight(
            datetime(2026, 4, 23, 12, tzinfo=UTC),
            local_tz,
        )

        self.assertEqual(boundary, datetime(2026, 4, 23, 22, tzinfo=UTC))
        self.assertEqual(
            boundary.astimezone(local_tz),
            datetime(2026, 4, 24, 1, tzinfo=local_tz),
        )

    def test_thermostat_metadata_filters_inactive_deleted_sensors_and_alerts(
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
            12: self.coordinator.SensorMetadata(
                sensor_id=12,
                thermostat_id=1,
                name="Room Sensor D",
                identifier="room_sensor_d",
                sensor_type="ecobee3_remote_sensor",
                in_use=True,
                inactive=False,
                deleted=True,
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

    def test_in_use_metadata_fails_closed_when_missing_or_invalid(self) -> None:
        sensor_metadata = self.coordinator._build_sensor_metadata(
            (
                {
                    "id": 10,
                    "thermostat_id": 1,
                    "name": "Room Sensor A",
                    "in_use": "not-a-boolean",
                },
                {
                    "id": 11,
                    "thermostat_id": 1,
                    "name": "Room Sensor B",
                    "in_use": "false",
                },
            )
        )
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
        )

        metadata = self.coordinator._build_thermostat_metadata(
            ({"id": 1},),
            sensor_metadata,
            datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
            ZoneInfo("America/New_York"),
            (thermostat,),
        )[1]

        self.assertIsNone(sensor_metadata[10].in_use)
        self.assertFalse(sensor_metadata[11].in_use)
        self.assertIsNone(metadata.active_sensor_count)
        self.assertEqual((), metadata.active_sensor_names)

        empty_snapshot = self.coordinator._build_thermostat_metadata(
            ({"id": 1},),
            {},
            datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
            ZoneInfo("America/New_York"),
            (thermostat,),
        )[1]
        self.assertEqual(0, empty_snapshot.active_sensor_count)
        self.assertEqual((), empty_snapshot.active_sensor_names)

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
            "ConfigEntryAuthFailed", (_FakeTranslatedHomeAssistantError,), {}
        )
        update_coordinator.UpdateFailed = type(
            "UpdateFailed", (_FakeTranslatedHomeAssistantError,), {}
        )
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

    def _owned_coordinator(self):
        tasks = []
        callbacks = []

        def create_task(_hass, coroutine, _name):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        entry = types.SimpleNamespace(
            async_on_unload=callbacks.append,
            async_create_background_task=create_task,
        )
        hass = types.SimpleNamespace()
        coordinator = self.coordinator.BeestatRuntimeDataCoordinator(
            hass,
            entry,
            types.SimpleNamespace(),
            local_tz=ZoneInfo("America/New_York"),
        )
        coordinator.hass = hass
        return coordinator, tasks, callbacks

    async def test_refresh_requests_serialize_and_publish_in_acquisition_order(self):
        coordinator, tasks, _callbacks = self._owned_coordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        published = []

        async def fetch(**_kwargs):
            sequence = len(calls) + 1
            calls.append(sequence)
            if sequence == 1:
                started.set()
                await release.wait()
            return sequence

        coordinator._async_fetch_runtime_data_locked = fetch
        coordinator.async_set_updated_data = published.append
        first = asyncio.create_task(coordinator.async_refresh_runtime())
        await started.wait()
        second = asyncio.create_task(coordinator.async_refresh_runtime(skip_sync=True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(calls, [1])
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [1, 2])
        self.assertEqual(published, [1, 2])
        self.assertEqual(len(tasks), 2)

    async def test_periodic_refresh_shares_the_manual_acquisition_lock(self):
        coordinator, _tasks, _callbacks = self._owned_coordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fetch(**_kwargs):
            sequence = len(calls) + 1
            calls.append(sequence)
            if sequence == 1:
                started.set()
                await release.wait()
            return sequence

        coordinator._async_fetch_runtime_data_locked = fetch
        coordinator.async_set_updated_data = lambda _data: None
        first = asyncio.create_task(coordinator._async_update_data())
        await started.wait()
        second = asyncio.create_task(coordinator.async_refresh_runtime())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(calls, [1])
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [1, 2])

    async def test_entry_task_cancellation_prevents_refresh_publication(self):
        coordinator, tasks, callbacks = self._owned_coordinator()
        started = asyncio.Event()
        published = []

        async def fetch(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        coordinator._async_fetch_runtime_data_locked = fetch
        coordinator.async_set_updated_data = published.append
        request = asyncio.create_task(coordinator.async_refresh_runtime())
        await started.wait()
        for task in tasks:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        for close in callbacks:
            close()
        with self.assertRaises(asyncio.CancelledError):
            await coordinator.async_refresh_runtime()
        with self.assertRaises(asyncio.CancelledError):
            await coordinator.async_dismiss_filter_alerts(1)
        self.assertEqual(published, [])
        self.assertIsNone(coordinator.last_error)
        self.assertFalse(coordinator._refresh_lock.locked())
        self.assertEqual(len(tasks), 1)

    async def test_framework_refresh_schedules_projection_after_publication(self):
        coordinator, _tasks, _callbacks = self._owned_coordinator()
        schedules = []
        coordinator._async_schedule_projection_boundary = lambda: schedules.append(True)
        coordinator.last_update_success = True
        coordinator._async_refresh_finished()
        coordinator.last_update_success = False
        coordinator._async_refresh_finished()
        coordinator.last_update_success = True
        coordinator._async_mark_closed()
        coordinator._async_refresh_finished()
        self.assertEqual(schedules, [True])

    def _attach_temporal_context(self, coordinator) -> None:
        coordinator._async_reconcile_filter_boundary = types.MethodType(
            self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_filter_boundary,
            coordinator,
        )
        coordinator.capture_temporal_context = lambda: self.coordinator.TemporalContext(
            datetime.now(UTC),
            coordinator._local_tz,
            coordinator._timezone_revision,
        )
        coordinator.temporal_context_is_current = lambda context: (
            context.timezone_revision == coordinator._timezone_revision
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
        coordinator._timezone_revision = 0
        coordinator._cancel_projection_boundary = None
        coordinator._client = types.SimpleNamespace()
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

    async def test_normalized_integral_ids_match_summary_and_metadata(self) -> None:
        now = datetime(2026, 7, 1, 16, tzinfo=UTC)
        coordinator = self._cached_coordinator(evaluated_at=now)
        data = coordinator._build_runtime_data(
            [
                {
                    "thermostat_id": 1.0,
                    "date": "2026-06-30",
                    "sum_fan": 3600,
                    "count": 288,
                }
            ],
            [{"id": 1.0, "name": "Zone", "data_end": now.isoformat()}],
            [],
            now,
            now,
            True,
            None,
            None,
            evaluated_at=now,
        )
        self.assertEqual(data.thermostats[1].latest_date, date(2026, 6, 30))
        self.assertEqual(data.thermostats[1].recent_runtime_hours_per_day, 1)
        self.assertEqual(data.thermostat_metadata[1].data_end, now)

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
        self.assertEqual(coordinator.listener_updates, 1)

    async def test_scheduler_selects_earliest_cached_projection_boundary(self) -> None:
        before = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[2][20] = "home"
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            data_end=datetime(2026, 7, 1, 11, 29, 59, 999999, tzinfo=UTC),
            schedule=schedule,
        )

        deadline = self.coordinator._next_projection_deadline(
            coordinator.data,
            coordinator._local_tz,
        )

        self.assertEqual(deadline, datetime(2026, 7, 1, 13, 30, 30, tzinfo=UTC))

    async def test_scheduler_selects_filter_uncertainty_before_cloud_staleness(
        self,
    ) -> None:
        before = datetime(2026, 7, 1, 13, tzinfo=UTC)
        coordinator = self._cached_coordinator(
            evaluated_at=before, data_end=before - timedelta(seconds=300)
        )
        data = coordinator.data
        thermostat = replace(
            data.config.thermostats[0], filter_lifetime_runtime_hours=1
        )
        observation = self.coordinator.FilterRuntimeObservation(
            3500, "complete", 0, 0, "finalized", before - timedelta(seconds=300)
        )
        data = replace(
            data,
            config=replace(data.config, thermostats=(thermostat,)),
            thermostats={
                1: replace(data.thermostats[1], filter_runtime_observation=observation)
            },
        )

        self.assertEqual(
            self.coordinator._next_projection_deadline(data, coordinator._local_tz),
            before + timedelta(seconds=100),
        )
        for observed, unknown in ((3500, 100), (3600, 0)):
            with self.subTest(observed=observed, unknown=unknown):
                projected = replace(
                    data,
                    projected_at=before + timedelta(seconds=100),
                    thermostats={
                        1: replace(
                            data.thermostats[1],
                            filter_runtime_observation=replace(
                                observation,
                                observed_seconds=observed,
                                unknown_interval_seconds=unknown,
                            ),
                        )
                    },
                )
                self.assertEqual(
                    self.coordinator._next_projection_deadline(
                        projected, coordinator._local_tz
                    ),
                    before + timedelta(minutes=115, seconds=30, microseconds=1),
                )

    async def test_scheduler_rebuilds_boundary_crossed_during_registration(
        self,
    ) -> None:
        before = datetime(2026, 7, 1, 13, 59, 59, 900000, tzinfo=UTC)
        after = datetime(2026, 7, 1, 14, 0, 0, 100000, tzinfo=UTC)
        schedule = [["sleep"] * 48 for _ in range(7)]
        schedule[2][20] = "home"
        coordinator = self._cached_coordinator(
            evaluated_at=before,
            schedule=schedule,
        )

        self.assertEqual(
            self.coordinator._next_projection_deadline(
                coordinator.data,
                coordinator._local_tz,
            ),
            datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
        )

        with patch.object(
            self.coordinator,
            "async_track_point_in_utc_time",
            return_value=lambda: None,
        ) as track_point:
            coordinator._async_schedule_projection_boundary(
                coordinator.data,
                now=after,
            )

        self.assertEqual(
            coordinator.data.thermostat_metadata[1].scheduled_climate_name,
            "Home",
        )
        self.assertEqual(coordinator.data.projected_at, after)
        self.assertEqual(coordinator.listener_updates, 1)
        self.assertEqual(
            track_point.call_args.args[2],
            datetime(2026, 7, 1, 14, 30, tzinfo=UTC),
        )

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
        self.assertEqual(coordinator.listener_updates, 1)

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
            await self.coordinator.BeestatRuntimeDataCoordinator._async_refresh_runtime(
                coordinator,
                skip_sync=False,
                summary_window=False,
            )

        self.assertEqual(raised.exception.translation_domain, "beestat_statistics")
        self.assertEqual(raised.exception.translation_key, "beestat_request_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(recorded_errors, [raised.exception])
        self.assertNotIn(secret, str(recorded_errors[0]))

    async def test_periodic_refresh_translates_auth_failure(self) -> None:
        secret = "private-auth-response-detail"

        async def fetch_runtime_data(**_kwargs):
            raise self.coordinator.BeestatAuthError(secret)

        coordinator = types.SimpleNamespace(
            _async_fetch_runtime_data=fetch_runtime_data,
            _client=types.SimpleNamespace(redact_error=lambda err: str(err)),
        )
        auth_failed = sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed

        with self.assertRaises(auth_failed) as raised:
            await self.coordinator.BeestatRuntimeDataCoordinator._async_update_data(
                coordinator
            )

        self.assertEqual(raised.exception.translation_domain, "beestat_statistics")
        self.assertEqual(raised.exception.translation_key, "beestat_auth_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(secret, str(raised.exception))

    async def test_periodic_refresh_translates_unexpected_failure(self) -> None:
        secret = "private-response-detail"

        async def fetch_runtime_data(**_kwargs):
            raise RuntimeError(secret)

        coordinator = types.SimpleNamespace(
            _async_fetch_runtime_data=fetch_runtime_data,
            _client=types.SimpleNamespace(redact_error=lambda err: str(err)),
        )
        update_failed = sys.modules[
            "homeassistant.helpers.update_coordinator"
        ].UpdateFailed

        with self.assertRaises(update_failed) as raised:
            await self.coordinator.BeestatRuntimeDataCoordinator._async_update_data(
                coordinator
            )

        self.assertEqual(raised.exception.translation_domain, "beestat_statistics")
        self.assertEqual(raised.exception.translation_key, "beestat_request_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(secret, str(raised.exception))

    async def test_boundary_revalidates_source_corrections_with_bounded_cached_reads(
        self,
    ) -> None:
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
        updates = []
        calls = []

        def update_entry(_entry, *, options):
            entry.options = options
            updates.append(options)

        start = datetime(2026, 7, 5, 4, tzinfo=UTC)
        raw_rows = [
            {"timestamp": (start + timedelta(minutes=5 * i)).isoformat(), "fan": 0}
            for i in range(212)
        ] + [
            {"timestamp": "2026-07-05T21:40:00+00:00", "fan": 300},
            {"timestamp": "2026-07-05T21:45:00+00:00", "fan": 180},
            {"timestamp": "2026-07-05T21:50:00+00:00", "fan": 0},
        ]

        async def read_runtime(*args):
            calls.append(args)
            entry.options = {**entry.options, "concurrent_option": "preserved"}
            return [dict(row) for row in raw_rows]

        coordinator = types.SimpleNamespace(
            _client=types.SimpleNamespace(
                async_read_runtime_thermostat=read_runtime,
                redact_error=lambda err: str(err),
            ),
            _local_tz=ZoneInfo("America/New_York"),
            _timezone_revision=0,
            config_entry=entry,
            hass=types.SimpleNamespace(
                config_entries=types.SimpleNamespace(async_update_entry=update_entry)
            ),
            async_schedule_filter_boundary_reconcile=lambda *_args: None,
            _async_cancel_filter_boundary_retry=lambda: None,
        )
        self._attach_temporal_context(coordinator)
        context = self.coordinator.TemporalContext(
            datetime(2026, 7, 7, 12, tzinfo=UTC), coordinator._local_tz, 0
        )
        coordinator.capture_temporal_context = lambda: context
        config = self.config_model.BeestatConfig(thermostats=(thermostat,), sensors=())
        summaries = [
            {"thermostat_id": 1001, "date": "2026-07-05", "count": 215, "sum_fan": 480}
        ]
        metadata = ({"id": 1001, "data_end": "2026-07-05T21:50:00+00:00"},)
        reconcile = types.MethodType(
            self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_pending_filter_boundaries,
            coordinator,
        )
        await reconcile(config, summaries, metadata)
        saved = entry.options["thermostats"][0]
        self.assertEqual(saved["filter_change_day_runtime_baseline_seconds"], 480)
        self.assertEqual(
            saved["filter_change_boundary_source_data_end"], "2026-07-05T21:50:00+00:00"
        )
        self.assertEqual(
            calls, [(1001, "2026-07-05T04:00:00+00:00", "2026-07-06T04:00:00+00:00")]
        )
        config = replace(
            config,
            thermostats=(
                replace(
                    thermostat,
                    filter_change_day_runtime_baseline_seconds=480,
                    filter_change_boundary_reconciled_at=datetime.fromisoformat(
                        saved["filter_change_boundary_reconciled_at"]
                    ),
                ),
            ),
        )
        await reconcile(config, summaries, metadata)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(updates), 1)
        raw_rows[0]["fan"] = 120
        summaries[0]["sum_fan"] = 600
        await reconcile(config, summaries, metadata)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            entry.options["thermostats"][0][
                "filter_change_day_runtime_baseline_seconds"
            ],
            600,
        )
        # Same-count/sum point redistribution is caught by the bounded periodic reread.
        raw_rows[0]["fan"] = 0
        raw_rows[-1]["fan"] = 120
        context = replace(
            context, evaluated_at=context.evaluated_at + timedelta(hours=6)
        )
        await reconcile(config, summaries, metadata)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            entry.options["thermostats"][0][
                "filter_change_day_runtime_baseline_seconds"
            ],
            480,
        )
        self.assertEqual(
            entry.options["thermostats"][0]["filter_changed_at"], changed_at.isoformat()
        )
        raw_rows[0]["deleted"] = True
        summaries[0]["count"] = 214
        await reconcile(config, summaries, metadata)
        self.assertNotIn(
            "filter_change_day_runtime_baseline_seconds",
            entry.options["thermostats"][0],
        )
        self.assertEqual(coordinator.last_filter_boundary_pending_count, 1)
        self.assertEqual(entry.options["concurrent_option"], "preserved")
        self.assertEqual(len(coordinator._filter_day_cache), 1)

    async def test_cleared_boundary_cannot_be_restored_by_slow_reconciliation(
        self,
    ) -> None:
        changed_at = datetime(2026, 7, 5, tzinfo=UTC)
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone",
            name="Zone",
            filter_changed_date=changed_at.date(),
            filter_changed_at=changed_at,
        )
        entry = types.SimpleNamespace(
            data={},
            options={
                "thermostats": [{"id": 1, "filter_changed_at": changed_at.isoformat()}]
            },
        )

        async def read_runtime(*_args):
            entry.options = {"thermostats": [{"id": 1}]}
            return [{"timestamp": changed_at.isoformat(), "fan": 60}]

        updates = []
        coordinator = types.SimpleNamespace(
            _filter_day_cache={},
            _client=types.SimpleNamespace(async_read_runtime_thermostat=read_runtime),
            _local_tz=ZoneInfo("UTC"),
            _timezone_revision=0,
            config_entry=entry,
            hass=types.SimpleNamespace(
                config_entries=types.SimpleNamespace(
                    async_update_entry=lambda *args, **kwargs: updates.append(kwargs)
                )
            ),
        )
        self._attach_temporal_context(coordinator)
        pending = await coordinator._async_reconcile_filter_boundary(
            thermostat, coordinator.capture_temporal_context(), [], ()
        )
        self.assertFalse(pending)
        self.assertEqual(updates, [])
        self.assertEqual(coordinator._filter_day_cache, {})

    async def test_timezone_change_prevents_stale_boundary_persistence(self) -> None:
        async def check_boundary(change_during_read):
            changed_at = datetime.fromisoformat("2026-07-05T01:48:00+00:00")
            thermostat = self.config_model.ConfiguredThermostat(
                thermostat_id=1001,
                slug="zone_a",
                name="Zone A",
                filter_changed_date=date(2026, 7, 4),
                filter_changed_at=changed_at,
            )
            entry = types.SimpleNamespace(
                data={},
                options={
                    "thermostats": [
                        {
                            "id": 1001,
                            "filter_changed_date": "2026-07-04",
                            "filter_changed_at": changed_at.isoformat(),
                        }
                    ]
                },
            )

            async def read_runtime(*_args):
                if change_during_read:
                    coordinator._local_tz = ZoneInfo("Europe/London")
                    coordinator._timezone_revision += 1
                start = datetime(2026, 7, 4, 4, tzinfo=UTC)
                return [
                    {
                        "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                        "fan": 0,
                    }
                    for index in range(261)
                ] + [
                    {"timestamp": "2026-07-05T01:45:00+00:00", "fan": 180},
                    {"timestamp": "2026-07-05T01:50:00+00:00", "fan": 0},
                ]

            updates: list[dict[str, object]] = []
            coordinator = types.SimpleNamespace(
                _client=types.SimpleNamespace(
                    async_read_runtime_thermostat=read_runtime,
                    redact_error=lambda err: str(err),
                ),
                _local_tz=ZoneInfo("America/New_York"),
                _timezone_revision=0,
                config_entry=entry,
                hass=types.SimpleNamespace(
                    config_entries=types.SimpleNamespace(
                        async_update_entry=lambda _entry, *, options: updates.append(
                            options
                        )
                    )
                ),
                last_filter_boundary_pending_count=0,
                last_filter_boundary_reconciled_count=0,
                last_filter_boundary_reconcile_error=None,
                last_filter_boundary_reconcile_attempt_at=None,
                async_schedule_filter_boundary_reconcile=lambda *_args: None,
                _async_cancel_filter_boundary_retry=lambda: None,
            )

            self._attach_temporal_context(coordinator)
            coordinator.capture_temporal_context = lambda: (
                self.coordinator.TemporalContext(
                    datetime(2026, 7, 7, 12, tzinfo=UTC),
                    coordinator._local_tz,
                    coordinator._timezone_revision,
                )
            )

            await self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_pending_filter_boundaries(
                coordinator,
                self.config_model.BeestatConfig(
                    thermostats=(thermostat,),
                    sensors=(),
                ),
            )

            if change_during_read:
                self.assertEqual(updates, [])
            else:
                self.assertEqual(len(updates), 1)
                self.assertEqual(
                    updates[0]["thermostats"][0][
                        "filter_change_day_runtime_baseline_seconds"
                    ],
                    180,
                )
            self.assertEqual(
                coordinator.last_filter_boundary_reconciled_count,
                0 if change_during_read else 1,
            )
            self.assertEqual(
                coordinator.last_filter_boundary_pending_count,
                1 if change_during_read else 0,
            )

        for change_during_read in (False, True):
            with self.subTest(change_during_read=change_during_read):
                await check_boundary(change_during_read)

    async def test_slow_reconciliation_does_not_overwrite_newer_click(self) -> None:
        async def check_boundary(change_during_read):
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
                if change_during_read:
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
                start = datetime(2026, 7, 5, 4, tzinfo=UTC)
                return [
                    {
                        "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                        "fan": 0,
                    }
                    for index in range(213)
                ] + [
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
                _timezone_revision=0,
                config_entry=entry,
                hass=types.SimpleNamespace(
                    config_entries=types.SimpleNamespace(
                        async_update_entry=update_entry
                    )
                ),
                last_filter_boundary_pending_count=0,
                last_filter_boundary_reconciled_count=0,
                last_filter_boundary_reconcile_error=None,
                last_filter_boundary_reconcile_attempt_at=None,
                async_schedule_filter_boundary_reconcile=lambda *_args: None,
                _async_cancel_filter_boundary_retry=lambda: None,
            )

            self._attach_temporal_context(coordinator)
            coordinator.capture_temporal_context = lambda: (
                self.coordinator.TemporalContext(
                    datetime(2026, 7, 7, 12, tzinfo=UTC),
                    coordinator._local_tz,
                    coordinator._timezone_revision,
                )
            )

            await self.coordinator.BeestatRuntimeDataCoordinator._async_reconcile_pending_filter_boundaries(
                coordinator,
                self.config_model.BeestatConfig(
                    thermostats=(thermostat,),
                    sensors=(),
                ),
            )

            saved = entry.options["thermostats"][0]
            expected_changed_at = newer_changed_at if change_during_read else changed_at
            self.assertEqual(
                saved["filter_changed_at"], expected_changed_at.isoformat()
            )
            if change_during_read:
                self.assertNotIn("filter_change_day_runtime_baseline_seconds", saved)
                self.assertNotIn("filter_change_boundary_reconciled_at", saved)
            else:
                self.assertEqual(
                    saved["filter_change_day_runtime_baseline_seconds"], 180
                )
                self.assertIn("filter_change_boundary_reconciled_at", saved)
            self.assertEqual(
                entry.options["unrelated_option"],
                "newer-value" if change_during_read else "preserve-me",
            )
            self.assertEqual(
                coordinator.last_filter_boundary_reconciled_count,
                0 if change_during_read else 1,
            )
            self.assertEqual(
                coordinator.last_filter_boundary_pending_count,
                1 if change_during_read else 0,
            )

        for change_during_read in (False, True):
            with self.subTest(change_during_read=change_during_read):
                await check_boundary(change_during_read)


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
