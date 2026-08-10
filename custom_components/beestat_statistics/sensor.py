"""Native Beestat Statistics sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alerts import active_alert_examples, classify_active_alerts
from .config_model import ConfiguredThermostat
from .const import thermostat_entity_unique_id
from .coordinator import (
    BeestatRuntimeData,
    BeestatRuntimeDataCoordinator,
    ThermostatMetadata,
)
from .entity import (
    async_add_new_entities,
    link_entity_to_device,
    service_device_info,
    thermostat_device_info,
    thermostat_suggested_object_id,
)
from .filter_forecast import (
    FilterForecast,
    build_filter_forecast,
    filter_forecast_revision,
)
from .profile import schedule_profile_payload
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime
from .thermostat_settings import (
    integer_setting,
    temperature_fahrenheit,
    text_setting,
)

if TYPE_CHECKING:
    from homeassistant.const import EntityCategory
    from homeassistant.helpers.device_registry import DeviceInfo
else:
    try:
        from homeassistant.const import EntityCategory
        from homeassistant.helpers.device_registry import DeviceInfo
    except ImportError:  # pragma: no cover - lightweight unit-test stubs
        from homeassistant.helpers.entity import DeviceInfo, EntityCategory

SensorValue = str | int | float | date | datetime | None

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BeestatSensorEntityDescription(SensorEntityDescription):
    """Entity description for a Beestat coordinator-backed sensor."""

    value_fn: Callable[[BeestatRuntimeDataCoordinator], SensorValue]
    available_fn: Callable[[BeestatRuntimeDataCoordinator], bool] = lambda coordinator: (
        coordinator.data is not None
    )
    uses_coordinator_availability: bool = True
    extra_attributes_fn: (
        Callable[[BeestatRuntimeDataCoordinator], dict[str, Any] | None] | None
    ) = None
    suggested_object_id: str | None = None


GLOBAL_SENSOR_DESCRIPTIONS: tuple[BeestatSensorEntityDescription, ...] = (
    BeestatSensorEntityDescription(
        key="status",
        name="Status",
        translation_key="status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.status,
        available_fn=lambda coordinator: True,
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="runtime_sync_last_success",
        name="Runtime sync last success",
        translation_key="runtime_sync_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: (
            coordinator.data.sync_success_at if coordinator.data else None
        ),
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="metadata_sync_last_success",
        name="Metadata sync last success",
        translation_key="metadata_sync_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: (
            coordinator.data.metadata_sync_success_at if coordinator.data else None
        ),
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="runtime_summary_row_count",
        name="Runtime summary row count",
        translation_key="runtime_summary_row_count",
        native_unit_of_measurement="rows",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: (
            coordinator.data.summary_row_count if coordinator.data else None
        ),
    ),
    BeestatSensorEntityDescription(
        key="statistics_last_import_success",
        name="Last import success",
        translation_key="statistics_last_import_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.last_import_success_at,
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="statistics_imported_series",
        name="Imported series",
        translation_key="statistics_imported_series",
        native_unit_of_measurement="series",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.last_imported_series,
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="statistics_imported_rows",
        name="Imported rows",
        translation_key="statistics_imported_rows",
        native_unit_of_measurement="rows",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.last_imported_rows,
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="statistics_source_rows",
        name="Source rows",
        translation_key="statistics_source_rows",
        native_unit_of_measurement="rows",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.last_import_source_rows,
        uses_coordinator_availability=False,
    ),
    BeestatSensorEntityDescription(
        key="statistics_skipped_windows",
        name="Skipped windows",
        translation_key="statistics_skipped_windows",
        native_unit_of_measurement="windows",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.last_import_skipped_windows,
        uses_coordinator_availability=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Beestat Statistics sensors from a config entry."""

    runtime: BeestatStatisticsRuntime = entry.runtime_data
    async_add_new_entities(
        runtime.coordinator,
        async_add_entities,
        _build_entities,
        entry.async_on_unload,
    )


def _build_entities(
    coordinator: BeestatRuntimeDataCoordinator,
) -> list[BeestatSensor]:
    entities: list[BeestatSensor] = [
        BeestatSensor(coordinator, description, service_device_info())
        for description in GLOBAL_SENSOR_DESCRIPTIONS
    ]

    data = coordinator.data
    thermostats = data.config.thermostats if data else ()
    for thermostat in thermostats:
        spread = data.room_temperature_spreads.get(thermostat.thermostat_id)
        entities.extend(
            BeestatSensor(
                coordinator,
                description,
                thermostat_device_info(thermostat),
                thermostat.device_id,
            )
            for description in _thermostat_sensor_descriptions(
                thermostat=thermostat,
                temperature_unit=spread.unit if spread is not None else None,
            )
        )
    return entities


class BeestatSensor(CoordinatorEntity[BeestatRuntimeDataCoordinator], SensorEntity):
    """A coordinator-backed Beestat sensor."""

    entity_description: BeestatSensorEntityDescription
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "current_profile",
            "current_profile_ref",
            "data_begin",
            "data_end",
            "last_error",
            "last_error_at",
            "last_import_success",
            "last_imported_rows",
            "last_import_source_rows",
            "last_import_partial",
            "last_import_skipped_windows",
            "last_import_skipped_runtime_sensor_windows",
            "last_import_skipped_runtime_thermostat_windows",
            "last_import_skipped_window_examples",
            "last_import_summary_mode",
            "last_import_summary_window_start",
            "last_import_summary_window_end",
            "last_import_summary_overlap_days",
            "last_import_summary_fallback_reason",
            "last_import_cumulative_seed_count",
            "last_filter_alert_dismiss_attempt_at",
            "last_filter_alert_dismiss_matched",
            "last_filter_alert_dismissed",
            "last_filter_alert_dismiss_error",
            "last_runtime_fetch",
            "local_room_sensor_count",
            "local_thermostat_count",
            "mapped_room_sensor_count",
            "mapped_thermostat_count",
            "next_scheduled_at",
            "next_scheduled_profile",
            "next_scheduled_profile_ref",
            "profile_ref",
            "profile_sensors",
            "participating_sensor_count",
            "participating_sensor_names",
            "valid_sensor_count",
            "unavailable_sensor_names",
            "hottest_sensor_name",
            "coldest_sensor_name",
            "profiles",
            "room_sensor_count",
            "scheduled_profile",
            "scheduled_profile_ref",
            "summary_row_count",
            "active_alerts",
            "alert_category",
            "changed_source",
            "changed_at",
            "changed_date",
            "days_remaining",
            "due",
            "due_date",
            "due_soon",
            "forecast_revision",
            "lifetime_runtime_hours",
            "max_age_days",
            "max_age_due_date",
            "notice_days",
            "recent_runtime_hours_per_day",
            "remaining_runtime_hours",
            "runtime_hours",
            "runtime_due_date",
            "thermostat_count",
            "unmapped_room_sensor_count",
            "unmapped_thermostat_count",
        }
    )

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        description: BeestatSensorEntityDescription,
        device_info: DeviceInfo | None,
        device_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        link_entity_to_device(self, coordinator.hass, device_id)
        self.entity_description = description
        self._device_info = device_info
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = description.key
        self._attr_suggested_object_id = description.suggested_object_id

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        if not self.entity_description.available_fn(self.coordinator):
            return False
        if not self.entity_description.uses_coordinator_availability:
            return True
        return super().available

    @property
    def native_value(self) -> SensorValue:
        """Return the sensor state from in-memory coordinator data."""

        return self.entity_description.value_fn(self.coordinator)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the Home Assistant device this entity belongs to."""

        return self._device_info

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return compact diagnostic attributes for the status sensor."""

        if self.entity_description.extra_attributes_fn is not None:
            return self.entity_description.extra_attributes_fn(self.coordinator)
        if self.entity_description.key != "status":
            return None
        data = self.coordinator.data
        return {
            "last_error": self.coordinator.last_error,
            "last_error_at": _isoformat(self.coordinator.last_error_at),
            "last_runtime_fetch": _isoformat(data.fetched_at if data else None),
            "summary_row_count": data.summary_row_count if data else None,
            "last_import_success": _isoformat(self.coordinator.last_import_success_at),
            "last_imported_rows": self.coordinator.last_imported_rows,
            "last_import_source_rows": self.coordinator.last_import_source_rows,
            "last_import_partial": self.coordinator.last_import_partial,
            "last_import_skipped_windows": self.coordinator.last_import_skipped_windows,
            "last_import_skipped_runtime_sensor_windows": (
                self.coordinator.last_import_skipped_runtime_sensor_windows
            ),
            "last_import_skipped_runtime_thermostat_windows": (
                self.coordinator.last_import_skipped_runtime_thermostat_windows
            ),
            "last_import_skipped_window_examples": list(
                self.coordinator.last_import_skipped_window_examples
            ),
            "last_import_summary_mode": self.coordinator.last_import_summary_mode,
            "last_import_summary_window_start": (
                self.coordinator.last_import_summary_window_start
            ),
            "last_import_summary_window_end": self.coordinator.last_import_summary_window_end,
            "last_import_summary_overlap_days": (
                self.coordinator.last_import_summary_overlap_days
            ),
            "last_import_summary_fallback_reason": (
                self.coordinator.last_import_summary_fallback_reason
            ),
            "last_import_cumulative_seed_count": (
                self.coordinator.last_import_cumulative_seed_count
            ),
            "last_filter_alert_dismiss_attempt_at": _isoformat(
                self.coordinator.last_filter_alert_dismiss_attempt_at
            ),
            "last_filter_alert_dismiss_matched": (
                self.coordinator.last_filter_alert_dismiss_matched
            ),
            "last_filter_alert_dismissed": self.coordinator.last_filter_alert_dismissed,
            "last_filter_alert_dismiss_error": (
                self.coordinator.last_filter_alert_dismiss_error
            ),
            **_mapping_summary_attributes(data),
        }


def _thermostat_sensor_descriptions(
    *,
    thermostat: ConfiguredThermostat,
    temperature_unit: str | None = None,
) -> tuple[BeestatSensorEntityDescription, ...]:
    thermostat_id = thermostat.thermostat_id
    return (
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "runtime_summary_latest_date",
            ),
            name="Runtime summary latest date",
            translation_key="runtime_summary_latest_date",
            device_class=SensorDeviceClass.DATE,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(_summary_available, thermostat_id=thermostat_id),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "runtime_summary_latest_date",
            ),
            value_fn=partial(
                _summary_value,
                thermostat_id=thermostat_id,
                field="latest_date",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "runtime_summary_lag_days",
            ),
            name="Runtime summary lag days",
            translation_key="runtime_summary_lag_days",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.DAYS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(_summary_available, thermostat_id=thermostat_id),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "runtime_summary_lag_days",
            ),
            value_fn=partial(
                _summary_value,
                thermostat_id=thermostat_id,
                field="lag_days",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "current_comfort_profile",
            ),
            name="Current comfort profile",
            translation_key="current_comfort_profile",
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "current_comfort_profile",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="current_climate_name",
            ),
            extra_attributes_fn=partial(
                _comfort_profile_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "scheduled_comfort_profile",
            ),
            name="Scheduled comfort profile",
            translation_key="scheduled_comfort_profile",
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "scheduled_comfort_profile",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="scheduled_climate_name",
            ),
            extra_attributes_fn=partial(
                _scheduled_profile_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "next_scheduled_comfort_profile_time",
            ),
            name="Next scheduled comfort profile time",
            translation_key="next_scheduled_comfort_profile_time",
            device_class=SensorDeviceClass.TIMESTAMP,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "next_scheduled_comfort_profile_time",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="next_scheduled_at",
            ),
            extra_attributes_fn=partial(
                _next_scheduled_profile_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "active_sensor_count",
            ),
            name="Active sensor count",
            translation_key="active_sensor_count",
            native_unit_of_measurement="sensors",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "active_sensor_count",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="active_sensor_count",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "current_profile_room_temperature_spread",
            ),
            name="Current profile room temperature spread",
            translation_key="current_profile_room_temperature_spread",
            native_unit_of_measurement=temperature_unit,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _room_temperature_spread_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "current_profile_room_temperature_spread",
            ),
            value_fn=partial(
                _room_temperature_spread_value,
                thermostat_id=thermostat_id,
            ),
            extra_attributes_fn=partial(
                _room_temperature_spread_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "compressor_minimum_off_time",
            ),
            name="Compressor minimum off time",
            translation_key="compressor_minimum_off_time",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement="s",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            available_fn=partial(
                _thermostat_setting_available,
                thermostat_id=thermostat_id,
                key="compressorProtectionMinTime",
                value_type="integer",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "compressor_minimum_off_time",
            ),
            value_fn=partial(
                _thermostat_setting_value,
                thermostat_id=thermostat_id,
                key="compressorProtectionMinTime",
                value_type="integer",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "compressor_minimum_outdoor_temperature",
            ),
            name="Compressor minimum outdoor temperature",
            translation_key="compressor_minimum_outdoor_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement="°F",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            available_fn=partial(
                _thermostat_setting_available,
                thermostat_id=thermostat_id,
                key="compressorProtectionMinTemp",
                value_type="temperature",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "compressor_minimum_outdoor_temperature",
            ),
            value_fn=partial(
                _thermostat_setting_value,
                thermostat_id=thermostat_id,
                key="compressorProtectionMinTemp",
                value_type="temperature",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "heat_cool_minimum_delta",
            ),
            name="Heat cool minimum delta",
            translation_key="heat_cool_minimum_delta",
            native_unit_of_measurement="°F",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            available_fn=partial(
                _thermostat_setting_available,
                thermostat_id=thermostat_id,
                key="heatCoolMinDelta",
                value_type="temperature",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "heat_cool_minimum_delta",
            ),
            value_fn=partial(
                _thermostat_setting_value,
                thermostat_id=thermostat_id,
                key="heatCoolMinDelta",
                value_type="temperature",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "hold_action",
            ),
            name="Hold action",
            translation_key="hold_action",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            available_fn=partial(
                _thermostat_setting_available,
                thermostat_id=thermostat_id,
                key="holdAction",
                value_type="text",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "hold_action",
            ),
            value_fn=partial(
                _thermostat_setting_value,
                thermostat_id=thermostat_id,
                key="holdAction",
                value_type="text",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "cloud_data_end",
            ),
            name="Cloud data end",
            translation_key="cloud_data_end",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "cloud_data_end",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="data_end",
            ),
            extra_attributes_fn=partial(
                _data_window_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "cloud_data_lag_minutes",
            ),
            name="Cloud data lag minutes",
            translation_key="cloud_data_lag_minutes",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "cloud_data_lag_minutes",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="data_lag_minutes",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "active_alert_count",
            ),
            name="Active alert count",
            translation_key="active_alert_count",
            native_unit_of_measurement="alerts",
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "active_alert_count",
            ),
            value_fn=partial(
                _thermostat_metadata_value,
                thermostat_id=thermostat_id,
                field="active_alert_count",
            ),
            extra_attributes_fn=partial(
                _active_alert_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "active_alert_category",
            ),
            name="Active alert category",
            translation_key="active_alert_category",
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "active_alert_category",
            ),
            available_fn=partial(
                _thermostat_metadata_available,
                thermostat_id=thermostat_id,
            ),
            value_fn=partial(
                _active_alert_category,
                thermostat_id=thermostat_id,
            ),
            extra_attributes_fn=partial(
                _active_alert_category_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_runtime_hours",
            ),
            name="Filter runtime hours",
            translation_key="filter_runtime_hours",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.HOURS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(_summary_available, thermostat_id=thermostat_id),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_runtime_hours",
            ),
            value_fn=partial(
                _summary_value,
                thermostat_id=thermostat_id,
                field="filter_runtime_hours",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_recent_runtime_hours_per_day",
            ),
            name="Filter recent runtime hours per day",
            translation_key="filter_recent_runtime_hours_per_day",
            native_unit_of_measurement="h/d",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(_summary_available, thermostat_id=thermostat_id),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_recent_runtime_hours_per_day",
            ),
            value_fn=partial(
                _summary_value,
                thermostat_id=thermostat_id,
                field="recent_runtime_hours_per_day",
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_remaining_runtime_hours",
            ),
            name="Filter remaining runtime hours",
            translation_key="filter_remaining_runtime_hours",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.HOURS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _filter_forecast_available,
                thermostat_id=thermostat_id,
                field="remaining_runtime_hours",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_remaining_runtime_hours",
            ),
            value_fn=partial(
                _filter_forecast_value,
                thermostat_id=thermostat_id,
                field="remaining_runtime_hours",
            ),
            extra_attributes_fn=partial(
                _filter_forecast_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_runtime_due_date",
            ),
            name="Filter runtime due date",
            translation_key="filter_runtime_due_date",
            device_class=SensorDeviceClass.DATE,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _filter_forecast_available,
                thermostat_id=thermostat_id,
                field="runtime_due_date",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_runtime_due_date",
            ),
            value_fn=partial(
                _filter_forecast_value,
                thermostat_id=thermostat_id,
                field="runtime_due_date",
            ),
            extra_attributes_fn=partial(
                _filter_forecast_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_max_age_due_date",
            ),
            name="Filter max age due date",
            translation_key="filter_max_age_due_date",
            device_class=SensorDeviceClass.DATE,
            entity_category=EntityCategory.DIAGNOSTIC,
            available_fn=partial(
                _filter_forecast_available,
                thermostat_id=thermostat_id,
                field="max_age_due_date",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_max_age_due_date",
            ),
            value_fn=partial(
                _filter_forecast_value,
                thermostat_id=thermostat_id,
                field="max_age_due_date",
            ),
            extra_attributes_fn=partial(
                _filter_forecast_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_due_date",
            ),
            name="Filter due date",
            translation_key="filter_due_date",
            device_class=SensorDeviceClass.DATE,
            available_fn=partial(
                _filter_forecast_available,
                thermostat_id=thermostat_id,
                field="due_date",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_due_date",
            ),
            value_fn=partial(
                _filter_forecast_value,
                thermostat_id=thermostat_id,
                field="due_date",
            ),
            extra_attributes_fn=partial(
                _filter_forecast_snapshot_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
        BeestatSensorEntityDescription(
            key=thermostat_entity_unique_id(
                thermostat_id,
                "filter_days_remaining",
            ),
            name="Filter days remaining",
            translation_key="filter_days_remaining",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.DAYS,
            state_class=SensorStateClass.MEASUREMENT,
            available_fn=partial(
                _filter_forecast_available,
                thermostat_id=thermostat_id,
                field="days_remaining",
            ),
            suggested_object_id=thermostat_suggested_object_id(
                thermostat,
                "filter_days_remaining",
            ),
            value_fn=partial(
                _filter_forecast_value,
                thermostat_id=thermostat_id,
                field="days_remaining",
            ),
            extra_attributes_fn=partial(
                _filter_forecast_attributes,
                thermostat_id=thermostat_id,
            ),
        ),
    )


def _summary_value(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    field: str,
) -> SensorValue:
    data: BeestatRuntimeData | None = coordinator.data
    if data is None or thermostat_id not in data.thermostats:
        return None
    return cast(SensorValue, getattr(data.thermostats[thermostat_id], field))


def _summary_available(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> bool:
    data: BeestatRuntimeData | None = coordinator.data
    return data is not None and thermostat_id in data.thermostats


def _thermostat_metadata(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> ThermostatMetadata | None:
    data: BeestatRuntimeData | None = coordinator.data
    if data is None:
        return None
    return data.thermostat_metadata.get(thermostat_id)


def _thermostat_metadata_available(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> bool:
    return _thermostat_metadata(coordinator, thermostat_id) is not None


def _thermostat_metadata_value(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    field: str,
) -> SensorValue:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return cast(SensorValue, getattr(metadata, field))


def _thermostat_config(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> ConfiguredThermostat | None:
    data: BeestatRuntimeData | None = coordinator.data
    if data is None:
        return None
    return next(
        (
            thermostat
            for thermostat in data.config.thermostats
            if thermostat.thermostat_id == thermostat_id
        ),
        None,
    )


def _room_temperature_spread_value(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> float | None:
    data = coordinator.data
    if (
        data is None
        or (projection := data.room_temperature_spreads.get(thermostat_id)) is None
    ):
        return None
    return projection.value


def _room_temperature_spread_available(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> bool:
    return _room_temperature_spread_value(coordinator, thermostat_id) is not None


def _room_temperature_spread_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    data = coordinator.data
    if (
        data is None
        or (projection := data.room_temperature_spreads.get(thermostat_id)) is None
    ):
        return None
    return {
        "participating_sensor_count": projection.participating_sensor_count,
        "valid_sensor_count": projection.valid_sensor_count,
        "participating_sensor_names": list(projection.participating_sensor_names),
        "unavailable_sensor_names": list(projection.unavailable_sensor_names),
        "hottest_sensor_name": projection.hottest_sensor_name,
        "coldest_sensor_name": projection.coldest_sensor_name,
    }


def _thermostat_setting_value(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    key: str,
    value_type: str,
) -> SensorValue:
    data = coordinator.data
    if (
        data is None
        or (snapshot := data.thermostat_settings.get(thermostat_id)) is None
    ):
        return None
    if value_type == "temperature":
        return temperature_fahrenheit(snapshot, key)
    if value_type == "integer":
        return integer_setting(snapshot, key)
    if value_type == "text":
        return text_setting(snapshot, key)
    return None


def _thermostat_setting_available(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    key: str,
    value_type: str,
) -> bool:
    return (
        _thermostat_setting_value(
            coordinator,
            thermostat_id,
            key,
            value_type,
        )
        is not None
    )


def _filter_forecast(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> FilterForecast | None:
    data: BeestatRuntimeData | None = coordinator.data
    thermostat = _thermostat_config(coordinator, thermostat_id)
    if data is None or thermostat is None:
        return None
    today = data.projected_at.astimezone(coordinator.local_tz).date()
    return build_filter_forecast(
        thermostat,
        data.thermostats.get(thermostat_id),
        today=today,
    )


def _filter_forecast_value(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    field: str,
) -> SensorValue:
    forecast = _filter_forecast(coordinator, thermostat_id)
    if forecast is None:
        return None
    return cast(SensorValue, getattr(forecast, field))


def _filter_forecast_available(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    field: str,
) -> bool:
    return _filter_forecast_value(coordinator, thermostat_id, field) is not None


def _filter_forecast_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    forecast = _filter_forecast(coordinator, thermostat_id)
    if forecast is None:
        return None
    return {
        "changed_source": forecast.changed_source,
        "lifetime_runtime_hours": forecast.lifetime_runtime_hours,
        "max_age_days": forecast.max_age_days,
        "notice_days": forecast.notice_days,
        "remaining_runtime_hours": forecast.remaining_runtime_hours,
        "runtime_due_date": (
            forecast.runtime_due_date.isoformat()
            if forecast.runtime_due_date is not None
            else None
        ),
        "due_date": forecast.due_date.isoformat()
        if forecast.due_date is not None
        else None,
    }


def _filter_forecast_snapshot_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    """Return one complete, coherent filter forecast for consumer automation."""

    forecast = _filter_forecast(coordinator, thermostat_id)
    if forecast is None:
        return None
    return {
        "forecast_revision": filter_forecast_revision(forecast),
        "changed_date": (
            forecast.changed_date.isoformat()
            if forecast.changed_date is not None
            else None
        ),
        "changed_at": (
            forecast.changed_at.isoformat() if forecast.changed_at is not None else None
        ),
        "changed_source": forecast.changed_source,
        "runtime_hours": forecast.runtime_hours,
        "recent_runtime_hours_per_day": forecast.recent_runtime_hours_per_day,
        "lifetime_runtime_hours": forecast.lifetime_runtime_hours,
        "remaining_runtime_hours": forecast.remaining_runtime_hours,
        "runtime_due_date": (
            forecast.runtime_due_date.isoformat()
            if forecast.runtime_due_date is not None
            else None
        ),
        "max_age_days": forecast.max_age_days,
        "max_age_due_date": (
            forecast.max_age_due_date.isoformat()
            if forecast.max_age_due_date is not None
            else None
        ),
        "due_date": (
            forecast.due_date.isoformat() if forecast.due_date is not None else None
        ),
        "days_remaining": forecast.days_remaining,
        "notice_days": forecast.notice_days,
        "due": forecast.due,
        "due_soon": forecast.due_soon,
    }


def _comfort_profile_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {
        "profile_ref": metadata.current_climate_ref,
        "profile_sensors": list(metadata.current_profile_sensor_names),
    }


def _scheduled_profile_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {
        "scheduled_profile_ref": metadata.scheduled_climate_ref,
        "current_profile": metadata.current_climate_name,
        "current_profile_ref": metadata.current_climate_ref,
        "next_scheduled_profile": metadata.next_scheduled_climate_name,
        "next_scheduled_profile_ref": metadata.next_scheduled_climate_ref,
        "next_scheduled_at": _isoformat(metadata.next_scheduled_at),
        "profiles": _schedule_profile_attributes(metadata),
    }


def _next_scheduled_profile_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {
        "next_scheduled_profile": metadata.next_scheduled_climate_name,
        "next_scheduled_profile_ref": metadata.next_scheduled_climate_ref,
        "scheduled_profile": metadata.scheduled_climate_name,
        "scheduled_profile_ref": metadata.scheduled_climate_ref,
    }


def _schedule_profile_attributes(metadata: ThermostatMetadata) -> list[dict[str, Any]]:
    return [
        schedule_profile_payload(profile, include_none=True)
        for profile in metadata.schedule_profiles
    ]


def _data_window_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {
        "data_begin": _isoformat(metadata.data_begin),
        "data_end": _isoformat(metadata.data_end),
    }


def _active_alert_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {"active_alerts": active_alert_examples(metadata.active_alerts)}


def _active_alert_category(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> str | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return _classify_active_alerts(metadata.active_alerts)


def _active_alert_category_attributes(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
) -> dict[str, Any] | None:
    metadata = _thermostat_metadata(coordinator, thermostat_id)
    if metadata is None:
        return None
    return {
        "alert_category": _classify_active_alerts(metadata.active_alerts),
        "active_alerts": active_alert_examples(metadata.active_alerts),
    }


def _classify_active_alerts(alerts: tuple[dict[str, Any], ...]) -> str:
    return classify_active_alerts(alerts)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _mapping_summary_attributes(
    data: BeestatRuntimeData | None,
) -> dict[str, int | None]:
    """Return compact HomeKit mapping counts for the status sensor."""

    if data is None:
        return {
            "thermostat_count": None,
            "mapped_thermostat_count": None,
            "unmapped_thermostat_count": None,
            "local_thermostat_count": None,
            "room_sensor_count": None,
            "mapped_room_sensor_count": None,
            "unmapped_room_sensor_count": None,
            "local_room_sensor_count": None,
        }

    thermostat_count = len(data.config.thermostats)
    mapped_thermostat_count = sum(
        1 for thermostat in data.config.thermostats if thermostat.device_id is not None
    )
    room_sensor_count = len(data.config.sensors)
    mapped_room_sensor_count = sum(
        1 for sensor in data.config.sensors if sensor.device_id is not None
    )
    return {
        "thermostat_count": thermostat_count,
        "mapped_thermostat_count": mapped_thermostat_count,
        "unmapped_thermostat_count": thermostat_count - mapped_thermostat_count,
        "local_thermostat_count": data.config.local_thermostat_count,
        "room_sensor_count": room_sensor_count,
        "mapped_room_sensor_count": mapped_room_sensor_count,
        "unmapped_room_sensor_count": room_sensor_count - mapped_room_sensor_count,
        "local_room_sensor_count": data.config.local_room_sensor_count,
    }
