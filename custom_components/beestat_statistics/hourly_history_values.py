"""Pure v3 representations of already qualified five-minute observations.

The existing hourly builder remains the single sample parser and arithmetic
owner. This projection changes only native storage representation: component
hours become mean percent, and degree days become mean Fahrenheit departure.
Neither representation needs a cumulative predecessor or synthetic extrema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any

from .const import STATISTIC_MEAN_TYPE_ARITHMETIC, STATISTIC_SOURCE
from .hourly_history_contract import METHOD_VERSION, quantity_id
from .hourly_statistics import HourlyBucket, HourlySeries

_V2_SUFFIX = "_hourly_v2"
_V3_SUFFIX = "_hourly_v3"
_STATISTIC_ID = re.compile(r"beestat:[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class HistorySeries:
    """An explicit physical descriptor, native mean rows and their coverage."""

    descriptor: dict[str, Any]
    metadata: dict[str, Any]
    hours: tuple[HourlyBucket, ...]
    source_rows: int
    rejected_timestamps: int = 0
    blocked_reason: str | None = None

    @property
    def statistic_id(self) -> str:
        """Return the stable v3 Recorder identity."""
        return str(self.metadata["statistic_id"])


def build_history_series(
    series: tuple[HourlySeries, ...],
    identity: dict[str, Any],
    *,
    saved: dict[str, dict[str, Any]] | None = None,
) -> tuple[HistorySeries, ...]:
    """Project v2 builder output without reinterpreting raw source samples.

    ``identity.resources`` maps each input v2 ID to its physical resource and
    quantity. ``saved`` maps physical quantity IDs to committed descriptors;
    those bindings keep their original native IDs when display slugs change.
    Proven new legacy aliases are appended without losing the original alias.
    Account/entry admission and persistence belong to the calling manager.

    This is a description, not admission: unsaved writers remain unselected.
    VOC remains in the returned denominator with blocked admission and no rows.
    """
    resources = identity.get("resources")
    if not isinstance(resources, Mapping):
        raise TypeError("history_quantity_identity_missing")
    bindings = _saved_bindings(saved or {})
    owners = {item["statistic_id"]: key for key, item in bindings.items()}
    aliases = {
        alias: key
        for key, item in bindings.items()
        for alias in item["legacy_statistic_ids"]
    }
    used: set[str] = set()
    parents: dict[int, int] = {
        item["sensor_id"]: item["thermostat_id"]
        for item in bindings.values()
        if item["sensor_id"] is not None
    }
    result: list[HistorySeries] = []
    for item in series:
        resource = resources.get(item.statistic_id)
        if not isinstance(resource, Mapping):
            raise TypeError("history_quantity_identity_missing")
        physical_id = quantity_id(resource)
        if physical_id in used:
            raise ValueError("history_quantity_identity_ambiguous")
        used.add(physical_id)
        _check_parent(resource, parents)
        descriptor, metadata, factor = _descriptor(item, resource)
        if old := bindings.get(physical_id):
            descriptor = _bind_saved(descriptor, old)
            metadata["statistic_id"] = descriptor["statistic_id"]
        native_id = descriptor["statistic_id"]
        if native_id in owners and owners[native_id] != physical_id:
            raise ValueError("history_statistic_identity_collision")
        owners[native_id] = physical_id
        for alias in descriptor["legacy_statistic_ids"]:
            if alias in aliases and aliases[alias] != physical_id:
                raise ValueError("history_legacy_identity_collision")
            aliases[alias] = physical_id
        blocked = item.blocked_reason
        if resource["quantity"] == "voc_concentration":
            blocked = "voc_unit_unresolved"
        if blocked:
            descriptor["blocked_reason"] = blocked
            descriptor["admission"] = "blocked"
        hours = (
            ()
            if resource["quantity"] == "voc_concentration"
            else tuple(_hour(hour, factor) for hour in item.hours)
        )
        result.append(
            HistorySeries(
                descriptor,
                metadata,
                hours,
                item.source_rows,
                item.rejected_timestamps,
                blocked,
            )
        )
    return tuple(result)


def _check_parent(resource: Mapping[str, Any], parents: dict[int, int]) -> None:
    sensor = resource.get("sensor_id")
    if sensor is not None:
        thermostat = resource["thermostat_id"]
        if sensor in parents and parents[sensor] != thermostat:
            raise ValueError("history_sensor_parent_changed")
        parents[sensor] = thermostat


def _descriptor(
    item: HourlySeries, resource: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], float | None]:
    base = item.statistic_id
    quantity = resource["quantity"]
    if _STATISTIC_ID.fullmatch(base) is None or not base.endswith(_V2_SUFFIX):
        raise ValueError("history_source_statistic_identity_invalid")
    if not base.endswith(f"_{quantity}{_V2_SUFFIX}"):
        # Configured sensors use their actual field as the suffix as well.
        raise ValueError("history_source_quantity_mismatch")
    metadata = {
        **item.metadata,
        "source": STATISTIC_SOURCE,
        "has_sum": False,
        "mean_type": STATISTIC_MEAN_TYPE_ARITHMETIC,
    }
    factor: float | None = None
    kind = "measurement"
    logical_unit = str(item.metadata["unit_of_measurement"])
    if quantity.endswith("_runtime_hours"):
        kind, logical_unit, factor = "runtime", "h", 100.0
        metadata.update(unit_of_measurement="%", unit_class="unitless")
        metadata["name"] = (
            str(item.metadata["name"]).removesuffix(" Hourly") + " Hourly Rate"
        )
    elif quantity in {"heating_degree_days", "cooling_degree_days"}:
        kind, logical_unit, factor = "degree_days", "°F·day", 24.0
        metadata.update(unit_of_measurement="°F", unit_class="temperature_delta")
        metadata["name"] = (
            str(item.metadata["name"]).removesuffix(" Hourly") + " Hourly Departure"
        )
    if bool(item.metadata.get("has_sum")) != (factor is not None):
        raise ValueError("history_source_representation_mismatch")
    native = _native_statistic_id(base, kind)
    metadata["statistic_id"] = native
    descriptor = {
        "quantity_id": quantity_id(resource),
        "thermostat_id": resource["thermostat_id"],
        "sensor_id": resource.get("sensor_id"),
        "quantity": quantity,
        "kind": kind,
        "logical_unit": logical_unit,
        "method_version": METHOD_VERSION,
        "statistic_id": native,
        "legacy_statistic_ids": [base.removesuffix(_V2_SUFFIX)],
        "representation": {
            "kind": "arithmetic_mean",
            "native_field": "mean",
            "unit_of_measurement": metadata["unit_of_measurement"],
            "unit_class": metadata.get("unit_class"),
            "logical_multiplier": 1 / factor if factor is not None else 1.0,
            "v2_statistic_id": base,
        },
        "admission": "eligible",
        "writer_status": "unselected",
    }
    return descriptor, metadata, factor


def _native_statistic_id(base: str, kind: str) -> str:
    native = base.removesuffix(_V2_SUFFIX)
    if kind == "runtime":
        native = native.removesuffix("_runtime_hours") + "_runtime_rate"
    elif kind == "degree_days":
        native = native.removesuffix("_degree_days") + "_temperature_departure"
    return native + _V3_SUFFIX


def _saved_bindings(saved: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reject ambiguous persisted owners before considering current inputs."""
    result: dict[str, dict[str, Any]] = {}
    native_owners: set[str] = set()
    alias_owners: set[str] = set()
    parents: dict[int, int] = {}
    for key, descriptor in saved.items():
        if not isinstance(descriptor, dict) or quantity_id(descriptor) != key:
            raise ValueError("history_saved_identity_invalid")
        if descriptor.get("quantity_id") != key:
            raise ValueError("history_saved_identity_invalid")
        _check_parent(descriptor, parents)
        native = descriptor.get("statistic_id")
        if (
            not isinstance(native, str)
            or _STATISTIC_ID.fullmatch(native) is None
            or not native.endswith(_V3_SUFFIX)
        ):
            raise ValueError("history_saved_statistic_identity_invalid")
        if native in native_owners:
            raise ValueError("history_statistic_identity_collision")
        native_owners.add(native)
        aliases = descriptor.get("legacy_statistic_ids")
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(
                not isinstance(alias, str)
                or _STATISTIC_ID.fullmatch(alias) is None
                or alias.endswith((_V2_SUFFIX, _V3_SUFFIX))
                for alias in aliases
            )
        ):
            raise ValueError("history_saved_legacy_identity_invalid")
        if alias_owners.intersection(aliases):
            raise ValueError("history_legacy_identity_collision")
        alias_owners.update(aliases)
        result[key] = deepcopy(descriptor)
    return result


def _bind_saved(current: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    for field in (
        "quantity_id",
        "thermostat_id",
        "sensor_id",
        "quantity",
        "kind",
        "logical_unit",
        "method_version",
    ):
        if saved.get(field) != current[field]:
            raise ValueError("history_saved_quantity_contract_changed")
    old_representation = saved.get("representation")
    if not isinstance(old_representation, dict) or any(
        old_representation.get(key) != value
        for key, value in current["representation"].items()
        if key != "v2_statistic_id"
    ):
        raise ValueError("history_saved_representation_changed")
    old_v2 = old_representation.get("v2_statistic_id")
    if (
        not isinstance(old_v2, str)
        or _STATISTIC_ID.fullmatch(old_v2) is None
        or not old_v2.endswith(f"_{current['quantity']}{_V2_SUFFIX}")
        or old_v2.removesuffix(_V2_SUFFIX) not in saved["legacy_statistic_ids"]
        or saved["statistic_id"] != _native_statistic_id(old_v2, current["kind"])
    ):
        raise ValueError("history_saved_representation_changed")
    return {
        **current,
        "statistic_id": saved["statistic_id"],
        "legacy_statistic_ids": list(
            dict.fromkeys(
                [*saved["legacy_statistic_ids"], *current["legacy_statistic_ids"]]
            )
        ),
        "representation": deepcopy(old_representation),
        "writer_status": saved.get("writer_status", "unselected"),
    }


def _hour(hour: HourlyBucket, factor: float | None) -> HourlyBucket:
    if hour.values is None:
        return hour
    if factor is None:
        return replace(hour, values=dict(hour.values))
    # No rounding, clipping, predecessor, or fabricated min/max is introduced.
    mean = hour.values["increment"] * factor
    return replace(
        hour,
        values={"mean": mean} if isfinite(mean) else None,
        reason=hour.reason if isfinite(mean) else "invalid_slots",
    )
