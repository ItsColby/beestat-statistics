"""Native Beestat comfort-profile binary sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alerts import classify_active_alerts
from .config_model import ConfiguredSensor, ConfiguredThermostat
from .const import sensor_entity_unique_id, thermostat_entity_unique_id
from .coordinator import (
    BeestatRuntimeData,
    BeestatRuntimeDataCoordinator,
    SensorMetadata,
    ThermostatMetadata,
)
from .entity import (
    async_add_new_entities,
    room_sensor_device_info,
    service_device_info,
    thermostat_device_info,
    thermostat_suggested_object_id,
)
from .filter_forecast import FilterForecast, build_filter_forecast
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Beestat comfort-profile binary sensors from a config entry."""

    runtime: BeestatStatisticsRuntime = entry.runtime_data
    async_add_new_entities(
        runtime.coordinator,
        async_add_entities,
        _build_entities,
        entry.async_on_unload,
    )


def _build_entities(
    coordinator: BeestatRuntimeDataCoordinator,
) -> list[BinarySensorEntity]:
    data = coordinator.data
    if data is None:
        return []
    entities: list[BinarySensorEntity] = [
        BeestatImportPartialProblemBinarySensor(coordinator),
        BeestatHomeKitMappingIncompleteProblemBinarySensor(coordinator),
    ]
    entities.extend(
        BeestatSensorInUseBinarySensor(coordinator, sensor)
        for sensor in data.config.sensors
    )
    entities.extend(
        BeestatThermostatAlertProblemBinarySensor(
            coordinator,
            thermostat,
        )
        for thermostat in data.config.thermostats
    )
    entities.extend(
        BeestatEquipmentAlertProblemBinarySensor(coordinator, thermostat)
        for thermostat in data.config.thermostats
    )
    entities.extend(
        BeestatFilterDueProblemBinarySensor(coordinator, thermostat)
        for thermostat in data.config.thermostats
    )
    entities.extend(
        BeestatFilterDueSoonProblemBinarySensor(coordinator, thermostat)
        for thermostat in data.config.thermostats
    )
    entities.extend(
        BeestatRuntimeStaleProblemBinarySensor(coordinator, thermostat)
        for thermostat in data.config.thermostats
    )
    entities.extend(
        BeestatCloudDataStaleProblemBinarySensor(coordinator, thermostat)
        for thermostat in data.config.thermostats
    )
    return entities


class BeestatImportPartialProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose partial Recorder import passes as a native diagnostic problem."""

    _attr_has_entity_name = True
    _attr_name = "Import partial"
    _attr_translation_key = "statistics_import_partial"
    _attr_unique_id = "statistics_import_partial"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            "last_import_skipped_runtime_sensor_windows",
            "last_import_skipped_runtime_thermostat_windows",
            "last_import_skipped_windows",
        }
    )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat service device."""

        return service_device_info()

    @property
    def available(self) -> bool:
        """Return if an import result has been recorded."""

        return self.coordinator.last_import_partial is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when the last import skipped one or more windows."""

        return self.coordinator.last_import_partial

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return skipped-window counts from the latest import."""

        return {
            "last_import_skipped_windows": (
                self.coordinator.last_import_skipped_windows
            ),
            "last_import_skipped_runtime_thermostat_windows": (
                self.coordinator.last_import_skipped_runtime_thermostat_windows
            ),
            "last_import_skipped_runtime_sensor_windows": (
                self.coordinator.last_import_skipped_runtime_sensor_windows
            ),
        }


class BeestatHomeKitMappingIncompleteProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose whether any discovered Beestat devices are not HomeKit-backed."""

    _attr_has_entity_name = True
    _attr_name = "HomeKit mapping incomplete"
    _attr_translation_key = "homekit_mapping_incomplete"
    _attr_unique_id = "homekit_mapping_incomplete"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            "local_room_sensor_count",
            "local_thermostat_count",
            "mapped_room_sensor_count",
            "mapped_thermostat_count",
            "room_sensor_count",
            "thermostat_count",
            "unmapped_room_sensor_count",
            "unmapped_thermostat_count",
        }
    )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat service device."""

        return service_device_info()

    @property
    def available(self) -> bool:
        """Return if Beestat runtime data is currently available."""

        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when at least one Beestat device lacks a local mapping."""

        data = self.coordinator.data
        if data is None:
            return None
        summary = _mapping_summary(data)
        return bool(
            summary["unmapped_thermostat_count"]
            or summary["unmapped_room_sensor_count"]
        )

    @property
    def extra_state_attributes(self) -> dict[str, int | None] | None:
        """Return compact HomeKit mapping counts."""

        data = self.coordinator.data
        if data is None:
            return None
        return _mapping_summary(data)


class BeestatSensorInUseBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose whether Beestat reports a sensor as active in the comfort profile."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "beestat_name",
            "sensor_type",
        }
    )

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        sensor: ConfiguredSensor,
    ) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_name = "Sensor in use"
        self._attr_translation_key = "sensor_in_use"
        self._attr_unique_id = sensor_entity_unique_id(sensor.sensor_id, "sensor_in_use")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat room-sensor device."""

        return room_sensor_device_info(self._sensor)

    @property
    def available(self) -> bool:
        """Return if the sensor metadata is currently available."""

        metadata = self._metadata
        return (
            super().available
            and metadata is not None
            and not metadata.deleted
            and not metadata.inactive
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether Beestat reports this sensor as in use."""

        metadata = self._metadata
        if metadata is None:
            return None
        return metadata.in_use and not metadata.deleted and not metadata.inactive

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return compact Beestat source metadata."""

        metadata = self._metadata
        if metadata is None:
            return None
        return {
            "beestat_name": metadata.name,
            "sensor_type": metadata.sensor_type,
        }

    @property
    def _metadata(self) -> SensorMetadata | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.sensor_metadata.get(self._sensor.sensor_id)


class BeestatThermostatAlertProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose whether Beestat/Ecobee reports any active thermostat alert."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            "active_alert_count",
            "active_alerts",
        }
    )

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._attr_name = "Active alert"
        self._attr_translation_key = "active_alert"
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "active_alert",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "active_alert",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat thermostat device."""

        return thermostat_device_info(self._thermostat)

    @property
    def available(self) -> bool:
        """Return if Beestat thermostat metadata is available."""

        return super().available and self._metadata is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when Beestat reports any active thermostat alert."""

        metadata = self._metadata
        if metadata is None:
            return None
        return metadata.active_alert_count > 0

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return active Beestat alert details."""

        metadata = self._metadata
        if metadata is None:
            return None
        return {
            "active_alert_count": metadata.active_alert_count,
            "alert_category": classify_active_alerts(metadata.active_alerts),
            "active_alerts": list(metadata.active_alerts),
        }

    @property
    def _metadata(self) -> ThermostatMetadata | None:
        data: BeestatRuntimeData | None = self.coordinator.data
        if data is None:
            return None
        return data.thermostat_metadata.get(self._thermostat.thermostat_id)


class BeestatEquipmentAlertProblemBinarySensor(BeestatThermostatAlertProblemBinarySensor):
    """Expose equipment-looking Beestat/Ecobee alerts as HA problems."""

    _attr_name = "Equipment alert"
    _attr_translation_key = "equipment_alert"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator, thermostat)
        self._attr_name = "Equipment alert"
        self._attr_translation_key = "equipment_alert"
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "equipment_alert",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "equipment_alert",
        )

    @property
    def is_on(self) -> bool | None:
        """Return true when an active alert looks equipment-related."""

        metadata = self._metadata
        if metadata is None:
            return None
        return classify_active_alerts(metadata.active_alerts) in {
            "equipment",
            "unknown",
        }


class BeestatFilterDueProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose whether the thermostat filter is due now."""

    _attr_has_entity_name = True
    _attr_name = "Filter due"
    _attr_translation_key = "filter_due"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            "changed_source",
            "days_remaining",
            "due_date",
            "notice_days",
            "remaining_runtime_hours",
        }
    )

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "filter_due",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "filter_due",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat thermostat device."""

        return thermostat_device_info(self._thermostat)

    @property
    def available(self) -> bool:
        """Return if the filter forecast is currently available."""

        forecast = self._forecast
        return super().available and forecast is not None and forecast.due is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when the filter due date is today or earlier."""

        forecast = self._forecast
        return forecast.due if forecast is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return filter due context."""

        forecast = self._forecast
        if forecast is None:
            return None
        return {
            "changed_source": forecast.changed_source,
            "days_remaining": forecast.days_remaining,
            "due_date": (
                forecast.due_date.isoformat()
                if forecast.due_date is not None
                else None
            ),
            "notice_days": forecast.notice_days,
            "remaining_runtime_hours": forecast.remaining_runtime_hours,
        }

    @property
    def _forecast(self) -> FilterForecast | None:
        data: BeestatRuntimeData | None = self.coordinator.data
        if data is None:
            return None
        today = data.fetched_at.astimezone(self.coordinator.local_tz).date()
        return build_filter_forecast(
            self._thermostat,
            data.thermostats.get(self._thermostat.thermostat_id),
            today=today,
        )


class BeestatFilterDueSoonProblemBinarySensor(BeestatFilterDueProblemBinarySensor):
    """Expose when a thermostat filter is inside the notice window."""

    _attr_name = "Filter due soon"
    _attr_translation_key = "filter_due_soon"
    _attr_device_class = None

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator, thermostat)
        self._attr_name = "Filter due soon"
        self._attr_translation_key = "filter_due_soon"
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "filter_due_soon",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "filter_due_soon",
        )

    @property
    def is_on(self) -> bool | None:
        """Return true when the filter is within the notice window but not due."""

        forecast = self._forecast
        if forecast is None:
            return None
        return forecast.due_soon and not forecast.due


class BeestatRuntimeStaleProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose stale Beestat runtime summaries as a diagnostic problem."""

    _attr_has_entity_name = True
    _attr_translation_key = "runtime_summary_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"lag_days", "threshold_days"})

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._attr_name = "Runtime summary stale"
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "runtime_summary_stale",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "runtime_summary_stale",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat thermostat device."""

        return thermostat_device_info(self._thermostat)

    @property
    def available(self) -> bool:
        """Return if Beestat runtime summary data is available."""

        return super().available and self._summary is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when the latest summary is more than one day stale."""

        summary = self._summary
        if summary is None or summary.lag_days is None:
            return None
        return summary.lag_days > 1

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return stale-runtime diagnostic context."""

        summary = self._summary
        if summary is None:
            return None
        return {"lag_days": summary.lag_days, "threshold_days": 1}

    @property
    def _summary(self):
        data: BeestatRuntimeData | None = self.coordinator.data
        if data is None:
            return None
        return data.thermostats.get(self._thermostat.thermostat_id)


class BeestatCloudDataStaleProblemBinarySensor(
    CoordinatorEntity[BeestatRuntimeDataCoordinator],
    BinarySensorEntity,
):
    """Expose stale Beestat cloud data windows as a diagnostic problem."""

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_data_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"lag_minutes", "threshold_minutes"})

    def __init__(
        self,
        coordinator: BeestatRuntimeDataCoordinator,
        thermostat: ConfiguredThermostat,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._attr_name = "Cloud data stale"
        self._attr_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "cloud_data_stale",
        )
        self._attr_suggested_object_id = thermostat_suggested_object_id(
            thermostat,
            "cloud_data_stale",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Beestat thermostat device."""

        return thermostat_device_info(self._thermostat)

    @property
    def available(self) -> bool:
        """Return if Beestat thermostat metadata is available."""

        return super().available and self._metadata is not None

    @property
    def is_on(self) -> bool | None:
        """Return true when cloud data is more than two hours stale."""

        metadata = self._metadata
        if metadata is None or metadata.data_lag_minutes is None:
            return None
        return metadata.data_lag_minutes > 120

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return cloud-stale diagnostic context."""

        metadata = self._metadata
        if metadata is None:
            return None
        return {"lag_minutes": metadata.data_lag_minutes, "threshold_minutes": 120}

    @property
    def _metadata(self) -> ThermostatMetadata | None:
        data: BeestatRuntimeData | None = self.coordinator.data
        if data is None:
            return None
        return data.thermostat_metadata.get(self._thermostat.thermostat_id)


def _mapping_summary(data: BeestatRuntimeData) -> dict[str, int]:
    """Return compact HomeKit mapping counts for diagnostics."""

    thermostat_count = len(data.config.thermostats)
    mapped_thermostat_count = sum(
        1
        for thermostat in data.config.thermostats
        if thermostat.device_identifiers or thermostat.device_connections
    )
    room_sensor_count = len(data.config.sensors)
    mapped_room_sensor_count = sum(
        1
        for sensor in data.config.sensors
        if sensor.device_identifiers or sensor.device_connections
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
