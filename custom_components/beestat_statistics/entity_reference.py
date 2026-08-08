"""Stable references to foreign Home Assistant entity-registry entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config_rows import effective_override_items
from .const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_CLIMATE_ENTITY_REF,
    CONF_MOTION_ENTITY_ID,
    CONF_MOTION_ENTITY_REF,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_REF,
    CONF_SENSORS,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_REF,
    CONF_THERMOSTATS,
)

THERMOSTAT_STABLE_ENTITY_FIELDS = (
    CONF_CLIMATE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_MOTION_ENTITY_ID,
)
SENSOR_STABLE_ENTITY_FIELDS = (
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_MOTION_ENTITY_ID,
)

_REFERENCE_FIELDS = {
    CONF_CLIMATE_ENTITY_ID: CONF_CLIMATE_ENTITY_REF,
    CONF_TEMPERATURE_ENTITY_ID: CONF_TEMPERATURE_ENTITY_REF,
    CONF_OCCUPANCY_ENTITY_ID: CONF_OCCUPANCY_ENTITY_REF,
    CONF_MOTION_ENTITY_ID: CONF_MOTION_ENTITY_REF,
}


def entity_reference_field(entity_id_field: str) -> str:
    """Return the stable-reference field paired with an entity-ID field."""

    return _REFERENCE_FIELDS[entity_id_field]


def entity_reference_from_registry(
    registry: Any,
    entity_id: str,
) -> dict[str, str] | None:
    """Return a stable reference for a current entity-registry entry."""

    entry = registry.async_get(entity_id)
    if entry is None:
        return None
    identity = _entry_identity(entry)
    registry_entry_id = _nonempty_string(getattr(entry, "id", None))
    if identity is None or registry_entry_id is None:
        return None
    domain, platform, unique_id = identity
    return {
        "registry_entry_id": registry_entry_id,
        "domain": domain,
        "platform": platform,
        "unique_id": unique_id,
    }


def resolve_entity_reference(
    registry: Any,
    reference: Any,
) -> str | None:
    """Resolve a stable reference through UUID, then source identity."""

    identity = _reference_identity(reference)
    if identity is None:
        return None

    registry_entry_id = _mapping_string(reference, "registry_entry_id")
    if registry_entry_id is not None:
        entry = registry.async_get(registry_entry_id)
        if entry is not None and _entry_identity(entry) == identity:
            return _nonempty_string(getattr(entry, "entity_id", None))

    entity_id = registry.async_get_entity_id(*identity)
    if entity_id is None:
        return None
    entry = registry.async_get(entity_id)
    if entry is None or _entry_identity(entry) != identity:
        return None
    return _nonempty_string(getattr(entry, "entity_id", None))


def resolve_override_entity_id(
    registry: Any,
    override: Mapping[str, Any],
    entity_id_field: str,
) -> str | None:
    """Resolve one stored mapping, preferring its stable reference."""

    reference_field = entity_reference_field(entity_id_field)
    if reference_field in override:
        return resolve_entity_reference(registry, override.get(reference_field))
    return _nonempty_string(override.get(entity_id_field))


def mapping_form_defaults(
    registry: Any,
    override: Mapping[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    """Return form defaults with stable references resolved to current IDs."""

    defaults = dict(override)
    for field in fields:
        reference_field = entity_reference_field(field)
        if reference_field not in override:
            continue
        entity_id = resolve_entity_reference(registry, override.get(reference_field))
        if entity_id is None:
            defaults.pop(field, None)
        else:
            defaults[field] = entity_id
    return defaults


def has_explicit_entity_mapping(
    override: Mapping[str, Any],
    fields: tuple[str, ...],
) -> bool:
    """Return whether an override deliberately selects any local entity."""

    return any(
        _nonempty_string(override.get(field)) is not None
        or entity_reference_field(field) in override
        for field in fields
    )


def mapping_updates_with_entity_references(
    registry: Any,
    updates: Mapping[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    """Attach stable references to user-confirmed entity selections."""

    result = dict(updates)
    for field in fields:
        if field not in updates:
            continue
        reference_field = entity_reference_field(field)
        entity_id = _nonempty_string(updates.get(field))
        if entity_id is None:
            result[reference_field] = None
            continue
        reference = entity_reference_from_registry(registry, entity_id)
        if reference is None:
            raise ValueError(f"Entity registry entry unavailable for {field}")
        result[reference_field] = reference
    return result


def migrate_option_entity_references(
    registry: Any,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Backfill stable references for UI-owned mapping option rows."""

    migrated = dict(options)
    for key, fields in (
        (CONF_THERMOSTATS, THERMOSTAT_STABLE_ENTITY_FIELDS),
        (CONF_SENSORS, SENSOR_STABLE_ENTITY_FIELDS),
    ):
        value = options.get(key)
        if not isinstance(value, list):
            continue
        rows: list[Any] = []
        for value_item in value:
            if not isinstance(value_item, dict):
                rows.append(value_item)
                continue
            item = dict(value_item)
            for field in fields:
                reference_field = entity_reference_field(field)
                if reference_field in item:
                    continue
                entity_id = _nonempty_string(item.get(field))
                if entity_id is None:
                    continue
                reference = entity_reference_from_registry(registry, entity_id)
                if reference is not None:
                    item[reference_field] = reference
            rows.append(item)
        migrated[key] = rows
    return migrated


def configured_entity_references(
    config_data: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return all enabled stable source references in effective config."""

    references: list[Mapping[str, Any]] = []
    for key, fields in (
        (CONF_THERMOSTATS, THERMOSTAT_STABLE_ENTITY_FIELDS),
        (CONF_SENSORS, SENSOR_STABLE_ENTITY_FIELDS),
    ):
        for item in effective_override_items(config_data.get(key)):
            if item.get("enabled") is False:
                continue
            for field in fields:
                reference = item.get(entity_reference_field(field))
                if (
                    isinstance(reference, Mapping)
                    and _reference_identity(reference) is not None
                ):
                    references.append(reference)
    return tuple(references)


def entity_reference_matches_entry(reference: Any, entry: Any) -> bool:
    """Return whether a registry entry has the referenced source identity."""

    identity = _reference_identity(reference)
    return identity is not None and _entry_identity(entry) == identity


def _reference_identity(reference: Any) -> tuple[str, str, str] | None:
    if not isinstance(reference, Mapping):
        return None
    domain = _mapping_string(reference, "domain")
    platform = _mapping_string(reference, "platform")
    unique_id = _mapping_string(reference, "unique_id")
    if domain is None or platform is None or unique_id is None:
        return None
    return domain, platform, unique_id


def _entry_identity(entry: Any) -> tuple[str, str, str] | None:
    entity_id = _nonempty_string(getattr(entry, "entity_id", None))
    domain = _nonempty_string(getattr(entry, "domain", None))
    if domain is None and entity_id is not None and "." in entity_id:
        domain = entity_id.split(".", 1)[0]
    platform = _nonempty_string(getattr(entry, "platform", None))
    unique_id = _nonempty_string(getattr(entry, "unique_id", None))
    if domain is None or platform is None or unique_id is None:
        return None
    return domain, platform, unique_id


def _mapping_string(value: Any, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _nonempty_string(value.get(key))


def _nonempty_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
