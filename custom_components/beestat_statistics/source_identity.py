"""Narrow native identity proof for an Ecobee thermostat's physical probe."""

from __future__ import annotations

from typing import Any


def is_thermostat_identity_source(entry: Any) -> bool:
    """Identify registry records whose changes can alter physical identity proof."""

    platform = getattr(entry, "platform", None)
    entity_id = getattr(entry, "entity_id", "")
    return (platform == "homekit_controller" and entity_id.startswith("climate.")) or (
        platform == "ecobee"
        and entity_id.startswith("sensor.")
        and str(getattr(entry, "unique_id", "")).endswith("-ei:0-temperature")
    )


def physical_thermostat_probe_devices(
    entity_registry: Any, device_registry: Any
) -> dict[str, str]:
    """Map proven cloud probe entity IDs to their unique HomeKit thermostat.

    Core's Ecobee integration identifies the built-in probe by the thermostat
    identifier and ``ei:0``. HomeKit publishes that identifier as its hardware
    serial. Names, areas, values, and user-entered aliases are never proof.
    """

    if device_registry is None:
        return {}
    entries = tuple(entity_registry.entities.values())
    homekit_by_serial: dict[str, set[str]] = {}
    for entry in entries:
        if (
            getattr(entry, "platform", None) != "homekit_controller"
            or not entry.entity_id.startswith("climate.")
            or not (device_id := getattr(entry, "device_id", None))
        ):
            continue
        device = device_registry.async_get(device_id)
        serial = _identity(getattr(device, "serial_number", None))
        if serial and _is_ecobee(device):
            homekit_by_serial.setdefault(serial, set()).add(device_id)

    candidates: dict[str, tuple[str, str]] = {}
    cloud_by_serial: dict[str, set[str]] = {}
    for entry in entries:
        serial = _physical_probe_serial(entry, device_registry)
        if serial is None:
            continue
        cloud_by_serial.setdefault(serial, set()).add(entry.device_id)
        candidates[entry.entity_id] = (serial, entry.device_id)

    return {
        entity_id: next(iter(homekit_by_serial[serial]))
        for entity_id, (serial, _device_id) in candidates.items()
        if len(homekit_by_serial.get(serial, ())) == 1
        and len(cloud_by_serial[serial]) == 1
    }


def _physical_probe_serial(entry: Any, device_registry: Any) -> str | None:
    if (
        getattr(entry, "platform", None) != "ecobee"
        or not entry.entity_id.startswith("sensor.")
        or not (device_id := getattr(entry, "device_id", None))
    ):
        return None
    device = device_registry.async_get(device_id)
    if not _is_ecobee(device):
        return None
    identifiers = {
        identity
        for domain, value in getattr(device, "identifiers", ())
        if domain == "ecobee" and (identity := _identity(value)) is not None
    }
    if len(identifiers) != 1:
        return None
    serial = next(iter(identifiers))
    if getattr(entry, "unique_id", None) != f"{serial}-ei:0-temperature":
        return None
    return serial


def _identity(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _is_ecobee(device: Any) -> bool:
    manufacturer = _identity(getattr(device, "manufacturer", None))
    return manufacturer is not None and manufacturer.casefold() in {
        "ecobee",
        "ecobee inc.",
        "ecobee inc",
    }
