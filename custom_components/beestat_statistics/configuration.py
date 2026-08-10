"""Local, read-only configuration response helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from math import isfinite
from typing import Any

from .config_model import (
    BeestatConfig,
    ConfiguredSensor,
    ConfiguredThermostat,
    filter_boundary_status,
)
from .const import CONF_SENSORS, CONF_THERMOSTATS
from .profile import schedule_profile_payload, schedule_profiles_by_ref
from .thermostat_settings import ThermostatSettingsSnapshot


def configuration_response(
    *,
    entry_id: str,
    entry_data: Mapping[str, Any],
    entry_options: Mapping[str, Any],
    config: BeestatConfig,
    point_lookback_days: int,
    scan_interval_seconds: int,
    thermostat_rows: tuple[dict[str, Any], ...] = (),
    thermostat_settings: Mapping[int, ThermostatSettingsSnapshot] | None = None,
) -> dict[str, Any]:
    """Return the complete non-secret saved and effective configuration."""

    return {
        "config_entry_id": entry_id,
        "timing": {
            "point_lookback_days": point_lookback_days,
            "scan_interval_seconds": scan_interval_seconds,
        },
        "saved_overrides": {
            "thermostats": _saved_overrides(
                entry_data,
                entry_options,
                CONF_THERMOSTATS,
            ),
            "sensors": _saved_overrides(
                entry_data,
                entry_options,
                CONF_SENSORS,
            ),
        },
        "effective_configuration": {
            "thermostats": [
                _configured_thermostat(thermostat) for thermostat in config.thermostats
            ],
            "sensors": [_configured_sensor(sensor) for sensor in config.sensors],
        },
        "source_details": {
            "thermostats": _thermostat_source_details(
                config,
                thermostat_rows,
                thermostat_settings or {},
            ),
        },
    }


def _thermostat_source_details(
    config: BeestatConfig,
    thermostat_rows: tuple[dict[str, Any], ...],
    thermostat_settings: Mapping[int, ThermostatSettingsSnapshot],
) -> list[dict[str, Any]]:
    """Return allow-listed Beestat hardware, system, property, and program data."""

    rows_by_id = {
        row_id: row
        for row in thermostat_rows
        if (row_id := _int_or_none(row.get("thermostat_id", row.get("id")))) is not None
    }
    details: list[dict[str, Any]] = []
    for thermostat in config.thermostats:
        row = rows_by_id.get(thermostat.thermostat_id)
        if row is None:
            continue
        item: dict[str, Any] = {"thermostat_id": thermostat.thermostat_id}
        _copy_scalar(item, row, "model_number")
        version_value = row.get("version")
        version = _allowlisted_mapping(
            version_value,
            ("thermostatFirmwareVersion", "firmware_version", "version"),
        )
        if version:
            item["version"] = version
        elif (version_scalar := _safe_scalar(version_value)) is not None:
            item["version"] = version_scalar
        settings = _allowlisted_mapping(
            row.get("settings"),
            ("differential_heat", "differential_cool"),
        )
        if settings:
            item["settings"] = settings
        system_type = _system_type(row.get("system_type"))
        if system_type:
            item["system_type"] = system_type
        property_details = _allowlisted_mapping(
            row.get("property"),
            ("age", "square_feet", "stories", "structure_type"),
        )
        if property_details:
            item["property"] = property_details
        comfort_profiles = [
            schedule_profile_payload(profile, include_none=False)
            for profile in schedule_profiles_by_ref(row.get("program")).values()
        ]
        if comfort_profiles:
            item["comfort_profiles"] = comfort_profiles
        if (snapshot := thermostat_settings.get(thermostat.thermostat_id)) is not None:
            item["ecobee_configuration"] = snapshot.source_details
        details.append(item)
    return details


def _system_type(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for source in ("reported", "detected"):
        source_value = value.get(source)
        if not isinstance(source_value, Mapping):
            continue
        systems: dict[str, Any] = {}
        for system in ("heat", "auxiliary_heat", "cool"):
            system_value = source_value.get(system)
            details = _allowlisted_mapping(system_value, ("equipment", "stages"))
            if details:
                systems[system] = details
        if systems:
            result[source] = systems
    return result


def _allowlisted_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in keys:
        scalar = _safe_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    return result


def _copy_scalar(target: dict[str, Any], source: Mapping[str, Any], key: str) -> None:
    value = _safe_scalar(source.get(key))
    if value is not None:
        target[key] = value


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    return _text_or_none(value)


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except OverflowError, TypeError, ValueError:
        return None


def _saved_overrides(
    entry_data: Mapping[str, Any],
    entry_options: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    if key in entry_options:
        source = "options"
        value = entry_options[key]
    elif key in entry_data:
        source = "data"
        value = entry_data[key]
    else:
        source = "automatic"
        value = []
    return {
        "source": source,
        "items": _json_value(value) if isinstance(value, list) else [],
    }


def _configured_thermostat(thermostat: ConfiguredThermostat) -> dict[str, Any]:
    return {
        "thermostat_id": thermostat.thermostat_id,
        "slug": thermostat.slug,
        "name": thermostat.name,
        "climate_entity_id": thermostat.climate_entity_id,
        "temperature_entity_id": thermostat.temperature_entity_id,
        "occupancy_entity_id": thermostat.occupancy_entity_id,
        "motion_entity_id": thermostat.motion_entity_id,
        "filter_changed_entity_id": thermostat.filter_changed_entity_id,
        "filter_changed_date": _json_value(thermostat.filter_changed_date),
        "filter_changed_at": _json_value(thermostat.filter_changed_at),
        "filter_boundary_status": filter_boundary_status(thermostat),
        "filter_change_day_runtime_baseline_seconds": (
            thermostat.filter_change_day_runtime_baseline_seconds
        ),
        "filter_change_boundary_reconciled_at": _json_value(
            thermostat.filter_change_boundary_reconciled_at
        ),
        "filter_change_boundary_source_data_end": _json_value(
            thermostat.filter_change_boundary_source_data_end
        ),
        "filter_lifetime_runtime_hours": thermostat.filter_lifetime_runtime_hours,
        "filter_max_age_days": thermostat.filter_max_age_days,
        "filter_notice_days": thermostat.filter_notice_days,
    }


def _configured_sensor(sensor: ConfiguredSensor) -> dict[str, Any]:
    return {
        "sensor_id": sensor.sensor_id,
        "thermostat_id": sensor.thermostat_id,
        "thermostat_slug": sensor.thermostat_slug,
        "slug": sensor.slug,
        "name": sensor.name,
        "temperature_entity_id": sensor.temperature_entity_id,
        "occupancy_entity_id": sensor.occupancy_entity_id,
        "motion_entity_id": sensor.motion_entity_id,
        "include_temperature": sensor.include_temperature,
        "include_air_quality": sensor.include_air_quality,
        "include_co2": sensor.include_co2,
        "include_voc": sensor.include_voc,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
