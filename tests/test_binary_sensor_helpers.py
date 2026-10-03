"""Tests for native binary sensor helper logic."""

from __future__ import annotations

import types
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from custom_components.beestat_statistics import (
    binary_sensor,
    config_model,
    coordinator,
    entity,
)


class BinarySensorHelpersTest(unittest.TestCase):
    """Validate dependency-light binary sensor behavior."""

    def setUp(self) -> None:
        self.config_model = config_model
        self.coordinator = coordinator
        self.binary_sensor = binary_sensor
        self.enterContext(
            patch.object(
                entity.dr,
                "async_get",
                return_value=types.SimpleNamespace(
                    async_get=lambda device_id: types.SimpleNamespace(id=device_id)
                ),
            )
        )

    def test_binary_sensors_separate_advisory_and_problem_states(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            device_id="thermostat-device-id",
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
            device_id="sensor-device-id",
        )
        data = self.coordinator.BeestatRuntimeData(
            config=self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(sensor,),
            ),
            fetched_at=datetime(2026, 7, 5, tzinfo=UTC),
            projected_at=datetime(2026, 7, 5, tzinfo=UTC),
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
                    data_end=datetime(2026, 7, 5, 11, tzinfo=UTC),
                    data_lag_minutes=480,
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
            thermostat_settings={
                1: self.coordinator.ThermostatSettingsSnapshot(
                    thermostat_id=1,
                    source_details={},
                    settings={
                        "autoAway": True,
                        "followMeComfort": False,
                        "smartCirculation": True,
                        "disablePreHeating": False,
                        "disablePreCooling": True,
                        "hotTempAlertEnabled": True,
                        "coldTempAlertEnabled": False,
                        "wifiOfflineAlert": True,
                        "serviceRemindMe": True,
                    },
                    audio={"microphoneEnabled": False},
                )
            },
        )
        fake_coordinator = _FakeCoordinator(data)
        fake_coordinator.last_import_partial = True
        fake_coordinator.last_import_hourly_coverage_incomplete = True
        fake_coordinator.last_import_writers = {"hourly_blocked_reason": "pending"}
        fake_coordinator.last_import_skipped_window_examples = (
            {
                "resource": "runtime_sensor",
                "start": "2026-07-01 00:00:00",
                "end": "2026-07-02 00:00:00",
            },
        )

        entities = self.binary_sensor._build_entities(fake_coordinator)
        by_key = {
            entity._attr_translation_key: entity
            for entity in entities
            if getattr(entity, "_attr_translation_key", None)
        }

        for key in ("sensor_in_use", "active_alert", "filter_due", "filter_due_soon"):
            self.assertFalse(by_key[key]._attr_entity_registry_enabled_default, key)
        for key in (
            "equipment_alert",
            "runtime_summary_stale",
            "cloud_data_stale",
            "statistics_import_partial",
            "homekit_mapping_incomplete",
        ):
            self.assertTrue(
                getattr(by_key[key], "_attr_entity_registry_enabled_default", True),
                key,
            )

        self.assertTrue(by_key["active_alert"].is_on)
        self.assertFalse(by_key["equipment_alert"].is_on)
        self.assertFalse(by_key["filter_due"].is_on)
        self.assertTrue(by_key["filter_due_soon"].is_on)
        self.assertTrue(by_key["runtime_summary_stale"].is_on)
        cloud_stale = by_key["cloud_data_stale"]
        self.assertTrue(cloud_stale.is_on)
        self.assertEqual(
            cloud_stale.extra_state_attributes,
            {"lag_minutes": 480, "threshold_minutes": 420},
        )
        metadata = data.thermostat_metadata[1]
        fake_coordinator.data = replace(
            data,
            thermostat_metadata={1: replace(metadata, data_lag_minutes=420)},
        )
        self.assertFalse(cloud_stale.is_on)
        fake_coordinator.data = replace(
            data,
            thermostat_metadata={1: replace(metadata, data_lag_minutes=421)},
        )
        self.assertTrue(cloud_stale.is_on)
        self.assertFalse(by_key["homekit_mapping_incomplete"].is_on)
        self.assertTrue(by_key["auto_away_enabled"].is_on)
        self.assertFalse(by_key["follow_me_enabled"].is_on)
        self.assertTrue(by_key["smart_circulation_enabled"].is_on)
        self.assertTrue(by_key["preheating_enabled"].is_on)
        self.assertFalse(by_key["precooling_enabled"].is_on)
        self.assertTrue(by_key["hot_temperature_alert_enabled"].is_on)
        self.assertFalse(by_key["cold_temperature_alert_enabled"].is_on)
        self.assertTrue(by_key["wifi_offline_alert_enabled"].is_on)
        self.assertTrue(by_key["service_reminder_enabled"].is_on)
        self.assertFalse(by_key["microphone_enabled"].is_on)
        sensor_in_use = by_key["sensor_in_use"]
        self.assertEqual(
            "Beestat-reported sensor in use",
            sensor_in_use._attr_name,
        )
        self.assertTrue(sensor_in_use.available)
        self.assertTrue(sensor_in_use.is_on)
        fake_coordinator.data = replace(
            data,
            sensor_metadata={
                10: replace(data.sensor_metadata[10], in_use=None),
            },
        )
        self.assertTrue(sensor_in_use.available)
        self.assertIsNone(sensor_in_use.is_on)
        fake_coordinator.data = replace(data, sensor_metadata={})
        self.assertFalse(sensor_in_use.available)
        fake_coordinator.data = replace(
            data,
            sensor_metadata={
                10: replace(data.sensor_metadata[10], inactive=True),
            },
        )
        self.assertFalse(sensor_in_use.available)
        fake_coordinator.data = replace(
            data,
            sensor_metadata={
                10: replace(data.sensor_metadata[10], deleted=True),
            },
        )
        self.assertFalse(sensor_in_use.available)
        fake_coordinator.data = data
        self.assertFalse(
            by_key["auto_away_enabled"]._attr_entity_registry_enabled_default
        )
        self.assertEqual(
            by_key["statistics_import_partial"].extra_state_attributes[
                "last_import_skipped_window_examples"
            ],
            [
                {
                    "resource": "runtime_sensor",
                    "start": "2026-07-01 00:00:00",
                    "end": "2026-07-02 00:00:00",
                }
            ],
        )

        partial = by_key["statistics_import_partial"]
        self.assertTrue(partial.is_on)
        self.assertTrue(
            partial.extra_state_attributes["last_import_hourly_coverage_incomplete"]
        )
        self.assertEqual(
            partial.extra_state_attributes["last_import_hourly_blocked_reason"],
            "pending",
        )
        fake_coordinator.last_import_writers = None
        self.assertIsNone(
            partial.extra_state_attributes["last_import_hourly_blocked_reason"]
        )

    def test_filter_due_sensor_uses_latest_thermostat_options(self) -> None:
        old = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="main",
            name="Main",
            filter_lifetime_runtime_hours=1000,
            filter_max_age_days=1,
        )
        summary = self.coordinator.ThermostatRuntimeSummary(
            thermostat_id=1,
            slug="main",
            label="Main",
            latest_date=date(2026, 7, 5),
            lag_days=0,
            filter_changed_date=date(2026, 7, 3),
            filter_changed_source="native",
            filter_runtime_hours=10,
            recent_runtime_hours_per_day=1,
        )
        data = self.coordinator.BeestatRuntimeData(
            config=self.config_model.BeestatConfig(thermostats=(old,), sensors=()),
            fetched_at=datetime(2026, 7, 5, tzinfo=UTC),
            projected_at=datetime(2026, 7, 5, tzinfo=UTC),
            sync_success_at=None,
            metadata_sync_success_at=None,
            summary_rows=(),
            summary_rows_full=True,
            summary_window_start=None,
            summary_window_end=None,
            thermostat_rows=(),
            sensor_rows=(),
            summary_row_count=0,
            thermostats={1: summary},
            thermostat_metadata={},
            sensor_metadata={},
        )
        fake_coordinator = _FakeCoordinator(data)
        entity = self.binary_sensor.BeestatFilterDueProblemBinarySensor(
            fake_coordinator,
            old,
        )
        self.assertTrue(entity.is_on)

        current = replace(old, filter_max_age_days=90)
        fake_coordinator.data = replace(
            data,
            config=self.config_model.BeestatConfig(
                thermostats=(current,),
                sensors=(),
            ),
        )

        self.assertFalse(entity.is_on)

    def test_filter_due_sensor_uses_local_projection_date(self) -> None:
        thermostat = self.config_model.ConfiguredThermostat(
            thermostat_id=1,
            slug="zone_a",
            name="Zone A",
            filter_max_age_days=1,
        )
        summary = self.coordinator.ThermostatRuntimeSummary(
            thermostat_id=1,
            slug="zone_a",
            label="Zone A",
            latest_date=date(2026, 7, 5),
            lag_days=1,
            filter_changed_date=date(2026, 7, 5),
            filter_changed_source="native",
            filter_runtime_hours=0,
            recent_runtime_hours_per_day=1,
        )
        data = self.coordinator.BeestatRuntimeData(
            config=self.config_model.BeestatConfig(
                thermostats=(thermostat,),
                sensors=(),
            ),
            fetched_at=datetime(2026, 7, 5, 12, tzinfo=UTC),
            projected_at=datetime(2026, 7, 6, 3, tzinfo=UTC),
            sync_success_at=None,
            metadata_sync_success_at=None,
            summary_rows=(),
            summary_rows_full=True,
            summary_window_start=None,
            summary_window_end=None,
            thermostat_rows=(),
            sensor_rows=(),
            summary_row_count=0,
            thermostats={1: summary},
            thermostat_metadata={},
            sensor_metadata={},
        )
        coordinator = _FakeCoordinator(data)
        entity = self.binary_sensor.BeestatFilterDueProblemBinarySensor(
            coordinator,
            thermostat,
        )

        self.assertFalse(entity.is_on)
        coordinator.data = replace(
            data, projected_at=datetime(2026, 7, 6, 4, tzinfo=UTC)
        )
        self.assertTrue(entity.is_on)


class _FakeCoordinator:
    def __init__(self, data) -> None:
        self.data = data
        self.hass = object()
        self.last_update_success = True
        self.local_tz = ZoneInfo("America/New_York")
        self.cloud_data_stale_threshold_minutes = 420
        self.last_import_partial = False
        self.last_import_skipped_windows = 0
        self.last_import_skipped_runtime_thermostat_windows = 0
        self.last_import_skipped_runtime_sensor_windows = 0
        self.last_import_skipped_window_examples = ()
        self.last_import_hourly_coverage_incomplete = False
        self.last_import_writers = None


if __name__ == "__main__":
    unittest.main()
