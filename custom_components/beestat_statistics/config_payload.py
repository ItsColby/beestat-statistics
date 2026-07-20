"""Config-entry payload helpers for Beestat Statistics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .const import (
    API_BASE,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_ENABLED,
    CONF_FILTER_CHANGED_ENTITY_ID,
    CONF_FILTER_CHANGED_DATE,
    CONF_FILTER_LIFETIME_RUNTIME_HOURS,
    CONF_FILTER_MAX_AGE_DAYS,
    CONF_FILTER_NOTICE_DAYS,
    CONF_ID,
    CONF_INCLUDE_AIR_QUALITY,
    CONF_INCLUDE_CO2,
    CONF_INCLUDE_TEMPERATURE,
    CONF_INCLUDE_VOC,
    CONF_MOTION_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_POINT_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_SENSORS,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTAT_ID,
    CONF_THERMOSTATS,
    DEFAULT_POINT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)

CONF_API_KEY = "api_key"
CONF_SCAN_INTERVAL = "scan_interval"


def split_entry_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split config-entry data from user-tunable options."""

    data = {
        CONF_API_KEY: _clean_string(payload[CONF_API_KEY]),
        CONF_API_BASE: _clean_string(payload.get(CONF_API_BASE, API_BASE)) or API_BASE,
    }
    if payload.get(CONF_THERMOSTATS):
        data[CONF_THERMOSTATS] = _normalize_thermostat_overrides(
            payload[CONF_THERMOSTATS]
        )
    if payload.get(CONF_SENSORS):
        data[CONF_SENSORS] = payload[CONF_SENSORS]
    return data, options_from_user_input(payload)


def options_from_user_input(payload: Mapping[str, Any]) -> dict[str, int]:
    """Return normalized options from a config or options flow payload."""

    return {
        CONF_POINT_LOOKBACK_DAYS: int(
            payload.get(CONF_POINT_LOOKBACK_DAYS, DEFAULT_POINT_LOOKBACK_DAYS)
        ),
        CONF_SCAN_INTERVAL_SECONDS: max(
            int(payload.get(CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)),
            MIN_SCAN_INTERVAL_SECONDS,
        ),
    }


def connection_data_from_user_input(
    current_data: Mapping[str, Any],
    user_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge reconfigure/reauth user input with existing connection data."""

    api_key = _clean_string(user_input.get(CONF_API_KEY))
    api_base = _clean_string(user_input.get(CONF_API_BASE))
    return {
        CONF_API_KEY: api_key or current_data[CONF_API_KEY],
        CONF_API_BASE: api_base or current_data.get(CONF_API_BASE, API_BASE),
    }


def entry_data_from_yaml(conf: Mapping[str, Any]) -> dict[str, Any]:
    """Return config-entry data fields from YAML/import config."""

    data = {
        CONF_API_KEY: _clean_string(conf[CONF_API_KEY]),
        CONF_API_BASE: _clean_string(conf[CONF_API_BASE]) or API_BASE,
    }
    if conf.get(CONF_THERMOSTATS):
        data[CONF_THERMOSTATS] = _normalize_thermostat_overrides(
            conf[CONF_THERMOSTATS]
        )
    if conf.get(CONF_SENSORS):
        data[CONF_SENSORS] = conf[CONF_SENSORS]
    return data


def entry_options_from_yaml(conf: Mapping[str, Any]) -> dict[str, Any]:
    """Return config-entry option fields from YAML/import config."""

    return {
        CONF_POINT_LOOKBACK_DAYS: conf[CONF_POINT_LOOKBACK_DAYS],
        CONF_SCAN_INTERVAL_SECONDS: max(
            int(conf[CONF_SCAN_INTERVAL].total_seconds()),
            MIN_SCAN_INTERVAL_SECONDS,
        ),
    }


def merge_import_options(
    existing_options: Mapping[str, Any],
    import_data: Mapping[str, Any],
    import_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge YAML import options with UI-owned native mapping options."""

    options = dict(existing_options)
    options.update(import_options)
    for key in (CONF_THERMOSTATS, CONF_SENSORS):
        if key in import_data:
            options.pop(key, None)
    return options


def migrate_entry_payload(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return config-entry data/options normalized to the current storage shape."""

    migrated_data = dict(data)
    migrated_options = dict(options)
    migrated_data.setdefault(CONF_API_BASE, API_BASE)

    legacy_lookback = migrated_data.pop(CONF_POINT_LOOKBACK_DAYS, None)
    if legacy_lookback is not None and CONF_POINT_LOOKBACK_DAYS not in migrated_options:
        migrated_options[CONF_POINT_LOOKBACK_DAYS] = int(legacy_lookback)

    legacy_scan_seconds = migrated_data.pop(CONF_SCAN_INTERVAL_SECONDS, None)
    legacy_scan_interval = migrated_data.pop(CONF_SCAN_INTERVAL, None)
    if legacy_scan_seconds is None:
        legacy_scan_seconds = _scan_interval_seconds(legacy_scan_interval)

    if (
        legacy_scan_seconds is not None
        and CONF_SCAN_INTERVAL_SECONDS not in migrated_options
    ):
        migrated_options[CONF_SCAN_INTERVAL_SECONDS] = max(
            int(legacy_scan_seconds),
            MIN_SCAN_INTERVAL_SECONDS,
        )

    return migrated_data, migrated_options


def entry_runtime_config_data(entry: Any) -> dict[str, Any]:
    """Return data used to build runtime mapping from data plus UI options."""

    data = dict(entry.data)
    for key in (CONF_THERMOSTATS, CONF_SENSORS):
        if key in entry.options:
            data[key] = entry.options[key]
    return data


def update_thermostat_override_options(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    thermostat_id: int,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Return options with one thermostat override merged."""

    return _update_override_options(
        data,
        options,
        CONF_THERMOSTATS,
        thermostat_id,
        updates,
        managed_fields=(
            CONF_CLIMATE_ENTITY_ID,
            CONF_TEMPERATURE_ENTITY_ID,
            CONF_OCCUPANCY_ENTITY_ID,
            CONF_MOTION_ENTITY_ID,
            CONF_FILTER_CHANGED_ENTITY_ID,
            CONF_FILTER_CHANGED_DATE,
            CONF_FILTER_LIFETIME_RUNTIME_HOURS,
            CONF_FILTER_MAX_AGE_DAYS,
            CONF_FILTER_NOTICE_DAYS,
        ),
    )


def update_sensor_override_options(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    sensor_id: int,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Return options with one room-sensor override merged."""

    return _update_override_options(
        data,
        options,
        CONF_SENSORS,
        sensor_id,
        updates,
        managed_fields=(
            CONF_THERMOSTAT_ID,
            CONF_TEMPERATURE_ENTITY_ID,
            CONF_OCCUPANCY_ENTITY_ID,
            CONF_MOTION_ENTITY_ID,
            CONF_INCLUDE_TEMPERATURE,
            CONF_INCLUDE_AIR_QUALITY,
            CONF_INCLUDE_CO2,
            CONF_INCLUDE_VOC,
        ),
    )


def update_source_scope_options(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    known_thermostat_ids: tuple[int, ...],
    enabled_thermostat_ids: tuple[int, ...],
    known_sensor_ids: tuple[int, ...],
    enabled_sensor_ids: tuple[int, ...],
    explicitly_enabled_thermostat_ids: tuple[int, ...] = (),
    explicitly_enabled_sensor_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Return options with discovered Beestat source scope updated."""

    new_options = dict(options)
    _set_source_scope_options(
        new_options,
        data,
        options,
        CONF_THERMOSTATS,
        known_thermostat_ids,
        enabled_thermostat_ids,
        explicitly_enabled_thermostat_ids,
    )
    _set_source_scope_options(
        new_options,
        data,
        options,
        CONF_SENSORS,
        known_sensor_ids,
        enabled_sensor_ids,
        explicitly_enabled_sensor_ids,
    )
    return new_options


def _set_source_scope_options(
    new_options: dict[str, Any],
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    key: str,
    known_ids: tuple[int, ...],
    enabled_ids: tuple[int, ...],
    explicitly_enabled_ids: tuple[int, ...],
) -> None:
    """Update enabled flags while preserving mapping fields and unknown rows."""

    source = options.get(key) if key in options else data.get(key)
    items = [dict(item) for item in _override_items(source)]
    enabled = set(enabled_ids)
    explicitly_enabled = set(explicitly_enabled_ids)
    for item_id in sorted(set(known_ids)):
        item = _find_override_item(items, item_id)
        if item_id in enabled:
            if item_id in explicitly_enabled:
                item[CONF_ENABLED] = True
            else:
                item.pop(CONF_ENABLED, None)
        else:
            item[CONF_ENABLED] = False

    items = [item for item in items if set(item) != {CONF_ID}]
    if items or key in data:
        new_options[key] = items
    else:
        new_options.pop(key, None)


def _update_override_options(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    key: str,
    item_id: int,
    updates: Mapping[str, Any],
    *,
    managed_fields: tuple[str, ...],
) -> dict[str, Any]:
    new_options = dict(options)
    source = options.get(key) if key in options else data.get(key)
    items = [dict(item) for item in _override_items(source)]
    item = _find_override_item(items, item_id)
    for field in managed_fields:
        if field not in updates:
            continue
        value = updates[field]
        if value in (None, ""):
            item.pop(field, None)
        else:
            item[field] = value
    new_options[key] = items
    return new_options


def _find_override_item(items: list[dict[str, Any]], item_id: int) -> dict[str, Any]:
    for item in items:
        if int(item.get(CONF_ID, -1)) == item_id:
            item[CONF_ID] = item_id
            return item
    item = {CONF_ID: item_id}
    items.append(item)
    return item


def _override_items(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _normalize_thermostat_overrides(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _override_items(value):
        normalized = dict(item)
        if isinstance(normalized.get(CONF_FILTER_CHANGED_DATE), date):
            normalized[CONF_FILTER_CHANGED_DATE] = normalized[
                CONF_FILTER_CHANGED_DATE
            ].isoformat()
        items.append(normalized)
    return items


def _scan_interval_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    return int(value)


def _clean_string(value: Any) -> str:
    """Return a stripped config string without preserving copy/paste whitespace."""

    if value is None:
        return ""
    return str(value).strip()
