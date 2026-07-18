"""Shared entity helpers for Beestat Statistics."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
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


def service_device_info() -> DeviceInfo:
    """Return the Beestat service device info."""

    return DeviceInfo(
        identifiers={SERVICE_IDENTIFIER},
        name=SERVICE_NAME,
        manufacturer="Beestat",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )


def thermostat_device_info(thermostat: ConfiguredThermostat) -> DeviceInfo:
    """Return device info for a thermostat enrichment entity."""

    if thermostat.device_identifiers or thermostat.device_connections:
        return DeviceInfo(
            identifiers=set(thermostat.device_identifiers),
            connections=set(thermostat.device_connections),
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"thermostat_{thermostat.thermostat_id}")},
        name=thermostat.name,
        manufacturer="Ecobee",
        model="Thermostat via Beestat",
        via_device=SERVICE_IDENTIFIER,
        configuration_url=CONFIGURATION_URL,
    )


def thermostat_suggested_object_id(
    thermostat: ConfiguredThermostat,
    suffix: str,
) -> str | None:
    """Return a fallback-only object ID hint for thermostat entities."""

    if thermostat.device_identifiers or thermostat.device_connections:
        return None
    return f"beestat_{thermostat.slug}_{suffix}"


def room_sensor_device_info(sensor: ConfiguredSensor) -> DeviceInfo:
    """Return device info for a room-sensor enrichment entity."""

    if sensor.device_identifiers or sensor.device_connections:
        return DeviceInfo(
            identifiers=set(sensor.device_identifiers),
            connections=set(sensor.device_connections),
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"sensor_{sensor.sensor_id}")},
        name=sensor.name,
        manufacturer="Ecobee",
        model="Room sensor via Beestat",
        via_device=SERVICE_IDENTIFIER,
        configuration_url=CONFIGURATION_URL,
    )
