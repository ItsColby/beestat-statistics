"""HomeKit-first Beestat thermostat and sensor mapping."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any

from .config_rows import effective_override_items
from .const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_ENABLED,
    CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT,
    CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END,
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGED_AT,
    CONF_FILTER_CHANGED_DATE,
    CONF_FILTER_CHANGED_ENTITY_ID,
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
    CONF_OVERRIDE_NAME,
    CONF_SENSORS,
    CONF_SLUG,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTAT_ID,
    CONF_THERMOSTATS,
    DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
    DEFAULT_FILTER_MAX_AGE_DAYS,
    DEFAULT_FILTER_NOTICE_DAYS,
    MAX_FILTER_LIFETIME_RUNTIME_HOURS,
    MAX_FILTER_MAX_AGE_DAYS,
    MAX_FILTER_NOTICE_DAYS,
    STATISTIC_UNIT_CLASS_TEMPERATURE,
    STATISTIC_UNIT_CLASS_UNITLESS,
    UNIT_FAHRENHEIT,
    SensorStatistic,
)
from .entity_reference import (
    SENSOR_STABLE_ENTITY_FIELDS,
    THERMOSTAT_STABLE_ENTITY_FIELDS,
    entity_reference_field,
    has_explicit_entity_mapping,
    resolve_override_entity_id,
)

_AIR_QUALITY_CAPABILITIES = {"airquality", "air_quality"}
_CO2_CAPABILITIES = {"co2", "co2ppm", "co2_concentration"}
_TEMPERATURE_CAPABILITIES = {"temperature"}
_VOC_CAPABILITIES = {"voc", "vocppm", "tvoc", "voc_concentration"}
_HOMEKIT_PLATFORM = "homekit_controller"

_THERMOSTAT_OVERRIDE_ENTITY_DOMAINS = (
    (CONF_CLIMATE_ENTITY_ID, "climate"),
    (CONF_TEMPERATURE_ENTITY_ID, "sensor"),
    (CONF_OCCUPANCY_ENTITY_ID, "binary_sensor"),
    (CONF_MOTION_ENTITY_ID, "binary_sensor"),
    (CONF_FILTER_CHANGED_ENTITY_ID, "input_datetime"),
)
_SENSOR_OVERRIDE_ENTITY_DOMAINS = (
    (CONF_TEMPERATURE_ENTITY_ID, "sensor"),
    (CONF_OCCUPANCY_ENTITY_ID, "binary_sensor"),
    (CONF_MOTION_ENTITY_ID, "binary_sensor"),
)


@dataclass(frozen=True, slots=True)
class LocalEcobeeDevice:
    """A local Ecobee/HomeKit device already configured in Home Assistant."""

    device_id: str
    name: str
    slug: str
    climate_entity_id: str | None
    temperature_entity_id: str | None
    occupancy_entity_id: str | None
    motion_entity_id: str | None
    is_thermostat: bool
    has_ecobee_signal: bool

    @property
    def match_keys(self) -> set[str]:
        """Return normalized labels suitable for matching Beestat names."""

        values = {self.name, self.slug}
        for entity_id in (
            self.climate_entity_id,
            self.temperature_entity_id,
            self.occupancy_entity_id,
            self.motion_entity_id,
        ):
            if entity_id:
                values.add(_entity_object_slug(entity_id))
        return {_slugify(value.removeprefix("ecobee_")) for value in values if value}


@dataclass(frozen=True, slots=True)
class _LocalMatchCandidate:
    """One explicit or automatic local-device candidate for a Beestat source."""

    resource_id: int
    device: LocalEcobeeDevice
    explicit: bool
    priority: int


@dataclass(frozen=True, slots=True)
class MappingDeviceConflict:
    """One explicit mapping that cannot safely own a source device."""

    resource_type: str
    resource_ids: tuple[int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ConfiguredThermostat:
    """One Beestat thermostat mapped to local HomeKit/Ecobee identity."""

    thermostat_id: int
    slug: str
    name: str
    filter_changed_entity_id: str | None = None
    filter_changed_date: date | None = None
    filter_changed_at: datetime | None = None
    filter_change_day_runtime_baseline_seconds: float | None = None
    filter_change_boundary_reconciled_at: datetime | None = None
    filter_change_boundary_source_data_end: datetime | None = None
    filter_lifetime_runtime_hours: float = DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS
    filter_max_age_days: int = DEFAULT_FILTER_MAX_AGE_DAYS
    filter_notice_days: int = DEFAULT_FILTER_NOTICE_DAYS
    climate_entity_id: str | None = None
    temperature_entity_id: str | None = None
    occupancy_entity_id: str | None = None
    motion_entity_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfiguredSensor:
    """One Beestat sensor mapped to local HomeKit/Ecobee identity."""

    sensor_id: int
    slug: str
    name: str
    thermostat_id: int | None
    thermostat_slug: str | None
    include_temperature: bool
    include_air_quality: bool
    include_co2: bool
    include_voc: bool
    temperature_entity_id: str | None = None
    occupancy_entity_id: str | None = None
    motion_entity_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class BeestatConfig:
    """Runtime mapping derived from HomeKit metadata, Beestat metadata, and overrides."""

    thermostats: tuple[ConfiguredThermostat, ...]
    sensors: tuple[ConfiguredSensor, ...]
    local_thermostat_count: int = 0
    local_room_sensor_count: int = 0


def filter_boundary_status(thermostat: ConfiguredThermostat) -> str:
    """Return the persisted click-boundary lifecycle state."""

    if thermostat.filter_changed_at is not None:
        if (
            thermostat.filter_change_day_runtime_baseline_seconds is not None
            and thermostat.filter_change_boundary_reconciled_at is not None
        ):
            return "finalized"
        return "pending_data"
    if thermostat.filter_change_day_runtime_baseline_seconds is not None:
        return "legacy_snapshot"
    return "legacy_date_only"


def build_beestat_config(
    hass: Any,
    thermostat_rows: tuple[dict[str, Any], ...],
    sensor_rows: tuple[dict[str, Any], ...],
    config_data: dict[str, Any],
) -> BeestatConfig:
    """Build the HomeKit-first runtime thermostat/sensor map."""

    local_devices = _local_ecobee_devices(hass)
    mapping_conflicts = _mapping_device_conflicts_for_hass(hass, config_data)
    conflicted_thermostat_ids = frozenset(
        resource_id
        for conflict in mapping_conflicts
        if conflict.resource_type == "thermostat"
        for resource_id in conflict.resource_ids
    )
    conflicted_sensor_ids = frozenset(
        resource_id
        for conflict in mapping_conflicts
        if conflict.resource_type == "sensor"
        for resource_id in conflict.resource_ids
    )
    thermostat_overrides = _resolved_override_map(
        hass,
        config_data.get(CONF_THERMOSTATS),
        THERMOSTAT_STABLE_ENTITY_FIELDS,
    )
    sensor_overrides = _resolved_override_map(
        hass,
        config_data.get(CONF_SENSORS),
        SENSOR_STABLE_ENTITY_FIELDS,
    )
    thermostats = _build_thermostats(
        hass,
        thermostat_rows,
        thermostat_overrides,
        local_devices,
        conflicted_thermostat_ids,
    )
    sensors = _build_sensors(
        sensor_rows,
        sensor_overrides,
        thermostats,
        local_devices,
        conflicted_sensor_ids,
    )
    return BeestatConfig(
        thermostats=thermostats,
        sensors=sensors,
        local_thermostat_count=sum(
            1 for device in local_devices if device.is_thermostat
        ),
        local_room_sensor_count=sum(
            1 for device in local_devices if not device.is_thermostat
        ),
    )


def build_sensor_statistics(config: BeestatConfig) -> tuple[SensorStatistic, ...]:
    """Build statistic specs for the configured Beestat sensors."""

    specs: list[SensorStatistic] = []
    for sensor in config.sensors:
        if sensor.include_temperature:
            specs.append(
                SensorStatistic(
                    sensor.sensor_id,
                    f"{sensor.slug}_temperature",
                    f"Beestat {sensor.name} Temperature",
                    "temperature",
                    UNIT_FAHRENHEIT,
                    STATISTIC_UNIT_CLASS_TEMPERATURE,
                )
            )
        if sensor.include_air_quality:
            specs.append(
                SensorStatistic(
                    sensor.sensor_id,
                    f"{sensor.slug}_air_quality",
                    f"Beestat {sensor.name} Air Quality",
                    "air_quality",
                    "%",
                    STATISTIC_UNIT_CLASS_UNITLESS,
                )
            )
        if sensor.include_co2:
            specs.append(
                SensorStatistic(
                    sensor.sensor_id,
                    f"{sensor.slug}_co2_concentration",
                    f"Beestat {sensor.name} CO2",
                    "co2_concentration",
                    "ppm",
                    STATISTIC_UNIT_CLASS_UNITLESS,
                )
            )
        if sensor.include_voc:
            specs.append(
                SensorStatistic(
                    sensor.sensor_id,
                    f"{sensor.slug}_voc_concentration",
                    f"Beestat {sensor.name} TVOC",
                    "voc_concentration",
                    "ppb",
                    STATISTIC_UNIT_CLASS_UNITLESS,
                )
            )
        if sensor.occupancy_entity_id is not None:
            specs.append(
                SensorStatistic(
                    sensor.sensor_id,
                    f"{sensor.slug}_occupancy",
                    f"Beestat {sensor.name} Occupancy",
                    "occupancy",
                    "%",
                    STATISTIC_UNIT_CLASS_UNITLESS,
                    100.0,
                )
            )
    return tuple(specs)


def configured_override_entity_ids(
    config_data: Mapping[str, Any],
    *,
    entity_registry: Any | None = None,
) -> tuple[str, ...]:
    """Return entity IDs explicitly referenced by advanced override config."""

    references: list[str] = []
    for item in effective_override_items(config_data.get(CONF_THERMOSTATS)):
        if _is_disabled(item):
            continue
        references.extend(
            entity_id
            for field in (
                CONF_CLIMATE_ENTITY_ID,
                CONF_TEMPERATURE_ENTITY_ID,
                CONF_OCCUPANCY_ENTITY_ID,
                CONF_MOTION_ENTITY_ID,
                CONF_FILTER_CHANGED_ENTITY_ID,
            )
            if (
                entity_id := _configured_entity_id(
                    entity_registry,
                    item,
                    field,
                )
            )
        )
    for item in effective_override_items(config_data.get(CONF_SENSORS)):
        if _is_disabled(item):
            continue
        references.extend(
            entity_id
            for field in (
                CONF_TEMPERATURE_ENTITY_ID,
                CONF_OCCUPANCY_ENTITY_ID,
                CONF_MOTION_ENTITY_ID,
            )
            if (
                entity_id := _configured_entity_id(
                    entity_registry,
                    item,
                    field,
                )
            )
        )
    return tuple(dict.fromkeys(references))


def configured_unresolved_entity_ids(
    config_data: Mapping[str, Any],
    entity_registry: Any,
) -> tuple[str, ...]:
    """Return last-known IDs for stable references that cannot be resolved."""

    unresolved: list[str] = []
    for key, fields in (
        (CONF_THERMOSTATS, THERMOSTAT_STABLE_ENTITY_FIELDS),
        (CONF_SENSORS, SENSOR_STABLE_ENTITY_FIELDS),
    ):
        for item in effective_override_items(config_data.get(key)):
            if _is_disabled(item):
                continue
            for field in fields:
                if entity_reference_field(field) not in item:
                    continue
                if resolve_override_entity_id(entity_registry, item, field) is not None:
                    continue
                unresolved.append(
                    _string_or_none(item.get(field)) or f"unavailable {field}"
                )
    return tuple(dict.fromkeys(unresolved))


def configured_mapping_device_conflicts(
    config_data: Mapping[str, Any],
    entity_registry: Any,
) -> tuple[MappingDeviceConflict, ...]:
    """Return cross-device and duplicate explicit source-device claims."""

    conflicts: list[MappingDeviceConflict] = []
    for key, fields, resource_type in (
        (CONF_THERMOSTATS, THERMOSTAT_STABLE_ENTITY_FIELDS, "thermostat"),
        (CONF_SENSORS, SENSOR_STABLE_ENTITY_FIELDS, "sensor"),
    ):
        claims_by_device: dict[str, list[int]] = {}
        items = sorted(
            _override_map(config_data.get(key)).values(),
            key=lambda item: (
                _row_int(item, CONF_ID, "sensor_id", "thermostat_id") or -1
            ),
        )
        for item in items:
            if _is_disabled(item):
                continue
            resource_id = _row_int(item, CONF_ID, "sensor_id", "thermostat_id")
            if resource_id is None:
                continue
            device_ids = _explicit_registry_device_ids(
                entity_registry,
                item,
                fields,
            )
            if len(device_ids) > 1:
                conflicts.append(
                    MappingDeviceConflict(
                        resource_type=resource_type,
                        resource_ids=(resource_id,),
                        reason="cross_device",
                    )
                )
            for device_id in device_ids:
                claims_by_device.setdefault(device_id, []).append(resource_id)
        conflicts.extend(
            MappingDeviceConflict(
                resource_type=resource_type,
                resource_ids=tuple(sorted(resource_ids)),
                reason="duplicate_device",
            )
            for resource_ids in claims_by_device.values()
            if len(resource_ids) > 1
        )
    return tuple(
        sorted(
            set(conflicts),
            key=lambda item: (item.resource_type, item.resource_ids, item.reason),
        )
    )


def configured_override_entity_domain_errors(
    config_data: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return override entity references that use the wrong Home Assistant domain."""

    errors: list[str] = []
    errors.extend(
        _override_entity_domain_errors(
            config_data.get(CONF_THERMOSTATS),
            _THERMOSTAT_OVERRIDE_ENTITY_DOMAINS,
            item_type="thermostat",
        )
    )
    errors.extend(
        _override_entity_domain_errors(
            config_data.get(CONF_SENSORS),
            _SENSOR_OVERRIDE_ENTITY_DOMAINS,
            item_type="sensor",
        )
    )
    return tuple(dict.fromkeys(errors))


def _override_entity_domain_errors(
    overrides: Any,
    domains: tuple[tuple[str, str], ...],
    *,
    item_type: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    for item in effective_override_items(overrides):
        if _is_disabled(item):
            continue
        item_id = _row_int(item, CONF_ID, "sensor_id", "thermostat_id")
        item_label = f"{item_type} {item_id}" if item_id is not None else item_type
        for field, expected_domain in domains:
            entity_id = _string_or_none(item.get(field))
            if entity_id is None or _entity_domain(entity_id) == expected_domain:
                continue
            errors.append(
                f"{item_label} {field}: {entity_id} (expected {expected_domain})"
            )
    return tuple(errors)


def _build_thermostats(
    hass: Any,
    rows: tuple[dict[str, Any], ...],
    overrides: dict[int, dict[str, Any]],
    local_devices: tuple[LocalEcobeeDevice, ...],
    conflicted_ids: frozenset[int],
) -> tuple[ConfiguredThermostat, ...]:
    items: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    seen: set[int] = set()
    local_thermostats = tuple(
        device for device in local_devices if device.is_thermostat
    )

    for row in sorted(
        rows, key=lambda item: str(_row_int(item, "thermostat_id", "id") or "")
    ):
        thermostat_id = _row_int(row, "thermostat_id", "id")
        if thermostat_id is None:
            continue
        override = overrides.get(thermostat_id, {})
        if _is_disabled(override):
            continue
        if _bool(row.get("inactive")) and thermostat_id not in overrides:
            continue
        items.append((thermostat_id, row, override))
        seen.add(thermostat_id)

    for thermostat_id, override in sorted(overrides.items()):
        if thermostat_id in seen or _is_disabled(override):
            continue
        items.append((thermostat_id, {}, override))

    local_by_id = _resolve_unique_local_matches(
        tuple(
            candidate
            for thermostat_id, row, override in items
            if thermostat_id not in conflicted_ids
            if (
                candidate := _thermostat_match_candidate(
                    thermostat_id,
                    row,
                    override,
                    local_thermostats,
                )
            )
            is not None
        )
    )
    thermostats: list[ConfiguredThermostat] = []
    used_slugs: set[str] = set()
    for thermostat_id, row, override in items:
        thermostats.append(
            _thermostat_from_row(
                hass,
                row,
                override,
                used_slugs,
                thermostat_id,
                local_by_id.get(thermostat_id),
            )
        )

    return tuple(thermostats)


def _thermostat_from_row(
    hass: Any,
    row: dict[str, Any],
    override: dict[str, Any],
    used_slugs: set[str],
    thermostat_id: int,
    local: LocalEcobeeDevice | None,
) -> ConfiguredThermostat:
    fallback_name = (
        _string_or_none(row.get("name"))
        or (local.name if local else None)
        or f"Thermostat {thermostat_id}"
    )
    name = _string_or_none(override.get(CONF_OVERRIDE_NAME)) or (
        local.name if local else fallback_name
    )
    slug_source = _string_or_none(override.get(CONF_SLUG)) or (
        local.slug if local else fallback_name
    )
    slug = _unique_slug(
        slug_source,
        used_slugs,
        fallback=f"thermostat_{thermostat_id}",
        suffix=str(thermostat_id),
    )
    return ConfiguredThermostat(
        thermostat_id=thermostat_id,
        slug=slug,
        name=name,
        filter_changed_entity_id=_filter_changed_entity_id(hass, slug, override),
        filter_changed_date=_date_or_none(override.get(CONF_FILTER_CHANGED_DATE)),
        filter_changed_at=_aware_datetime_or_none(override.get(CONF_FILTER_CHANGED_AT)),
        filter_change_day_runtime_baseline_seconds=_nonnegative_float_or_none(
            override.get(CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS)
        ),
        filter_change_boundary_reconciled_at=_aware_datetime_or_none(
            override.get(CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT)
        ),
        filter_change_boundary_source_data_end=_aware_datetime_or_none(
            override.get(CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END)
        ),
        filter_lifetime_runtime_hours=_bounded_float_or_default(
            override.get(CONF_FILTER_LIFETIME_RUNTIME_HOURS),
            DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
            minimum=1,
            maximum=MAX_FILTER_LIFETIME_RUNTIME_HOURS,
        ),
        filter_max_age_days=_bounded_int_or_default(
            override.get(CONF_FILTER_MAX_AGE_DAYS),
            DEFAULT_FILTER_MAX_AGE_DAYS,
            minimum=1,
            maximum=MAX_FILTER_MAX_AGE_DAYS,
        ),
        filter_notice_days=_bounded_int_or_default(
            override.get(CONF_FILTER_NOTICE_DAYS),
            DEFAULT_FILTER_NOTICE_DAYS,
            minimum=0,
            maximum=MAX_FILTER_NOTICE_DAYS,
        ),
        climate_entity_id=_string_or_none(override.get(CONF_CLIMATE_ENTITY_ID))
        or (local.climate_entity_id if local else None),
        temperature_entity_id=_string_or_none(override.get(CONF_TEMPERATURE_ENTITY_ID))
        or (local.temperature_entity_id if local else None),
        occupancy_entity_id=_string_or_none(override.get(CONF_OCCUPANCY_ENTITY_ID))
        or (local.occupancy_entity_id if local else None),
        motion_entity_id=_string_or_none(override.get(CONF_MOTION_ENTITY_ID))
        or (local.motion_entity_id if local else None),
        device_id=local.device_id if local else None,
    )


def _build_sensors(
    rows: tuple[dict[str, Any], ...],
    overrides: dict[int, dict[str, Any]],
    thermostats: tuple[ConfiguredThermostat, ...],
    local_devices: tuple[LocalEcobeeDevice, ...],
    conflicted_ids: frozenset[int],
) -> tuple[ConfiguredSensor, ...]:
    items: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    seen: set[int] = set()
    thermostat_by_id = {
        thermostat.thermostat_id: thermostat for thermostat in thermostats
    }
    local_sensors = tuple(
        device for device in local_devices if not device.is_thermostat
    )

    for row in sorted(
        rows, key=lambda item: str(_row_int(item, "sensor_id", "id") or "")
    ):
        sensor_id = _row_int(row, "sensor_id", "id")
        if sensor_id is None:
            continue
        override = overrides.get(sensor_id, {})
        if _is_disabled(override):
            continue
        if _bool(row.get("inactive")) and sensor_id not in overrides:
            continue
        items.append((sensor_id, row, override))
        seen.add(sensor_id)

    for sensor_id, override in sorted(overrides.items()):
        if sensor_id in seen or _is_disabled(override):
            continue
        items.append((sensor_id, {}, override))

    local_by_id = _resolve_unique_local_matches(
        tuple(
            candidate
            for sensor_id, row, override in items
            if sensor_id not in conflicted_ids
            if not (
                _is_thermostat_sensor(row)
                and (
                    _row_int(override, CONF_THERMOSTAT_ID)
                    or _row_int(row, "thermostat_id")
                )
                in thermostat_by_id
            )
            if (
                candidate := _sensor_match_candidate(
                    sensor_id,
                    row,
                    override,
                    local_sensors,
                )
            )
            is not None
        )
    )
    sensors: list[ConfiguredSensor] = []
    used_slugs: set[str] = set()
    for sensor_id, row, override in items:
        sensors.append(
            _sensor_from_row(
                row,
                override,
                used_slugs,
                sensor_id,
                thermostat_by_id,
                local_by_id.get(sensor_id),
                mapping_conflicted=sensor_id in conflicted_ids,
                default_include_temperature=_sensor_supports(
                    row,
                    _TEMPERATURE_CAPABILITIES,
                    fallback_field="temperature",
                )
                if row
                else True,
            )
        )

    return tuple(sensors)


def _sensor_from_row(
    row: dict[str, Any],
    override: dict[str, Any],
    used_slugs: set[str],
    sensor_id: int,
    thermostat_by_id: dict[int, ConfiguredThermostat],
    local: LocalEcobeeDevice | None,
    *,
    mapping_conflicted: bool,
    default_include_temperature: bool,
) -> ConfiguredSensor:
    thermostat_id = _row_int(override, CONF_THERMOSTAT_ID) or _row_int(
        row,
        "thermostat_id",
    )
    thermostat = (
        thermostat_by_id.get(thermostat_id) if thermostat_id is not None else None
    )
    if _is_thermostat_sensor(row) and thermostat is not None:
        local = None
        fallback_name = thermostat.name
        fallback_slug = thermostat.slug
        device_id = None if mapping_conflicted else thermostat.device_id
        temperature_entity_id = thermostat.temperature_entity_id
        occupancy_entity_id = thermostat.occupancy_entity_id
        motion_entity_id = thermostat.motion_entity_id
    else:
        fallback_name = _string_or_none(row.get("name")) or f"Sensor {sensor_id}"
        fallback_slug = fallback_name
        device_id = local.device_id if local else None
        temperature_entity_id = local.temperature_entity_id if local else None
        occupancy_entity_id = local.occupancy_entity_id if local else None
        motion_entity_id = local.motion_entity_id if local else None

    name = _string_or_none(override.get(CONF_OVERRIDE_NAME)) or (
        local.name if local else fallback_name
    )
    slug_source = _string_or_none(override.get(CONF_SLUG)) or (
        local.slug if local else fallback_slug
    )
    slug = _unique_slug(
        slug_source,
        used_slugs,
        fallback=f"sensor_{sensor_id}",
        suffix=str(sensor_id),
    )
    return ConfiguredSensor(
        sensor_id=sensor_id,
        slug=slug,
        name=name,
        thermostat_id=thermostat_id,
        thermostat_slug=thermostat.slug if thermostat else None,
        include_temperature=_override_bool(
            override,
            CONF_INCLUDE_TEMPERATURE,
            default_include_temperature,
        ),
        include_air_quality=_override_bool(
            override,
            CONF_INCLUDE_AIR_QUALITY,
            _sensor_supports(
                row, _AIR_QUALITY_CAPABILITIES, fallback_field="air_quality"
            ),
        ),
        include_co2=_override_bool(
            override,
            CONF_INCLUDE_CO2,
            _sensor_supports(
                row, _CO2_CAPABILITIES, fallback_field="co2_concentration"
            ),
        ),
        include_voc=_override_bool(
            override,
            CONF_INCLUDE_VOC,
            _sensor_supports(
                row, _VOC_CAPABILITIES, fallback_field="voc_concentration"
            ),
        ),
        temperature_entity_id=_string_or_none(override.get(CONF_TEMPERATURE_ENTITY_ID))
        or temperature_entity_id,
        occupancy_entity_id=_string_or_none(override.get(CONF_OCCUPANCY_ENTITY_ID))
        or occupancy_entity_id,
        motion_entity_id=_string_or_none(override.get(CONF_MOTION_ENTITY_ID))
        or motion_entity_id,
        device_id=device_id,
    )


def _thermostat_match_candidate(
    thermostat_id: int,
    row: dict[str, Any],
    override: dict[str, Any],
    local_thermostats: tuple[LocalEcobeeDevice, ...],
) -> _LocalMatchCandidate | None:
    explicit_devices = _explicit_local_devices(
        override,
        THERMOSTAT_STABLE_ENTITY_FIELDS,
        local_thermostats,
    )
    if len(explicit_devices) == 1:
        return _LocalMatchCandidate(thermostat_id, explicit_devices[0], True, 3)
    if has_explicit_entity_mapping(override, THERMOSTAT_STABLE_ENTITY_FIELDS):
        return None
    row_key = _slugify(_string_or_none(row.get("name")) or "")
    if row_key:
        local = _select_preferred_local_match(
            tuple(local for local in local_thermostats if row_key in local.match_keys)
        )
        if local is not None:
            return _LocalMatchCandidate(thermostat_id, local, False, 2)
    strong_matches = tuple(
        local for local in local_thermostats if local.has_ecobee_signal
    )
    if len(strong_matches) == 1:
        return _LocalMatchCandidate(thermostat_id, strong_matches[0], False, 1)
    return None


def _sensor_match_candidate(
    sensor_id: int,
    row: dict[str, Any],
    override: dict[str, Any],
    local_sensors: tuple[LocalEcobeeDevice, ...],
) -> _LocalMatchCandidate | None:
    explicit_devices = _explicit_local_devices(
        override,
        SENSOR_STABLE_ENTITY_FIELDS,
        local_sensors,
    )
    if len(explicit_devices) == 1:
        return _LocalMatchCandidate(sensor_id, explicit_devices[0], True, 3)
    if has_explicit_entity_mapping(override, SENSOR_STABLE_ENTITY_FIELDS):
        return None
    row_key = _slugify(_string_or_none(row.get("name")) or "")
    if row_key:
        local = _select_preferred_local_match(
            tuple(local for local in local_sensors if row_key in local.match_keys)
        )
        if local is not None:
            return _LocalMatchCandidate(sensor_id, local, False, 2)
    return None


def _resolve_unique_local_matches(
    candidates: tuple[_LocalMatchCandidate, ...],
) -> dict[int, LocalEcobeeDevice]:
    """Resolve automatic candidates without sharing one foreign source device."""

    resolved: dict[int, LocalEcobeeDevice] = {}
    explicit_by_device: dict[str, list[_LocalMatchCandidate]] = {}
    for candidate in candidates:
        if candidate.explicit:
            explicit_by_device.setdefault(candidate.device.device_id, []).append(
                candidate
            )
    explicitly_claimed = set(explicit_by_device)
    for device_candidates in explicit_by_device.values():
        if len(device_candidates) == 1:
            candidate = device_candidates[0]
            resolved[candidate.resource_id] = candidate.device
    automatic_by_device: dict[str, list[_LocalMatchCandidate]] = {}
    for candidate in candidates:
        if candidate.explicit:
            continue
        automatic_by_device.setdefault(candidate.device.device_id, []).append(candidate)

    for device_id, device_candidates in automatic_by_device.items():
        if device_id in explicitly_claimed:
            continue
        highest_priority = max(candidate.priority for candidate in device_candidates)
        winners = [
            candidate
            for candidate in device_candidates
            if candidate.priority == highest_priority
        ]
        if len(winners) == 1:
            winner = winners[0]
            resolved[winner.resource_id] = winner.device
    return resolved


def _select_preferred_local_match(
    matches: tuple[LocalEcobeeDevice, ...],
) -> LocalEcobeeDevice | None:
    if not matches:
        return None
    strong_matches = tuple(match for match in matches if match.has_ecobee_signal)
    if len(strong_matches) == 1:
        return strong_matches[0]
    if len(strong_matches) > 1:
        return None
    if len(matches) == 1:
        return matches[0]
    return None


def _find_local_by_entity(
    devices: tuple[LocalEcobeeDevice, ...],
    entity_id: str,
) -> LocalEcobeeDevice | None:
    for device in devices:
        if entity_id in {
            device.climate_entity_id,
            device.temperature_entity_id,
            device.occupancy_entity_id,
            device.motion_entity_id,
        }:
            return device
    return None


def _explicit_local_devices(
    override: Mapping[str, Any],
    fields: tuple[str, ...],
    devices: tuple[LocalEcobeeDevice, ...],
) -> tuple[LocalEcobeeDevice, ...]:
    """Return every distinct local device selected by explicit entity fields."""

    matched: dict[str, LocalEcobeeDevice] = {}
    for field in fields:
        entity_id = _string_or_none(override.get(field))
        if entity_id and (local := _find_local_by_entity(devices, entity_id)):
            matched[local.device_id] = local
    return tuple(matched.values())


def _explicit_registry_device_ids(
    registry: Any,
    override: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    """Return distinct registry device IDs selected by explicit entity fields."""

    device_ids: set[str] = set()
    for field in fields:
        entity_id = resolve_override_entity_id(registry, override, field)
        if entity_id is None:
            continue
        entry = registry.async_get(entity_id)
        device_id = getattr(entry, "device_id", None) if entry is not None else None
        if device_id:
            device_ids.add(str(device_id))
    return tuple(sorted(device_ids))


def _local_ecobee_devices(hass: Any) -> tuple[LocalEcobeeDevice, ...]:
    # Keep Home Assistant optional so pure config-model tests can import this module.
    try:
        from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    except ImportError:
        return ()

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entries_by_device: dict[str, list[Any]] = {}
    for entry in _registry_entries(entity_registry):
        if getattr(entry, "platform", None) != _HOMEKIT_PLATFORM:
            continue
        if getattr(entry, "disabled_by", None) is not None:
            continue
        device_id = getattr(entry, "device_id", None)
        if not device_id:
            continue
        entries_by_device.setdefault(device_id, []).append(entry)

    devices: list[LocalEcobeeDevice] = []
    for device_id, entries in entries_by_device.items():
        device = device_registry.async_get(device_id)
        if device is None or getattr(device, "disabled_by", None) is not None:
            continue
        climate_entity_id = _first_entity(entries, "climate")
        temperature_entity_id = _first_temperature_entity(entries)
        occupancy_entity_id = _first_binary_entity(entries, "occupancy")
        motion_entity_id = _first_binary_entity(entries, "motion")
        if climate_entity_id is None and temperature_entity_id is None:
            continue
        has_ecobee_signal = _has_ecobee_signal(device, entries)
        if not has_ecobee_signal and not _is_ecobee_shaped_homekit_device(
            climate_entity_id=climate_entity_id,
            temperature_entity_id=temperature_entity_id,
            occupancy_entity_id=occupancy_entity_id,
            motion_entity_id=motion_entity_id,
        ):
            continue
        name = _local_device_name(
            hass,
            device,
            climate_entity_id=climate_entity_id,
            temperature_entity_id=temperature_entity_id,
        )
        slug = _local_device_slug(
            name,
            climate_entity_id=climate_entity_id,
            temperature_entity_id=temperature_entity_id,
        )
        devices.append(
            LocalEcobeeDevice(
                device_id=device_id,
                name=name,
                slug=slug,
                climate_entity_id=climate_entity_id,
                temperature_entity_id=temperature_entity_id,
                occupancy_entity_id=occupancy_entity_id,
                motion_entity_id=motion_entity_id,
                is_thermostat=climate_entity_id is not None,
                has_ecobee_signal=has_ecobee_signal,
            )
        )
    return tuple(devices)


def _registry_entries(entity_registry: Any) -> list[Any]:
    entities = getattr(entity_registry, "entities", {})
    if hasattr(entities, "values"):
        return list(entities.values())
    return list(entities)


def _has_ecobee_signal(device: Any, entries: list[Any]) -> bool:
    device_values = (
        getattr(device, "manufacturer", None),
        getattr(device, "model", None),
        getattr(device, "model_id", None),
        getattr(device, "name_by_user", None),
        getattr(device, "default_name", None),
        getattr(device, "name", None),
    )
    if any("ecobee" in str(value or "").lower() for value in device_values):
        return True
    return any(
        "ecobee" in str(getattr(entry, "entity_id", "")).lower() for entry in entries
    )


def _is_ecobee_shaped_homekit_device(
    *,
    climate_entity_id: str | None,
    temperature_entity_id: str | None,
    occupancy_entity_id: str | None,
    motion_entity_id: str | None,
) -> bool:
    if climate_entity_id is not None:
        return temperature_entity_id is not None
    return temperature_entity_id is not None and (
        occupancy_entity_id is not None or motion_entity_id is not None
    )


def _first_entity(entries: list[Any], domain: str) -> str | None:
    for entry in entries:
        entity_id = getattr(entry, "entity_id", "")
        if entity_id.startswith(f"{domain}."):
            return entity_id
    return None


def _first_temperature_entity(entries: list[Any]) -> str | None:
    candidates: list[str] = []
    for entry in entries:
        entity_id = getattr(entry, "entity_id", "")
        if not entity_id.startswith("sensor."):
            continue
        if _entry_device_class(entry) == "temperature":
            return entity_id
        if "temperature" in entity_id or "temp" in entity_id:
            candidates.append(entity_id)
    return candidates[0] if candidates else None


def _first_binary_entity(entries: list[Any], device_class: str) -> str | None:
    candidates: list[str] = []
    for entry in entries:
        entity_id = getattr(entry, "entity_id", "")
        if not entity_id.startswith("binary_sensor."):
            continue
        if _entry_device_class(entry) == device_class:
            return entity_id
        if device_class in entity_id:
            candidates.append(entity_id)
    return candidates[0] if candidates else None


def _entry_device_class(entry: Any) -> str:
    for attr in ("original_device_class", "device_class"):
        value = _device_class_value(getattr(entry, attr, None))
        if value:
            return value
    return ""


def _device_class_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).lower()


def _local_device_name(
    hass: Any,
    device: Any,
    *,
    climate_entity_id: str | None,
    temperature_entity_id: str | None,
) -> str:
    if climate_entity_id:
        return _device_name(device) or _title_from_slug(
            _entity_object_slug(climate_entity_id)
        )
    if temperature_entity_id:
        return _clean_local_sensor_name(
            _state_name(hass, temperature_entity_id)
            or _title_from_slug(_entity_object_slug(temperature_entity_id))
        )
    return _device_name(device) or "Ecobee Sensor"


def _local_device_slug(
    name: str,
    *,
    climate_entity_id: str | None,
    temperature_entity_id: str | None,
) -> str:
    if climate_entity_id:
        return _entity_object_slug(climate_entity_id)
    if temperature_entity_id:
        return _clean_local_sensor_slug(_entity_object_slug(temperature_entity_id))
    return _slugify(name)


def _device_name(device: Any) -> str | None:
    for attr in ("name_by_user", "default_name", "name"):
        value = _string_or_none(getattr(device, attr, None))
        if value:
            return _clean_local_sensor_name(value)
    return None


def _state_name(hass: Any, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return _string_or_none(state.attributes.get("friendly_name"))


def _filter_changed_entity_id(
    hass: Any,
    slug: str,
    override: dict[str, Any],
) -> str | None:
    entity_id = _string_or_none(override.get(CONF_FILTER_CHANGED_ENTITY_ID))
    if entity_id:
        return entity_id
    candidate = f"input_datetime.{slug}_hvac_filter_changed"
    states = getattr(hass, "states", None)
    if states is not None and states.get(candidate) is not None:
        return candidate
    return None


def _override_map(value: Any) -> dict[int, dict[str, Any]]:
    overrides: dict[int, dict[str, Any]] = {}
    for item in effective_override_items(value):
        item_id = _row_int(item, CONF_ID, "sensor_id", "thermostat_id")
        if item_id is None:
            continue
        overrides[item_id] = dict(item)
    return overrides


def _mapping_device_conflicts_for_hass(
    hass: Any,
    config_data: Mapping[str, Any],
) -> tuple[MappingDeviceConflict, ...]:
    """Return mapping conflicts when the Home Assistant registry is available."""

    try:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    except ImportError:
        return ()
    return configured_mapping_device_conflicts(config_data, er.async_get(hass))


def _resolved_override_map(
    hass: Any,
    value: Any,
    fields: tuple[str, ...],
) -> dict[int, dict[str, Any]]:
    """Return overrides with stable references resolved to current entity IDs."""

    overrides = _override_map(value)
    try:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    except ImportError:
        return overrides
    registry = er.async_get(hass)
    for item in overrides.values():
        for field in fields:
            if entity_reference_field(field) not in item:
                continue
            resolved = resolve_override_entity_id(registry, item, field)
            if resolved is None:
                item.pop(field, None)
            else:
                item[field] = resolved
    return overrides


def _configured_entity_id(
    registry: Any | None,
    item: Mapping[str, Any],
    field: str,
) -> str | None:
    if registry is not None and field in THERMOSTAT_STABLE_ENTITY_FIELDS:
        resolved = resolve_override_entity_id(registry, item, field)
        if resolved is not None:
            return resolved
        if entity_reference_field(field) in item:
            return _string_or_none(item.get(field))
    return _string_or_none(item.get(field))


def _sensor_supports(
    row: dict[str, Any],
    capability_names: set[str],
    *,
    fallback_field: str,
) -> bool:
    capabilities = _capability_types(row)
    if capabilities:
        return bool(capabilities & capability_names)
    return row.get(fallback_field) not in (None, "")


def _capability_types(row: dict[str, Any]) -> set[str]:
    capabilities = row.get("capability")
    if not isinstance(capabilities, list):
        return set()
    names: set[str] = set()
    for item in capabilities:
        if isinstance(item, dict):
            value = item.get("type") or item.get("name")
        else:
            value = item
        if value in (None, ""):
            continue
        names.add(str(value).replace("-", "_").lower())
    return names


def _is_thermostat_sensor(row: dict[str, Any]) -> bool:
    return str(row.get("type") or "").lower() == "thermostat"


def _unique_slug(
    value: str,
    used_slugs: set[str],
    *,
    fallback: str,
    suffix: str,
) -> str:
    base = _slugify(value) or fallback
    slug = base
    if slug in used_slugs:
        slug = f"{base}_{suffix}"
    counter = 2
    while slug in used_slugs:
        slug = f"{base}_{suffix}_{counter}"
        counter += 1
    used_slugs.add(slug)
    return slug


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return slug.strip("_")


def _entity_object_slug(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1]


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _clean_local_sensor_slug(value: str) -> str:
    slug = value.removeprefix("ecobee_")
    for suffix in (
        "_current_temperature",
        "_temperature",
        "_thermostat_temp",
        "_temp",
    ):
        if slug.endswith(suffix):
            return slug[: -len(suffix)]
    return slug


def _clean_local_sensor_name(value: str) -> str:
    name = value.strip()
    if name.lower().startswith("ecobee "):
        name = name[7:].strip()
    for suffix in (
        " Current Temperature",
        " Thermostat Temperature",
        " Thermostat Temp",
        " Temperature",
        " Temp",
    ):
        if name.lower().endswith(suffix.lower()):
            name = name[: -len(suffix)]
            break
    return name.strip() or value


def _title_from_slug(value: str) -> str:
    return _clean_local_sensor_name(value.replace("_", " ").title())


def _row_int(row: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except OverflowError, TypeError, ValueError:
            continue
    return None


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _date_or_none(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _bounded_float_or_default(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return default
    return parsed if isfinite(parsed) and minimum <= parsed <= maximum else default


def _nonnegative_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) and parsed >= 0 else None


def _aware_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _bounded_int_or_default(
    value: Any,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except OverflowError, TypeError, ValueError:
        return default
    return parsed if minimum <= parsed <= maximum else default


def _override_bool(
    override: dict[str, Any],
    key: str,
    default: bool,
) -> bool:
    if key not in override:
        return default
    return _bool(override[key])


def _is_disabled(override: dict[str, Any]) -> bool:
    return override.get(CONF_ENABLED) is False


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)
