"""Pure value helpers, errors and constants shared by the hourly importer and writer.

A leaf module: it imports neither side, so both can import it at module level.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .const import HOURLY_V2_SUFFIX, THERMOSTAT_QUANTITIES
from .hourly_history_contract import METADATA_FIELDS
from .hourly_rows import HourlyStatisticRow

MARKER = "hourly_import_contract"


def _history_fence(token: str) -> dict[str, str]:
    return {"kind": "history_invalidated_root", "token": token}


# Every invalidation fence's canonical bytes begin with this; collection keeps them.
HISTORY_FENCE_PREFIX = (
    json.dumps(_history_fence(""), sort_keys=True, separators=(",", ":"))
    .encode()
    .removesuffix(b'""}')
)
_SENSOR_QUANTITIES = frozenset(
    {
        "temperature",
        "occupancy",
        "air_quality",
        "co2_concentration",
        "voc_concentration",
    }
)


class HourlyImportError(ValueError):
    """An explicit reconciliation or selection is required before more effects."""


class HourlyReconciliationError(HourlyImportError):
    """A valid saved hourly intent cannot yet prove its native Recorder effects."""


@dataclass(frozen=True, slots=True)
class WriterPartition:
    """Detached ownership projection; membership does not enable a quantity."""

    legacy_statistic_ids: frozenset[str]
    has_hourly: bool
    hourly_blocked_reason: str | None = None


def _time(value: str | datetime) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise HourlyImportError("An aware whole UTC hour is required")
    instant = instant.astimezone(UTC)
    if instant.minute or instant.second or instant.microsecond:
        raise HourlyImportError("An aware whole UTC hour is required")
    return instant


def _iso(value: datetime) -> str:
    return _time(value).isoformat()


def _row(row: HourlyStatisticRow | None) -> dict[str, Any] | None:
    return None if row is None else {**asdict(row), "start": _iso(row.start)}


def _native(row: dict[str, Any]) -> HourlyStatisticRow:
    return HourlyStatisticRow(**{**row, "start": _time(row["start"])})


def _metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return (
        None
        if metadata is None
        else {key: metadata.get(key) for key in METADATA_FIELDS}
    )


def _resource(value: dict[str, Any]) -> tuple[Any, ...]:
    return value.get("thermostat_id"), value.get("sensor_id"), value.get("quantity")


def _validate_resource(
    value: Any, *, allow_unmapped_sensor: bool = False
) -> tuple[int | None, int | None, str]:
    if not isinstance(value, dict):
        raise TypeError("Invalid source resource")
    thermostat_id, sensor_id, quantity = _resource(value)
    unmapped_sensor = (
        allow_unmapped_sensor and thermostat_id is None and sensor_id is not None
    )
    if (
        (not unmapped_sensor and (type(thermostat_id) is not int or thermostat_id <= 0))
        or (sensor_id is not None and (type(sensor_id) is not int or sensor_id <= 0))
        or not isinstance(quantity, str)
        or quantity
        not in (THERMOSTAT_QUANTITIES if sensor_id is None else _SENSOR_QUANTITIES)
    ):
        raise ValueError("Invalid source resource")
    return thermostat_id, sensor_id, quantity


def _partition_resources(
    identity: dict[str, Any],
) -> tuple[dict[tuple[int | None, int | None, str], str], dict[int, int | None]]:
    current: dict[tuple[int | None, int | None, str], str] = {}
    parents: dict[int, int | None] = {}
    for base, value in identity["resources"].items():
        resource = _validate_resource(value, allow_unmapped_sensor=True)
        thermostat_id, sensor_id, _quantity = resource
        if (
            not isinstance(base, str)
            or not base.endswith(HOURLY_V2_SUFFIX)
            or resource in current
        ):
            raise ValueError("Ambiguous current quantity identity")
        current[resource] = base
        if sensor_id is not None:
            if sensor_id in parents and parents[sensor_id] != thermostat_id:
                raise ValueError("Configured sensor has conflicting parents")
            parents[sensor_id] = thermostat_id
    supplied_parents = identity.get("sensor_parents", parents)
    if not isinstance(supplied_parents, dict):
        raise TypeError("Invalid observed sensor parents")
    for sensor_id, thermostat_id in supplied_parents.items():
        if (
            type(sensor_id) is not int
            or sensor_id <= 0
            or (
                thermostat_id is not None
                and (type(thermostat_id) is not int or thermostat_id <= 0)
            )
            or (sensor_id in parents and parents[sensor_id] != thermostat_id)
        ):
            raise ValueError("Invalid observed sensor parents")
    return current, {**parents, **supplied_parents}
