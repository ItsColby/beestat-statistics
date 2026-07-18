"""Local, read-only configuration response helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .config_model import BeestatConfig, ConfiguredSensor, ConfiguredThermostat
from .const import CONF_SENSORS, CONF_THERMOSTATS


def configuration_response(
    *,
    entry_id: str,
    entry_data: Mapping[str, Any],
    entry_options: Mapping[str, Any],
    config: BeestatConfig,
    point_lookback_days: int,
    scan_interval_seconds: int,
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
                _configured_thermostat(thermostat)
                for thermostat in config.thermostats
            ],
            "sensors": [_configured_sensor(sensor) for sensor in config.sensors],
        },
    }


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
