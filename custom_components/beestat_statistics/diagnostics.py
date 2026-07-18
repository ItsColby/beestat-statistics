"""Diagnostics for Beestat Statistics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCOUNT_FINGERPRINT,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_MOTION_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
)
from .coordinator import BeestatRuntimeData, ThermostatRuntimeSummary
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime

TO_REDACT = {
    CONF_API_KEY,
    CONF_API_BASE,
    CONF_ACCOUNT_FINGERPRINT,
    "id",
    "identifier",
    "sensor_id",
    "thermostat_id",
    "thermostat_slug",
    "last_filter_alert_dismiss_thermostat_id",
    CONF_CLIMATE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_MOTION_ENTITY_ID,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a Beestat Statistics config entry."""

    runtime: BeestatStatisticsRuntime | None = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime else None
    redaction_values = _redaction_values(entry.data)
    diagnostics = {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "status": runtime.coordinator.status if runtime else "not_loaded",
            "last_error": _redacted_text(runtime.coordinator.last_error, redaction_values)
            if runtime
            else None,
            "last_error_at": _isoformat(runtime.coordinator.last_error_at)
            if runtime
            else None,
            "last_import_success_at": _isoformat(
                runtime.coordinator.last_import_success_at
            )
            if runtime
            else None,
            "last_imported_series": runtime.coordinator.last_imported_series
            if runtime
            else None,
            "last_imported_rows": runtime.coordinator.last_imported_rows
            if runtime
            else None,
            "last_import_source_rows": runtime.coordinator.last_import_source_rows
            if runtime
            else None,
            "last_import_partial": runtime.coordinator.last_import_partial
            if runtime
            else None,
            "last_import_skipped_windows": (
                runtime.coordinator.last_import_skipped_windows
            )
            if runtime
            else None,
            "last_import_skipped_runtime_thermostat_windows": (
                runtime.coordinator.last_import_skipped_runtime_thermostat_windows
            )
            if runtime
            else None,
            "last_import_skipped_runtime_sensor_windows": (
                runtime.coordinator.last_import_skipped_runtime_sensor_windows
            )
            if runtime
            else None,
            "last_import_summary_mode": runtime.coordinator.last_import_summary_mode
            if runtime
            else None,
            "last_import_summary_window_start": (
                runtime.coordinator.last_import_summary_window_start
            )
            if runtime
            else None,
            "last_import_summary_window_end": (
                runtime.coordinator.last_import_summary_window_end
            )
            if runtime
            else None,
            "last_import_summary_overlap_days": (
                runtime.coordinator.last_import_summary_overlap_days
            )
            if runtime
            else None,
            "last_import_summary_fallback_reason": (
                runtime.coordinator.last_import_summary_fallback_reason
            )
            if runtime
            else None,
            "last_import_cumulative_seed_count": (
                runtime.coordinator.last_import_cumulative_seed_count
            )
            if runtime
            else None,
            "last_filter_alert_dismiss_attempt_at": _isoformat(
                runtime.coordinator.last_filter_alert_dismiss_attempt_at
            )
            if runtime
            else None,
            "last_filter_alert_dismiss_thermostat_id": (
                runtime.coordinator.last_filter_alert_dismiss_thermostat_id
            )
            if runtime
            else None,
            "last_filter_alert_dismiss_matched": (
                runtime.coordinator.last_filter_alert_dismiss_matched
            )
            if runtime
            else None,
            "last_filter_alert_dismissed": (
                runtime.coordinator.last_filter_alert_dismissed
            )
            if runtime
            else None,
            "last_filter_alert_dismiss_error": (
                _redacted_text(
                    runtime.coordinator.last_filter_alert_dismiss_error,
                    redaction_values,
                )
            )
            if runtime
            else None,
        },
        "beestat_data": {
            "fetched_at": _isoformat(data.fetched_at) if data else None,
            "sync_success_at": _isoformat(data.sync_success_at) if data else None,
            "metadata_sync_success_at": _isoformat(data.metadata_sync_success_at)
            if data
            else None,
            "summary_row_count": data.summary_row_count if data else None,
            "thermostat_row_count": len(data.thermostat_rows) if data else None,
            "sensor_row_count": len(data.sensor_rows) if data else None,
            "thermostats": _thermostat_diagnostics(data) if data else [],
            "sensors": [
                {
                    "sensor_id": sensor.sensor_id,
                    "thermostat_slug": sensor.thermostat_slug,
                    "temperature_entity_id": sensor.temperature_entity_id,
                    "occupancy_entity_id": sensor.occupancy_entity_id,
                    "motion_entity_id": sensor.motion_entity_id,
                    "imports_temperature": sensor.include_temperature,
                    "imports_air_quality": sensor.include_air_quality,
                    "imports_co2": sensor.include_co2,
                    "imports_voc": sensor.include_voc,
                }
                for sensor in data.config.sensors
            ]
            if data
            else [],
        },
    }
    return async_redact_data(diagnostics, TO_REDACT)


def _thermostat_diagnostics(data: BeestatRuntimeData) -> list[dict[str, Any]]:
    """Return thermostat diagnostics without using room names as dictionary keys."""

    diagnostics: list[dict[str, Any]] = []
    for thermostat in data.config.thermostats:
        summary = data.thermostats.get(thermostat.thermostat_id)
        if summary is None:
            continue
        diagnostics.append(_thermostat_summary_diagnostics(data, summary))
    return diagnostics


def _thermostat_summary_diagnostics(
    data: BeestatRuntimeData,
    summary: ThermostatRuntimeSummary,
) -> dict[str, Any]:
    metadata = data.thermostat_metadata.get(summary.thermostat_id)
    return {
        "thermostat_id": summary.thermostat_id,
        "latest_date": str(summary.latest_date) if summary.latest_date else None,
        "lag_days": summary.lag_days,
        "filter_runtime_hours": summary.filter_runtime_hours,
        "recent_runtime_hours_per_day": summary.recent_runtime_hours_per_day,
        "current_profile": metadata.current_climate_name if metadata else None,
        "scheduled_profile": metadata.scheduled_climate_name if metadata else None,
        "next_scheduled_profile": (
            metadata.next_scheduled_climate_name if metadata else None
        ),
        "next_scheduled_at": _isoformat(metadata.next_scheduled_at if metadata else None),
        "current_profile_sensor_count": (
            len(metadata.current_profile_sensor_names) if metadata else None
        ),
        "cloud_data_lag_minutes": metadata.data_lag_minutes if metadata else None,
        "active_alert_count": metadata.active_alert_count if metadata else None,
        "climate_entity_id": _thermostat_mapping(
            data,
            summary.thermostat_id,
            "climate_entity_id",
        ),
        "temperature_entity_id": _thermostat_mapping(
            data,
            summary.thermostat_id,
            "temperature_entity_id",
        ),
    }


def _thermostat_mapping(data: BeestatRuntimeData, thermostat_id: int, field: str) -> Any:
    for thermostat in data.config.thermostats:
        if thermostat.thermostat_id == thermostat_id:
            return getattr(thermostat, field)
    return None


def _redaction_values(data: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in (CONF_API_KEY, CONF_API_BASE):
        value = data.get(key)
        if value in (None, ""):
            continue
        text = str(value)
        values.append(text)
        if key == CONF_API_BASE:
            values.append(text.rstrip("/"))
    return tuple(dict.fromkeys(value for value in values if value))


def _redacted_text(value: str | None, redaction_values: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    redacted = value
    for text in redaction_values:
        redacted = redacted.replace(text, "REDACTED")
    return redacted


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None
