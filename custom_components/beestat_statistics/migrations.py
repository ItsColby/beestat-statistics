"""Registry migrations and device reconciliation for Beestat Statistics."""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import BeestatRuntimeData
from .entity import is_beestat_only_device
from .runtime import BeestatStatisticsConfigEntry

_LOGGER = logging.getLogger(__name__)

_THERMOSTAT_ENTITY_SUFFIXES: tuple[str, ...] = (
    "runtime_summary_latest_date",
    "runtime_summary_lag_days",
    "current_comfort_profile",
    "scheduled_comfort_profile",
    "next_scheduled_comfort_profile_time",
    "active_sensor_count",
    "cloud_data_end",
    "cloud_data_lag_minutes",
    "active_alert_count",
    "active_alert_category",
    "filter_runtime_hours",
    "filter_recent_runtime_hours_per_day",
    "filter_remaining_runtime_hours",
    "filter_runtime_due_date",
    "filter_max_age_due_date",
    "filter_due_date",
    "filter_days_remaining",
    "filter_changed_date",
    "mark_filter_changed",
    "equipment_alert",
    "filter_due",
    "filter_due_soon",
    "runtime_summary_stale",
    "cloud_data_stale",
)
_GLOBAL_UNIQUE_ID_MIGRATION = {
    "beestat_statistics_status": "status",
    "beestat_runtime_sync_last_success": "runtime_sync_last_success",
    "beestat_metadata_sync_last_success": "metadata_sync_last_success",
    "beestat_runtime_summary_row_count": "runtime_summary_row_count",
    "beestat_statistics_last_import_success": "statistics_last_import_success",
    "beestat_statistics_imported_series": "statistics_imported_series",
    "beestat_statistics_imported_rows": "statistics_imported_rows",
    "beestat_statistics_source_rows": "statistics_source_rows",
    "beestat_refresh_runtime": "refresh_runtime",
    "beestat_import_statistics": "import_statistics",
}
_LEGACY_RESOURCE_UNIQUE_ID = re.compile(
    r"beestat_(?P<unique_id>(?P<kind>thermostat|sensor)_\d+_(?P<suffix>.+))"
)
UNIQUE_ID_MIGRATION_MINOR_VERSION = 6


def _current_beestat_device_identifiers(
    data: BeestatRuntimeData,
) -> set[tuple[str, str]]:
    """Return Beestat-owned fallback identifiers currently present in live data."""

    identifiers = {(DOMAIN, "service")}
    identifiers.update(
        (DOMAIN, f"thermostat_{thermostat.thermostat_id}")
        for thermostat in data.config.thermostats
        if thermostat.device_id is None
    )
    identifiers.update(
        (DOMAIN, f"sensor_{sensor.sensor_id}")
        for sensor in data.config.sensors
        if sensor.device_id is None
    )
    return identifiers


def _migrate_legacy_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move `beestat_`-prefixed entity unique IDs to stable resource-ID keys."""

    registry = er.async_get(hass)
    skipped_conflicts = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        new_unique_id = _current_unique_id(entity_entry.unique_id)
        if new_unique_id is None:
            continue
        existing_entity_id = registry.async_get_entity_id(
            entity_entry.domain,
            entity_entry.platform,
            new_unique_id,
        )
        if existing_entity_id not in (None, entity_entry.entity_id):
            skipped_conflicts += 1
            continue
        registry.async_update_entity(
            entity_entry.entity_id,
            new_unique_id=new_unique_id,
        )
    if skipped_conflicts:
        _LOGGER.warning(
            "Skipped %s Beestat unique ID migration conflict(s)",
            skipped_conflicts,
        )


def _current_unique_id(unique_id: str) -> str | None:
    """Return the stable unique ID for a legacy `beestat_`-prefixed one."""

    if (global_unique_id := _GLOBAL_UNIQUE_ID_MIGRATION.get(unique_id)) is not None:
        return global_unique_id
    match = _LEGACY_RESOURCE_UNIQUE_ID.fullmatch(unique_id)
    if match is None:
        return None
    suffixes = (
        (*_THERMOSTAT_ENTITY_SUFFIXES, "active_alert")
        if match["kind"] == "thermostat"
        else ("sensor_in_use",)
    )
    return match["unique_id"] if match["suffix"] in suffixes else None


@callback
def _async_migrate_homekit_device_assignments(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData | None,
) -> None:
    """Move existing Beestat entities to mapped HomeKit devices."""

    if data is None:
        return

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    target_device_ids = _mapped_resource_device_ids(data)
    moved_count = 0
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            entity_entry.config_entry_id != entry.entry_id
            or entity_entry.platform != DOMAIN
        ):
            continue
        resource_type, separator, resource_suffix = entity_entry.unique_id.partition(
            "_"
        )
        resource_id, suffix_separator, suffix = resource_suffix.partition("_")
        resource_key = (resource_type, resource_id)
        if (
            not separator
            or not suffix_separator
            or not suffix
            or resource_key not in target_device_ids
        ):
            continue
        target_device_id = target_device_ids[resource_key]
        if target_device_id is None and _is_current_resource_fallback(
            device_registry,
            entity_entry.device_id,
            entry.entry_id,
            resource_key,
        ):
            continue
        if entity_entry.device_id == target_device_id:
            continue
        entity_registry.async_update_entity(
            entity_entry.entity_id,
            device_id=target_device_id,
        )
        moved_count += 1

    current_fallback_identifiers = _current_beestat_device_identifiers(data)
    removed_count = 0
    for device_entry in dr.async_entries_for_config_entry(
        device_registry,
        entry.entry_id,
    ):
        if not is_beestat_only_device(device_entry, entry.entry_id):
            continue
        beestat_identifiers = set(device_entry.identifiers)
        if not beestat_identifiers.isdisjoint(current_fallback_identifiers):
            continue
        device_registry.async_remove_device(device_entry.id)
        removed_count += 1
    if moved_count or removed_count:
        _LOGGER.info(
            "Reconciled HomeKit/Ecobee device mapping: moved %s entity record(s), "
            "removed %s stale fallback device record(s)",
            moved_count,
            removed_count,
        )


def _mapped_resource_device_ids(
    data: BeestatRuntimeData,
) -> dict[tuple[str, str], str | None]:
    """Return target devices for every stable thermostat and sensor identity."""

    return {
        **{
            ("thermostat", str(thermostat.thermostat_id)): thermostat.device_id
            for thermostat in data.config.thermostats
        },
        **{
            ("sensor", str(sensor.sensor_id)): sensor.device_id
            for sensor in data.config.sensors
        },
    }


def _is_current_resource_fallback(
    registry: dr.DeviceRegistry,
    device_id: str | None,
    entry_id: str,
    resource_key: tuple[str, str],
) -> bool:
    """Preserve only the current resource's exclusively owned fallback device."""

    return (
        device_id is not None
        and (device := registry.async_get(device_id)) is not None
        and is_beestat_only_device(device, entry_id)
        and device.identifiers == {(DOMAIN, "_".join(resource_key))}
    )
