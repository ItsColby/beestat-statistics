"""Tests for Beestat diagnostics output redaction."""

from __future__ import annotations

import asyncio
import types
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime

from custom_components.beestat_statistics import (
    config_model,
    coordinator,
    diagnostics,
)


@dataclass
class FakeEntry:
    data: dict
    options: dict
    runtime_data: object
    version: int = 1
    minor_version: int = 4


class DiagnosticsTest(unittest.TestCase):
    """Validate diagnostics are useful without leaking local identifiers."""

    def setUp(self) -> None:
        self.config_model = config_model
        self.coordinator = coordinator
        self.diagnostics = diagnostics

    def test_unloaded_entry_keeps_diagnostics_available_for_malformed_options(self):
        entry = FakeEntry(
            data={"api_key": "secret-key", "future_private": "secret-value"},
            options={
                "point_lookback_days": float("inf"),
                "scan_interval_seconds": True,
                "thermostats": [None, {"id": 1, "name": "Private Zone"}],
                "sensors": {"private_id": "Private Room"},
            },
            runtime_data=None,
        )
        result = asyncio.run(
            self.diagnostics.async_get_config_entry_diagnostics(object(), entry)
        )
        self.assertEqual(result["coordinator"]["status"], "not_loaded")
        self.assertEqual(
            result["entry"]["timing"],
            {"point_lookback_days": None, "scan_interval_seconds": None},
        )
        self.assertEqual(result["entry"]["saved_overrides"]["thermostats"]["count"], 1)
        self.assertEqual(result["entry"]["saved_overrides"]["sensors"]["count"], 0)
        for private in ("secret-key", "secret-value", "Private Zone", "Private Room"):
            self.assertNotIn(private, repr(result))

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
            filter_runtime_observation=self.coordinator.FilterRuntimeObservation(
                3600,
                "partial",
                600,
                180,
                "source_gap",
                datetime(2026, 7, 3, 21, 55, tzinfo=UTC),
            ),
        )
        metadata = self.coordinator.ThermostatMetadata(
            thermostat_id=1001,
            slug="zone_a",
            label="Zone A",
            data_begin=None,
            data_end=None,
            data_lag_minutes=None,
            current_climate_ref="home",
            current_climate_name="Private Current Profile",
            scheduled_climate_ref="home",
            scheduled_climate_name="Private Scheduled Profile",
            next_scheduled_climate_ref="sleep",
            next_scheduled_climate_name="Private Next Profile",
            next_scheduled_at=datetime(2026, 7, 5, 22, 30, tzinfo=UTC),
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
            thermostats={1001: summary},
            thermostat_metadata={1001: metadata},
            sensor_metadata={},
        )
        runtime = types.SimpleNamespace(
            coordinator=types.SimpleNamespace(
                data=data,
                status="ok",
                last_error=("GET https://api.example.test/?api_key=secret-key failed"),
                last_error_at=None,
                last_import_success_at=None,
                last_imported_series=None,
                last_imported_rows=None,
                last_import_source_rows=None,
                last_import_partial=False,
                last_import_skipped_windows=0,
                last_import_skipped_runtime_thermostat_windows=0,
                last_import_skipped_runtime_sensor_windows=0,
                last_import_skipped_window_examples=(
                    {
                        "resource": "runtime_sensor",
                        "start": "2026-07-01 00:00:00",
                        "end": "2026-07-02 00:00:00",
                    },
                ),
                last_import_summary_mode="windowed",
                last_import_summary_window_start="2026-06-28",
                last_import_summary_window_end="2026-07-05",
                last_import_summary_overlap_days=7,
                last_import_summary_fallback_reason=None,
                last_import_cumulative_seed_count=5,
                last_import_writers={
                    "legacy_imported_rows": 2,
                    "hourly_imported_rows": 0,
                    "hourly_blocked_reason": "secret-key",
                },
                last_filter_alert_dismiss_attempt_at=None,
                last_filter_alert_dismiss_thermostat_id=1001,
                last_filter_alert_dismiss_matched=1,
                last_filter_alert_dismissed=1,
                last_filter_alert_dismiss_error=None,
                last_filter_boundary_reconcile_attempt_at=None,
                last_filter_boundary_reconciled_count=0,
                last_filter_boundary_pending_count=0,
                last_filter_boundary_reconcile_error=None,
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
            options={
                "thermostats": [
                    {
                        "id": 1001,
                        "slug": "private_zone_slug",
                        "name": "Private Zone Name",
                        "filter_changed_date": "2026-06-14",
                    }
                ],
                "sensors": [
                    {
                        "id": 2002,
                        "slug": "private_sensor_slug",
                        "name": "Private Sensor Name",
                    }
                ],
                "future_private_field": "Private Future Value",
                "point_lookback_days": 120,
                "scan_interval_seconds": 21600,
            },
            runtime_data=runtime,
        )

        result = asyncio.run(
            self.diagnostics.async_get_config_entry_diagnostics(object(), entry)
        )
        text = repr(result)

        self.assertIsInstance(result["beestat_data"]["thermostats"], list)
        quality = result["beestat_data"]["thermostats"][0]
        self.assertEqual(quality["runtime_coverage"], "partial")
        self.assertEqual(quality["filter_boundary_status"], "source_gap")
        self.assertEqual(quality["runtime_unknown_interval_minutes"], 10)
        self.assertNotIn("2026-07-03T21:55:00+00:00", text)
        self.assertIsInstance(result["beestat_data"]["sensors"], list)
        self.assertEqual(
            result["entry"],
            {
                "version": 1,
                "minor_version": 4,
                "connection": {
                    "api_key_configured": True,
                    "api_base_configured": True,
                    "account_fingerprint_configured": True,
                },
                "timing": {
                    "point_lookback_days": 120,
                    "scan_interval_seconds": 21600,
                },
                "saved_overrides": {
                    "thermostats": {"source": "options", "count": 1},
                    "sensors": {"source": "options", "count": 1},
                },
            },
        )
        self.assertEqual(
            result["coordinator"]["last_import_skipped_window_examples"],
            [
                {
                    "resource": "runtime_sensor",
                    "start": "2026-07-01 00:00:00",
                    "end": "2026-07-02 00:00:00",
                }
            ],
        )
        self.assertNotIn("secret-key", text)
        self.assertNotIn("fingerprint-secret", text)
        self.assertNotIn("https://api.example.test", text)
        self.assertNotIn("climate.zone_a", text)
        self.assertNotIn("sensor.room_sensor_a_temperature", text)
        self.assertNotIn("binary_sensor.room_sensor_a_occupancy", text)
        self.assertNotIn("private_zone_slug", text)
        self.assertNotIn("Private Zone Name", text)
        self.assertNotIn("private_sensor_slug", text)
        self.assertNotIn("Private Sensor Name", text)
        self.assertNotIn("2026-06-14", text)
        self.assertNotIn("Private Current Profile", text)
        self.assertNotIn("Private Scheduled Profile", text)
        self.assertNotIn("Private Next Profile", text)
        self.assertNotIn("2026-07-05T22:30:00+00:00", text)
        self.assertNotIn("Private Future Value", text)
        self.assertNotIn("future_private_field", text)
        self.assertIn("REDACTED", text)


if __name__ == "__main__":
    unittest.main()
