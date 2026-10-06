"""State and registry listeners that keep a Beestat Statistics entry current."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .config_model import (
    ConfiguredSensor,
    ConfiguredThermostat,
    configured_override_entity_ids,
)
from .config_payload import entry_runtime_config_data
from .coordinator import BeestatRuntimeData, BeestatRuntimeDataCoordinator
from .entity_reference import (
    configured_entity_references,
    entity_registry_event_matches_references,
)
from .issues import _async_update_mapping_device_conflicts_issue
from .migrations import _async_migrate_homekit_device_assignments
from .runtime import BeestatStatisticsConfigEntry
from .source_identity import is_thermostat_identity_source


@callback
def _async_track_time_zone_updates(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """Reproject cached state when Home Assistant's configured timezone changes."""

    @callback
    def handle_core_config_update(_event: Event[Any]) -> None:
        coordinator.async_update_local_timezone(ZoneInfo(str(hass.config.time_zone)))

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, handle_core_config_update)
    )


def _mapped_source_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return source registry entities whose association drives helper linking."""

    if data is None:
        return set()
    entity_ids: set[str] = set()
    for thermostat in data.config.thermostats:
        entity_ids.update(_configured_source_entity_ids(thermostat))
    for sensor in data.config.sensors:
        entity_ids.update(_configured_source_entity_ids(sensor))
    return entity_ids


def _configured_source_entity_ids(
    item: ConfiguredThermostat | ConfiguredSensor,
) -> set[str]:
    """Return explicitly or automatically selected source entities."""

    references = [
        item.temperature_entity_id,
        item.occupancy_entity_id,
        item.motion_entity_id,
    ]
    if isinstance(item, ConfiguredThermostat):
        references.append(item.climate_entity_id)
    return {reference for reference in references if reference is not None}


def _mapped_source_device_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return current source device IDs that drive helper linking."""

    if data is None:
        return set()
    return {
        device_id
        for device_id in (
            *(item.device_id for item in data.config.thermostats),
            *(item.device_id for item in data.config.sensors),
        )
        if device_id is not None
    }


@callback
def _room_temperature_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return mapped temperature sources used by profile-aware projections."""

    if data is None:
        return set()
    return {
        entity_id
        for entity_id in (
            *(item.temperature_entity_id for item in data.config.thermostats),
            *(item.temperature_entity_id for item in data.config.sensors),
        )
        if entity_id is not None
    }


@callback
def _async_track_room_temperature_sources(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> Callable[[], None]:
    """Reproject current-profile spreads on local temperature state changes."""

    @callback
    def handle_temperature_change(_event: Event[Any]) -> None:
        entry.runtime_data.coordinator.async_rebuild_runtime_from_cached_rows()

    return _async_track_runtime_entity_states(
        hass,
        entry,
        _room_temperature_entity_ids,
        handle_temperature_change,
    )


@callback
def _async_track_runtime_entity_states(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    sources: Callable[[BeestatRuntimeData | None], Iterable[str]],
    action: Callable[[Event[Any]], None],
) -> Callable[[], None]:
    """Keep one state listener aligned with the current normalized source set."""

    coordinator = entry.runtime_data.coordinator
    tracked_entity_ids: tuple[str, ...] = ()
    remove_state_listener: Callable[[], None] | None = None

    @callback
    def rebind_state_listener() -> None:
        nonlocal tracked_entity_ids, remove_state_listener
        entity_ids = tuple(sorted(set(sources(coordinator.data))))
        if entity_ids == tracked_entity_ids:
            return
        if remove_state_listener is not None:
            remove_state_listener()
        tracked_entity_ids = entity_ids
        remove_state_listener = (
            async_track_state_change_event(
                hass,
                entity_ids,
                action,
            )
            if entity_ids
            else None
        )

    rebind_state_listener()
    remove_coordinator_listener = coordinator.async_add_listener(rebind_state_listener)

    @callback
    def remove() -> None:
        remove_coordinator_listener()
        if remove_state_listener is not None:
            remove_state_listener()

    entry.async_on_unload(remove)
    return remove


@callback
def _async_track_source_device_relinks(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> tuple[Callable[[], None], ...]:
    """Rebind existing helpers when a mapped foreign source association changes."""

    coordinator = entry.runtime_data.coordinator
    watched_entity_ids = _mapped_source_entity_ids(coordinator.data)
    entity_registry = er.async_get(hass)
    stable_references = configured_entity_references(entry_runtime_config_data(entry))
    watched_entity_ids.update(
        configured_override_entity_ids(
            entry_runtime_config_data(entry),
            entity_registry=entity_registry,
        )
    )
    watched_device_ids = _mapped_source_device_ids(coordinator.data)

    @callback
    def track_registered_devices() -> None:
        # The physical probe may belong to the paired Ecobee registration while
        # enrichment remains attached to HomeKit. Observe identity changes on both.
        # Unselected candidate thermostats can introduce or remove ambiguity.
        watched_entity_ids.update(
            source.entity_id
            for source in entity_registry.entities.values()
            if is_thermostat_identity_source(source)
        )
        watched_device_ids.update(
            source.device_id
            for entity_id in watched_entity_ids
            if (source := entity_registry.async_get(entity_id)) is not None
            and source.device_id is not None
        )

    track_registered_devices()

    @callback
    def handle_coordinator_update() -> None:
        data = coordinator.data
        watched_entity_ids.update(_mapped_source_entity_ids(data))
        watched_entity_ids.update(
            configured_override_entity_ids(
                entry_runtime_config_data(entry),
                entity_registry=entity_registry,
            )
        )
        watched_device_ids.update(_mapped_source_device_ids(data))
        track_registered_devices()
        _async_migrate_homekit_device_assignments(hass, entry, data)
        _async_update_mapping_device_conflicts_issue(hass, entry)

    @callback
    def reconcile_assignments() -> None:
        coordinator.async_rebuild_runtime_from_cached_rows()

    @callback
    def handle_entity_registry_update(event: Event[Any]) -> None:
        changed_entity_ids = {
            str(value)
            for key in ("entity_id", "old_entity_id")
            if (value := event.data.get(key)) is not None
        }
        if any(
            is_thermostat_identity_source(entity_registry.async_get(entity_id))
            for entity_id in changed_entity_ids
        ):
            track_registered_devices()
        if watched_entity_ids.isdisjoint(
            changed_entity_ids
        ) and not entity_registry_event_matches_references(
            entity_registry,
            changed_entity_ids,
            stable_references,
        ):
            return
        reconcile_assignments()

    @callback
    def handle_device_registry_update(event: Event[Any]) -> None:
        device_id = event.data.get("device_id")
        if device_id is None or str(device_id) not in watched_device_ids:
            return
        reconcile_assignments()

    removers = (
        coordinator.async_add_listener(handle_coordinator_update),
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            handle_entity_registry_update,
        ),
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            handle_device_registry_update,
        ),
    )
    for remove_listener in removers:
        entry.async_on_unload(remove_listener)
    return removers


def _filter_changed_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    if data is None:
        return set()
    return {
        thermostat.filter_changed_entity_id
        for thermostat in data.config.thermostats
        if thermostat.filter_changed_entity_id is not None
    }
