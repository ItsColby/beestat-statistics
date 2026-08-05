"""Shared entity helpers for Beestat Statistics."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr

try:
    from homeassistant.helpers import helper_integration
except ImportError:  # pragma: no cover - lightweight unit-test stubs
    helper_integration = None

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .config_model import ConfiguredSensor, ConfiguredThermostat
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import BeestatRuntimeDataCoordinator

SERVICE_IDENTIFIER = (DOMAIN, "service")
SERVICE_NAME = "Beestat Statistics"
CONFIGURATION_URL = "https://app.beestat.io/"


def async_add_new_entities(
    coordinator: BeestatRuntimeDataCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build_entities: Callable[[BeestatRuntimeDataCoordinator], Iterable[Entity]],
    async_on_unload: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Add current entities and subscribe for entities discovered later."""

    known_unique_ids: set[str] = set()

    def add_new_entities() -> None:
        entities: list[Entity] = []
        for entity in build_entities(coordinator):
            unique_id = entity.unique_id
            if unique_id is not None and unique_id in known_unique_ids:
                continue
            entities.append(entity)
        if not entities:
            return
        known_unique_ids.update(
            entity.unique_id for entity in entities if entity.unique_id is not None
        )
        async_add_entities(entities)

    add_new_entities()
    if async_on_unload is not None:
        async_on_unload(coordinator.async_add_listener(add_new_entities))


def async_register_service_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Register the Beestat service device before child devices reference it."""

    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={SERVICE_IDENTIFIER},
        name=SERVICE_NAME,
        manufacturer="Beestat",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )


def is_beestat_only_device(device_entry: dr.DeviceEntry, entry_id: str) -> bool:
    """Return whether a device can be safely managed as Beestat-owned."""

    return (
        set(device_entry.config_entries) == {entry_id}
        and bool(device_entry.identifiers)
        and all(identifier[0] == DOMAIN for identifier in device_entry.identifiers)
        and not device_entry.connections
    )


def async_remove_cross_integration_device_ownership(
    hass: HomeAssistant,
    entry_id: str,
    device_ids: Iterable[str | None],
) -> None:
    """Remove legacy helper ownership using the current Home Assistant API."""

    if helper_integration is None:
        return
    mapped_device_ids = {device_id for device_id in device_ids if device_id is not None}
    remove_helper_devices = getattr(
        helper_integration,
        "async_remove_helper_devices",
        None,
    )
    remove_legacy_ownership = getattr(
        helper_integration,
        "async_remove_helper_config_entry_from_source_device",
        None,
    )
    for device_id in mapped_device_ids:
        if remove_helper_devices is not None:
            remove_helper_devices(
                hass,
                helper_config_entry_id=entry_id,
                source_device_id=device_id,
            )
        elif remove_legacy_ownership is not None:
            remove_legacy_ownership(
                hass,
                helper_config_entry_id=entry_id,
                source_device_id=device_id,
            )


def service_device_info() -> DeviceInfo:
    """Return the Beestat service device info."""

    return DeviceInfo(
        identifiers={SERVICE_IDENTIFIER},
        name=SERVICE_NAME,
        manufacturer="Beestat",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )


def link_entity_to_device(
    entity: Entity,
    hass: HomeAssistant,
    device_id: str | None,
) -> None:
    """Link an enrichment entity to an existing device without co-owning it."""

    if device_id is None:
        return
    device_entry = dr.async_get(hass).async_get(device_id)
    if device_entry is not None:
        entity.device_entry = device_entry


def thermostat_device_info(thermostat: ConfiguredThermostat) -> DeviceInfo | None:
    """Return device info for a thermostat enrichment entity."""

    if thermostat.device_id is not None:
        return None
    return DeviceInfo(
        identifiers={(DOMAIN, f"thermostat_{thermostat.thermostat_id}")},
        name=thermostat.name,
        manufacturer="Ecobee",
        model="Thermostat via Beestat",
        configuration_url=CONFIGURATION_URL,
    )


def thermostat_suggested_object_id(
    thermostat: ConfiguredThermostat,
    suffix: str,
) -> str | None:
    """Return a fallback-only object ID hint for thermostat entities."""

    if thermostat.device_id is not None:
        return None
    return f"beestat_{thermostat.slug}_{suffix}"


def room_sensor_device_info(sensor: ConfiguredSensor) -> DeviceInfo | None:
    """Return device info for a room-sensor enrichment entity."""

    if sensor.device_id is not None:
        return None
    return DeviceInfo(
        identifiers={(DOMAIN, f"sensor_{sensor.sensor_id}")},
        name=sensor.name,
        manufacturer="Ecobee",
        model="Room sensor via Beestat",
        configuration_url=CONFIGURATION_URL,
    )
