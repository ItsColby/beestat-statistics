"""Native Beestat Statistics buttons."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import BeestatApiError, BeestatAuthError
from .config_model import ConfiguredThermostat
from .const import DOMAIN, thermostat_entity_unique_id
from .coordinator import BeestatRuntimeDataCoordinator
from .entity import (
    async_add_new_entities,
    service_device_info,
    thermostat_device_info,
    thermostat_suggested_object_id,
)
from .entry_options import async_set_filter_changed_date
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime

if TYPE_CHECKING:
    from . import BeestatStatisticsImporter

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BeestatButtonEntityDescription(ButtonEntityDescription):
    """Entity description for a Beestat button."""

    action: str


BUTTON_DESCRIPTIONS: tuple[BeestatButtonEntityDescription, ...] = (
    BeestatButtonEntityDescription(
        key="refresh_runtime",
        name="Refresh runtime",
        translation_key="refresh_runtime",
        entity_category=EntityCategory.DIAGNOSTIC,
        action="refresh_runtime",
    ),
    BeestatButtonEntityDescription(
        key="import_statistics",
        name="Import statistics",
        translation_key="import_statistics",
        entity_category=EntityCategory.DIAGNOSTIC,
        action="import_statistics",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Beestat Statistics buttons from a config entry."""

    runtime: BeestatStatisticsRuntime = entry.runtime_data
    async_add_new_entities(
        runtime.coordinator,
        async_add_entities,
        lambda coordinator: _build_entities(coordinator, runtime.importer),
        entry.async_on_unload,
    )


def _build_entities(
    coordinator: BeestatRuntimeDataCoordinator,
    importer: BeestatStatisticsImporter,
) -> list[ButtonEntity]:
    entities: list[ButtonEntity] = [
        BeestatButton(coordinator, importer, description)
        for description in BUTTON_DESCRIPTIONS
    ]
    data = coordinator.data
    thermostats = data.config.thermostats if data else ()
    entities.extend(
        BeestatFilterChangedButton(coordinator, thermostat)
        for thermostat in thermostats
    )
    return entities


class BeestatButton(ButtonEntity):
    """A Beestat action button."""

    entity_description: BeestatButtonEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        importer: BeestatStatisticsImporter,
        description: BeestatButtonEntityDescription,
    ) -> None:
        self._coordinator = coordinator
        self._importer = importer
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = description.key

    async def async_press(self) -> None:
        """Handle the button press."""

        action = self.entity_description.action
        try:
            if action == "refresh_runtime":
                await self._coordinator.async_refresh_runtime()
                return
            await self._importer.async_import_statistics()
        except BeestatAuthError as err:
            if action != "refresh_runtime":
                self._coordinator.async_record_import_error(err)
            self._coordinator.config_entry.async_start_reauth_if_available(
                self._coordinator.hass
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_auth_failed",
            ) from err
        except BeestatApiError as err:
            if action != "refresh_runtime":
                self._coordinator.async_record_import_error(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_request_failed",
            ) from err
        except Exception as err:
            if action != "refresh_runtime":
                self._coordinator.async_record_import_error(err)
            _LOGGER.exception("Unexpected Beestat button failure during %s", action)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    "beestat_request_failed"
                    if action == "refresh_runtime"
                    else "statistics_import_failed"
                ),
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat service device."""

        return service_device_info()


class BeestatFilterChangedButton(ButtonEntity):
    """Mark an HVAC filter as changed today."""

    _attr_has_entity_name = True
    _attr_name = "Mark filter changed"
    _attr_translation_key = "mark_filter_changed"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        self._coordinator = coordinator
        self._thermostat_id = thermostat.thermostat_id
        self._device_info = thermostat_device_info(thermostat)
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "mark_filter_changed",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "mark_filter_changed",
        )

    async def async_press(self) -> None:
        """Mark the filter as changed on the current local date."""

        await async_set_filter_changed_date(
            self._coordinator,
            self._thermostat_id,
            dt_util.now().date(),
        )

    @property
    def available(self) -> bool:
        """Return whether the thermostat is currently configured."""

        data = self._coordinator.data
        return data is not None and any(
            thermostat.thermostat_id == self._thermostat_id
            for thermostat in data.config.thermostats
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Home Assistant device this entity belongs to."""

        return self._device_info
