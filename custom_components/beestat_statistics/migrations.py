"""Registry migrations and device reconciliation for Beestat Statistics."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import BeestatRuntimeData
from .entity import is_beestat_only_device
from .runtime import BeestatStatisticsConfigEntry

_LOGGER = logging.getLogger(__name__)


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
