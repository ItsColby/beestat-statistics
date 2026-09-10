"""Tests for native sensor helper logic."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_sensor_test"


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
                "homeassistant.helpers.event",
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
        self.thermostat_settings = _load_module("thermostat_settings")
        self.profile = _load_module("profile")
        self.sensor = _load_module("sensor")
        self.filter_runtime = _load_module("filter_runtime")
        self.filter_forecast = _load_module("filter_forecast")

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
                        device_id="thermostat-device-id",
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
                        device_id="sensor-device-id",
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

    def test_filter_forecast_retains_crossed_runtime_threshold_date(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 8, 3),
            filter_changed_source="home_assistant",
            filter_runtime_hours=397.6,
            recent_runtime_hours_per_day=18.3,
            filter_runtime_threshold_date=date(2026, 8, 15),
        )

        forecast = self.sensor.build_filter_forecast(
            thermostat,
            summary,
            today=date(2026, 8, 24),
        )

        self.assertEqual(forecast.remaining_runtime_hours, 0.0)
        self.assertEqual(forecast.runtime_due_date, date(2026, 8, 15))
        self.assertEqual(forecast.due_date, date(2026, 8, 15))
        self.assertEqual(forecast.days_remaining, -9)
        self.assertTrue(forecast.due)

    def test_filter_forecast_retains_calendar_limit_when_runtime_date_overflows(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=100000,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 7, 1),
            filter_changed_source="native",
            filter_runtime_hours=0,
            recent_runtime_hours_per_day=0.01,
        )
        forecast = self.sensor.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertIsNone(forecast.runtime_due_date)
        self.assertEqual(forecast.due_date, date(2026, 9, 29))

        summary.filter_changed_date = date.max
        summary.recent_runtime_hours_per_day = 0
        forecast = self.sensor.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertIsNone(forecast.max_age_due_date)
        self.assertIsNone(forecast.due_date)

    def test_filter_forecast_already_consumed_runtime_is_due_while_hvac_idle(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1, slug="main", name="Main"
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 7, 1),
            filter_changed_source="native",
            filter_runtime_hours=250,
            recent_runtime_hours_per_day=0,
        )
        forecast = self.sensor.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertEqual(forecast.runtime_due_date, date(2026, 7, 5))
        self.assertTrue(forecast.due)

    def test_profile_numeric_fields_reject_booleans_and_fractional_minutes(
        self,
    ) -> None:
        program = {
            "climates": [
                {
                    "climateRef": "home",
                    "heatTemp": True,
                    "coolTemp": False,
                    "ventilatorMinOnTime": 2.5,
                }
            ]
        }
        profile = self.profile.schedule_profiles_by_ref(program)["home"]
        self.assertIsNone(profile.heat_temperature)
        self.assertIsNone(profile.cool_temperature)
        self.assertIsNone(profile.ventilator_min_on_time)
        for value, expected in ((True, None), (2.0, 2), ("3", 3), (-1, None)):
            with self.subTest(value=value):
                program["climates"][0]["ventilatorMinOnTime"] = value
                profile = self.profile.schedule_profiles_by_ref(program)["home"]
                self.assertEqual(profile.ventilator_min_on_time, expected)

    def test_filter_due_date_snapshot_is_atomic_and_content_revisioned(self) -> None:
        changed_at = datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_changed_at=changed_at,
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 6, 18),
            filter_changed_source="home_assistant",
            filter_runtime_hours=200,
            recent_runtime_hours_per_day=10,
        )
        data = types.SimpleNamespace(
            config=self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(),
            ),
            thermostats={1: summary},
            projected_at=datetime(2026, 7, 5, 12, tzinfo=UTC),
        )
        coordinator = types.SimpleNamespace(
            data=data,
            local_tz=ZoneInfo("America/New_York"),
        )

        snapshot = self.sensor._filter_forecast_snapshot_attributes(coordinator, 1)
        repeated = self.sensor._filter_forecast_snapshot_attributes(coordinator, 1)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot, repeated)
        self.assertEqual(snapshot["changed_at"], changed_at.isoformat())
        self.assertEqual(snapshot["runtime_hours"], 200)
        self.assertEqual(snapshot["remaining_runtime_hours"], 50.0)
        self.assertEqual(snapshot["runtime_due_date"], "2026-07-10")
        self.assertEqual(snapshot["max_age_due_date"], "2026-09-16")
        self.assertEqual(snapshot["due_date"], "2026-07-10")
        self.assertEqual(snapshot["days_remaining"], 5)
        original_revision = snapshot["forecast_revision"]

        data.thermostats[1] = types.SimpleNamespace(
            filter_changed_date=date(2026, 6, 18),
            filter_changed_source="home_assistant",
            filter_runtime_hours=201,
            recent_runtime_hours_per_day=10,
        )
        changed = self.sensor._filter_forecast_snapshot_attributes(coordinator, 1)

        self.assertIsNotNone(changed)
        assert changed is not None
        self.assertNotEqual(changed["forecast_revision"], original_revision)

    def test_fractional_boundary_retains_qualified_forecast_and_quality_revision(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_changed_at=datetime(2026, 6, 18, 14, 32, tzinfo=UTC),
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
        )
        observation = self.filter_runtime.FilterRuntimeObservation(
            200 * 3600,
            "complete",
            180,
            180,
            "finalized",
            datetime(2026, 7, 4, 23, 55, tzinfo=UTC),
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 6, 18),
            filter_changed_source="home_assistant",
            filter_runtime_hours=200,
            recent_runtime_hours_per_day=10,
            filter_runtime_observation=observation,
            recent_runtime_rate=self.filter_runtime.RecentRuntimeRate(
                10, date(2026, 6, 5), date(2026, 7, 4), 29, 1
            ),
        )
        forecast = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertEqual(forecast.runtime_due_date, date(2026, 7, 10))
        self.assertEqual(forecast.runtime_coverage, "complete")
        self.assertTrue(forecast.runtime_is_lower_bound)
        self.assertEqual(
            forecast.runtime_forecast_basis, "observed_lower_bound_projection"
        )
        self.assertTrue(forecast.remaining_runtime_hours_is_upper_bound)
        self.assertFalse(forecast.runtime_threshold_reached)
        self.assertEqual(forecast.recent_runtime_complete_days, 29)
        before = self.filter_forecast.filter_forecast_revision(forecast)
        summary.filter_runtime_observation = replace(
            observation,
            coverage="partial",
            unknown_interval_seconds=86400,
            boundary_status="source_gap",
        )
        after = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertEqual(after.runtime_hours, forecast.runtime_hours)
        self.assertEqual(after.runtime_due_date, forecast.runtime_due_date)
        self.assertNotEqual(
            self.filter_forecast.filter_forecast_revision(after), before
        )
        attributes = self.filter_forecast.filter_forecast_quality_attributes(after)
        self.assertEqual(attributes["boundary_status"], "source_gap")
        self.assertEqual(attributes["runtime_unknown_interval_minutes"], 1440)
        self.assertLessEqual(
            attributes.keys(), self.sensor.BeestatSensor._unrecorded_attributes
        )

    def test_fractional_lifetime_uses_unrounded_proof_and_rounds_remaining_up(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone",
            name="Zone",
            filter_lifetime_runtime_hours=0.01,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 7, 5),
            filter_changed_source="home_assistant",
            filter_runtime_hours=0.0,
            recent_runtime_hours_per_day=1,
            filter_runtime_observation=self.filter_runtime.FilterRuntimeObservation(
                35, "complete", 1, 0, "finalized", None
            ),
        )
        before = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 6)
        )
        self.assertEqual(before.remaining_runtime_hours, 0.1)
        self.assertTrue(before.remaining_runtime_hours_is_upper_bound)
        summary.filter_runtime_observation = replace(
            summary.filter_runtime_observation, observed_seconds=36
        )
        summary.filter_runtime_threshold_date = date(2026, 7, 5)
        after = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 6)
        )
        self.assertTrue(after.runtime_threshold_reached)
        self.assertEqual(after.remaining_runtime_hours, 0)
        self.assertEqual(after.runtime_due_date, date(2026, 7, 5))
        self.assertTrue(after.due)

    def test_forecast_revision_distinguishes_source_uncertainty_from_elapsed_tail(
        self,
    ) -> None:
        changed = datetime(2026, 7, 4, tzinfo=UTC)
        source_end = datetime(2026, 7, 4, 23, 55, tzinfo=UTC)
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone",
            name="Zone",
            filter_changed_at=changed,
            filter_lifetime_runtime_hours=250,
        )
        raw = self.filter_runtime.ChangeDayObservation(
            3600, 300, 0, "source_gap", None, source_end
        )

        def forecast(at: datetime, change_day=raw, horizon=source_end):
            observation = self.filter_runtime.build_filter_runtime_observation(
                [],
                changed_date=changed.date(),
                changed_at=changed,
                change_day=change_day,
                source_data_end=horizon,
                evaluated_at=at,
                local_tz=ZoneInfo("UTC"),
            )
            summary = types.SimpleNamespace(
                filter_changed_date=changed.date(),
                filter_changed_source="home_assistant",
                filter_runtime_hours=observation.observed_hours,
                recent_runtime_hours_per_day=None,
                filter_runtime_observation=observation,
            )
            return self.filter_forecast.build_filter_forecast(
                thermostat, summary, today=at.date()
            )

        first_at = datetime(2026, 7, 5, 12, tzinfo=UTC)
        first = forecast(first_at)
        later = forecast(first_at + timedelta(seconds=15))
        revision = self.filter_forecast.filter_forecast_revision(first)
        self.assertEqual(self.filter_forecast.filter_forecast_revision(later), revision)
        self.assertEqual(later.runtime_unknown_interval_minutes, 725.25)
        self.assertEqual(first.runtime_unknown_interval_minutes, 725)
        self.assertEqual(later.runtime_source_unknown_interval_minutes, 5)
        self.assertEqual(later.changed_at, changed)
        self.assertFalse(later.runtime_threshold_reached)

        variants = {
            "source_gap_duration": forecast(first_at, replace(raw, gap_seconds=600)),
            "source_horizon": forecast(
                first_at, horizon=source_end + timedelta(minutes=5)
            ),
            "coverage": forecast(
                first_at, replace(raw, gap_seconds=0, boundary_status="finalized")
            ),
            "observed_threshold": forecast(
                first_at, replace(raw, observed_seconds=250 * 3600)
            ),
        }
        for reason, value in variants.items():
            with self.subTest(reason=reason):
                self.assertNotEqual(
                    self.filter_forecast.filter_forecast_revision(value), revision
                )
        corrected = variants["source_gap_duration"]
        self.assertEqual(corrected.runtime_coverage, first.runtime_coverage)
        self.assertEqual(corrected.runtime_hours, first.runtime_hours)
        self.assertEqual(corrected.due_date, first.due_date)

        # Unreported time can invalidate a previous not-due proof. That is semantic.
        thermostat = replace(thermostat, filter_lifetime_runtime_hours=13.1)
        before_threshold = forecast(first_at)
        uncertain = forecast(first_at + timedelta(minutes=2))
        self.assertFalse(before_threshold.runtime_threshold_reached)
        self.assertIsNone(uncertain.runtime_threshold_reached)
        self.assertNotEqual(
            self.filter_forecast.filter_forecast_revision(before_threshold),
            self.filter_forecast.filter_forecast_revision(uncertain),
        )

    def test_lower_bound_proof_and_calendar_cap_are_independent_of_projection(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 6, 18),
            filter_changed_source="home_assistant",
            filter_runtime_hours=249.9,
            recent_runtime_hours_per_day=10,
            filter_runtime_observation=self.filter_runtime.FilterRuntimeObservation(
                249.99 * 3600, "partial", None, 180, "source_gap", None
            ),
        )
        forecast = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertIsNone(forecast.runtime_threshold_reached)
        self.assertIsNone(forecast.due)
        self.assertTrue(forecast.runtime_due_date_is_projection)
        self.assertEqual(forecast.max_age_due_date, date(2026, 9, 16))
        at_cap = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 9, 16)
        )
        self.assertTrue(at_cap.due)
        summary.filter_runtime_observation = replace(
            summary.filter_runtime_observation, observed_seconds=250 * 3600
        )
        proven = self.filter_forecast.build_filter_forecast(
            thermostat, summary, today=date(2026, 7, 5)
        )
        self.assertTrue(proven.runtime_threshold_reached)
        self.assertTrue(proven.due)
        self.assertFalse(proven.runtime_due_date_is_projection)

    def test_filter_forecast_preserves_unknown_runtime_on_replacement_date(
        self,
    ) -> None:
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
            filter_runtime_hours=None,
            filter_runtime_observation=self.filter_runtime.FilterRuntimeObservation(
                None, "unknown", None, 0, "pending_data", None
            ),
            recent_runtime_hours_per_day=15.4,
        )

        forecast = self.sensor.build_filter_forecast(
            thermostat,
            summary,
            today=date(2026, 7, 5),
        )

        self.assertIsNone(forecast.runtime_hours)
        self.assertIsNone(forecast.remaining_runtime_hours)
        self.assertIsNone(forecast.runtime_due_date)
        self.assertIsNone(forecast.due)
        self.assertFalse(forecast.due_soon)

    def test_filter_forecast_uses_click_boundary_runtime_on_replacement_date(
        self,
    ) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=250,
            filter_max_age_days=90,
            filter_notice_days=7,
            filter_change_day_runtime_baseline_seconds=28800,
        )
        summary = types.SimpleNamespace(
            filter_changed_date=date(2026, 7, 5),
            filter_changed_source="home_assistant",
            filter_runtime_hours=2.0,
            recent_runtime_hours_per_day=15.4,
        )

        forecast = self.sensor.build_filter_forecast(
            thermostat,
            summary,
            today=date(2026, 7, 5),
        )

        self.assertEqual(forecast.runtime_hours, 2.0)
        self.assertEqual(forecast.remaining_runtime_hours, 248.0)
        self.assertEqual(forecast.runtime_due_date, date(2026, 7, 21))
        self.assertFalse(forecast.due)
        self.assertFalse(forecast.due_soon)

    def test_active_alert_category_separates_maintenance_from_equipment(self) -> None:
        self.assertEqual(
            self.sensor.classify_active_alerts(
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
            self.sensor.classify_active_alerts(
                ({"text": "System fault: not cooling"},)
            ),
            "equipment",
        )
        self.assertEqual(self.sensor.classify_active_alerts(()), "none")

    def test_maintenance_alert_does_not_hide_an_unknown_or_equipment_alert(
        self,
    ) -> None:
        maintenance = {"text": "Replace the filter"}
        unknown = {"code": "unrecognized_code"}
        equipment = {"text": "System fault: not cooling"}
        self.assertEqual(
            self.sensor.classify_active_alerts((maintenance, unknown)), "unknown"
        )
        self.assertEqual(
            self.sensor.classify_active_alerts((equipment, maintenance, unknown)),
            "equipment",
        )

    def test_entity_surface_keeps_primary_entities_and_classifies_details(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
        )
        descriptions = {
            description.translation_key: description
            for description in self.sensor._thermostat_sensor_descriptions(
                thermostat=thermostat
            )
        }

        active_count = descriptions["active_sensor_count"]
        self.assertEqual(
            "Beestat-reported in-use sensor count",
            active_count.name,
        )
        unknown_count = types.SimpleNamespace(
            data=types.SimpleNamespace(
                thermostat_metadata={
                    1: types.SimpleNamespace(active_sensor_count=None),
                }
            )
        )
        self.assertTrue(active_count.available_fn(unknown_count))
        self.assertIsNone(active_count.value_fn(unknown_count))
        unknown_count.data.thermostat_metadata[1].active_sensor_count = 0
        self.assertTrue(active_count.available_fn(unknown_count))
        unknown_count.data.thermostat_metadata = {}
        self.assertFalse(active_count.available_fn(unknown_count))

        for key in (
            "scheduled_comfort_profile",
            "next_scheduled_comfort_profile_time",
            "filter_due_date",
            "filter_days_remaining",
        ):
            self.assertIsNone(descriptions[key].entity_category, key)
        for key in (
            "current_comfort_profile",
            "runtime_summary_latest_date",
            "runtime_summary_lag_days",
            "active_sensor_count",
            "current_profile_room_temperature_spread",
            "compressor_minimum_off_time",
            "compressor_minimum_outdoor_temperature",
            "heat_cool_minimum_delta",
            "hold_action",
            "temperature_correction",
            "heating_differential",
            "cooling_differential",
            "heating_dissipation_time",
            "cooling_dissipation_time",
            "hot_temperature_alert",
            "cold_temperature_alert",
            "high_humidity_alert",
            "low_humidity_alert",
            "last_service_date",
            "service_reminder_date",
            "service_reminder_interval",
            "playback_volume",
            "filter_runtime_hours",
            "filter_recent_runtime_hours_per_day",
            "filter_remaining_runtime_hours",
            "filter_runtime_due_date",
            "filter_max_age_due_date",
        ):
            self.assertEqual("diagnostic", descriptions[key].entity_category, key)
        for key in (
            "compressor_minimum_off_time",
            "compressor_minimum_outdoor_temperature",
            "heat_cool_minimum_delta",
            "hold_action",
            "temperature_correction",
            "heating_differential",
            "cooling_differential",
            "heating_dissipation_time",
            "cooling_dissipation_time",
            "hot_temperature_alert",
            "cold_temperature_alert",
            "high_humidity_alert",
            "low_humidity_alert",
            "last_service_date",
            "service_reminder_date",
            "service_reminder_interval",
            "playback_volume",
        ):
            self.assertFalse(descriptions[key].entity_registry_enabled_default, key)
        self.assertEqual(
            "Configured profile room temperature spread",
            descriptions["current_profile_room_temperature_spread"].name,
        )
        for key in (
            "current_profile_room_temperature_spread",
            "heat_cool_minimum_delta",
            "temperature_correction",
            "heating_differential",
            "cooling_differential",
        ):
            self.assertEqual("temperature_delta", descriptions[key].device_class, key)
        for key in (
            "compressor_minimum_off_time",
            "compressor_minimum_outdoor_temperature",
            "heat_cool_minimum_delta",
            "temperature_correction",
            "heating_differential",
            "cooling_differential",
            "heating_dissipation_time",
            "cooling_dissipation_time",
            "hot_temperature_alert",
            "cold_temperature_alert",
            "high_humidity_alert",
            "low_humidity_alert",
            "service_reminder_interval",
            "playback_volume",
        ):
            self.assertIsNone(descriptions[key].state_class, key)
        for key in ("high_humidity_alert", "low_humidity_alert"):
            self.assertEqual("humidity", descriptions[key].device_class, key)
        self.assertEqual(
            "_filter_forecast_snapshot_attributes",
            descriptions["filter_due_date"].extra_attributes_fn.func.__name__,
        )

    def test_selected_settings_are_typed_and_disabled_values_are_unavailable(
        self,
    ) -> None:
        snapshot = self.thermostat_settings.ThermostatSettingsSnapshot(
            thermostat_id=1,
            source_details={},
            settings={
                "tempCorrection": -5,
                "humidityHighAlert": 70,
                "humidityLowAlert": -1,
                "lastServiceDate": "2026-06-18",
            },
            audio={"playbackVolume": 55},
        )
        coordinator = types.SimpleNamespace(
            data=types.SimpleNamespace(thermostat_settings={1: snapshot})
        )

        self.assertEqual(
            -0.5,
            self.sensor._thermostat_setting_value(
                coordinator, 1, "tempCorrection", "temperature"
            ),
        )
        self.assertEqual(
            70,
            self.sensor._thermostat_setting_value(
                coordinator, 1, "humidityHighAlert", "nonnegative_integer"
            ),
        )
        self.assertIsNone(
            self.sensor._thermostat_setting_value(
                coordinator, 1, "humidityLowAlert", "nonnegative_integer"
            )
        )
        self.assertEqual(
            date(2026, 6, 18),
            self.sensor._thermostat_setting_value(
                coordinator, 1, "lastServiceDate", "date"
            ),
        )
        self.assertEqual(
            55,
            self.sensor._thermostat_setting_value(
                coordinator, 1, "playbackVolume", "audio_integer"
            ),
        )

    def test_spread_attributes_name_configured_membership_without_breaking_legacy(
        self,
    ) -> None:
        projection = types.SimpleNamespace(
            participating_sensor_count=2,
            valid_sensor_count=2,
            participating_sensor_names=("Bedroom", "Office"),
            unavailable_sensor_names=(),
            hottest_sensor_name="Office",
            coldest_sensor_name="Bedroom",
        )
        coordinator = types.SimpleNamespace(
            data=types.SimpleNamespace(
                room_temperature_spreads={1: projection},
                thermostat_metadata={
                    1: types.SimpleNamespace(
                        current_climate_name="Home", current_climate_ref="home"
                    )
                },
                metadata_sync_success_at=datetime(2026, 7, 5, 12, tzinfo=UTC),
            )
        )

        attributes = self.sensor._room_temperature_spread_attributes(coordinator, 1)
        self.assertEqual(2, attributes["configured_sensor_count"])
        self.assertEqual(["Bedroom", "Office"], attributes["configured_sensor_names"])
        self.assertEqual(2, attributes["participating_sensor_count"])
        self.assertEqual("Home", attributes["profile_name"])
        self.assertEqual("home", attributes["profile_ref"])
        self.assertEqual("2026-07-05T12:00:00+00:00", attributes["metadata_synced_at"])
        self.assertLessEqual(
            {"profile_name", "profile_ref", "metadata_synced_at"},
            self.sensor.BeestatSensor._unrecorded_attributes,
        )

    def test_spread_missing_metadata_does_not_claim_profile_or_coverage(self) -> None:
        coordinator = types.SimpleNamespace(data=None)
        self.assertIsNone(
            self.sensor._room_temperature_spread_attributes(coordinator, 1)
        )
        coordinator.data = types.SimpleNamespace(
            room_temperature_spreads={},
            thermostat_metadata={},
            metadata_sync_success_at=None,
        )
        self.assertIsNone(
            self.sensor._room_temperature_spread_attributes(coordinator, 1)
        )

    def test_spread_unit_follows_recovered_and_updated_projection(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1, slug="main", name="Main"
        )
        coordinator = types.SimpleNamespace(
            hass=object(),
            data=types.SimpleNamespace(room_temperature_spreads={}),
        )
        description = next(
            description
            for description in self.sensor._thermostat_sensor_descriptions(
                thermostat=thermostat
            )
            if description.translation_key == "current_profile_room_temperature_spread"
        )
        entity = self.sensor.BeestatSensor(coordinator, description, None)
        self.assertIsNone(entity.native_unit_of_measurement)
        self.assertFalse(entity.available)
        for unit, value in (("°C", 2.0), ("°F", 3.6)):
            with self.subTest(unit=unit):
                coordinator.data.room_temperature_spreads[1] = types.SimpleNamespace(
                    unit=unit, value=value
                )
                self.assertEqual(entity.native_unit_of_measurement, unit)
                self.assertEqual(entity.native_value, value)
                self.assertTrue(entity.available)

    def test_active_alert_examples_are_bounded_for_entity_state(self) -> None:
        alerts = tuple(
            {
                "code": str(index),
                "type": "t" * 200 if index == 0 else "thermostat",
                "severity": "low",
                "timestamp": f"2026-07-0{index + 1} 12:00:00",
                "text": (
                    "Replace private filter"
                    if index == 0
                    else f"Private room detail {index}"
                ),
                "guid": f"private-alert-guid-{index}",
            }
            for index in range(5)
        )

        self.assertEqual(
            self.sensor.active_alert_examples(alerts),
            [
                {
                    "category": "maintenance",
                    "code": "0",
                    "type": "t" * 96,
                    "severity": "low",
                    "timestamp": "2026-07-01 12:00:00",
                },
                {
                    "category": "unknown",
                    "code": "1",
                    "type": "thermostat",
                    "severity": "low",
                    "timestamp": "2026-07-02 12:00:00",
                },
                {
                    "category": "unknown",
                    "code": "2",
                    "type": "thermostat",
                    "severity": "low",
                    "timestamp": "2026-07-03 12:00:00",
                },
            ],
        )

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
        event = types.ModuleType("homeassistant.helpers.event")
        update_coordinator = types.ModuleType(
            "homeassistant.helpers.update_coordinator"
        )

        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        config_entries.ConfigEntry = _Subscriptable
        const.UnitOfTime = types.SimpleNamespace(
            DAYS="d",
            HOURS="h",
            MINUTES="min",
        )
        const.UnitOfTemperature = types.SimpleNamespace(FAHRENHEIT="F")
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
        event.async_call_later = lambda *_args, **_kwargs: lambda: None
        event.async_track_point_in_utc_time = lambda *_args, **_kwargs: lambda: None
        update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator
        update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
        update_coordinator.CoordinatorEntity = _FakeCoordinatorEntity
        sensor.SensorDeviceClass = types.SimpleNamespace(
            DATE="date",
            DURATION="duration",
            HUMIDITY="humidity",
            TEMPERATURE="temperature",
            TEMPERATURE_DELTA="temperature_delta",
            TIMESTAMP="timestamp",
        )
        sensor.SensorEntity = object
        sensor.SensorEntityDescription = FakeSensorEntityDescription
        sensor.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")

        components.sensor = sensor
        helpers.device_registry = device_registry
        helpers.entity = entity
        helpers.entity_platform = entity_platform
        helpers.event = event
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
        sys.modules["homeassistant.helpers.event"] = event
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
