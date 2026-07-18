"""Native Beestat Statistics date entities."""

from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .config_model import ConfiguredThermostat
from .const import thermostat_entity_unique_id
from .coordinator import BeestatRuntimeDataCoordinator
from .entity import (
    async_add_new_entities,
    thermostat_device_info,
    thermostat_suggested_object_id,
)
from .entry_options import async_set_filter_changed_date
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Beestat Statistics date entities from a config entry."""

    runtime: BeestatStatisticsRuntime = entry.runtime_data
    async_add_new_entities(
        runtime.coordinator,
        async_add_entities,
        _build_entities,
        entry.async_on_unload,
    )


def _build_entities(
    coordinator: BeestatRuntimeDataCoordinator,
) -> list["BeestatFilterChangedDate"]:
    data = coordinator.data
    if data is None:
        return []
    return [
        BeestatFilterChangedDate(coordinator, thermostat)
        for thermostat in data.config.thermostats
    ]


class BeestatFilterChangedDate(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    DateEntity,
):
    """Native filter-changed date for Beestat filter-runtime calculations."""

    _attr_has_entity_name = True
    _attr_name = "Filter changed date"
    _attr_translation_key = "filter_changed_date"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat_id = thermostat.thermostat_id
        self._device_info = thermostat_device_info(thermostat)
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "filter_changed_date",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "filter_changed_date",
        )

    @property
    def available(self) -> bool:
        """Return whether the thermostat is currently configured."""

        return super().available and self._thermostat is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Home Assistant device this entity belongs to."""

        return self._device_info

    @property
    def native_value(self) -> date | None:
        """Return the effective filter-changed date."""

        thermostat = self._thermostat
        if thermostat is None:
            return None
        data = self.coordinator.data
        summary = data.thermostats.get(self._thermostat_id) if data is not None else None
        if summary is not None:
            return summary.filter_changed_date
        return thermostat.filter_changed_date

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        """Return where the effective date came from."""

        data = self.coordinator.data
        if data is None:
            return None
        summary = data.thermostats.get(self._thermostat_id)
        thermostat = self._thermostat
        if summary is None or thermostat is None:
            return None
        return {
            "source": summary.filter_changed_source,
            "home_assistant_override_date": (
                thermostat.filter_changed_date.isoformat()
                if thermostat.filter_changed_date is not None
                else None
            ),
            "legacy_helper_entity_id": thermostat.filter_changed_entity_id,
        }

    async def async_set_value(self, value: date) -> None:
        """Set the native filter-changed date."""

        await async_set_filter_changed_date(self.coordinator, self._thermostat_id, value)

    @property
    def _thermostat(self) -> ConfiguredThermostat | None:
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (
                thermostat
                for thermostat in data.config.thermostats
                if thermostat.thermostat_id == self._thermostat_id
            ),
            None,
        )
