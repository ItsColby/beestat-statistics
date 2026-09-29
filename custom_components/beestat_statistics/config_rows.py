"""Effective config-row identity and shared scalar normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .const import CONF_ID


def positive_resource_id(value: Any) -> int | None:
    """Normalize an exact positive source identity without numeric truncation."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except OverflowError, ValueError:
        return None
    return parsed if parsed > 0 else None


def row_resource_id(row: Mapping[str, Any], *fields: str) -> int | None:
    """Return the first field holding an exact positive source identity."""

    for field in fields:
        if (value := positive_resource_id(row.get(field))) is not None:
            return value
    return None


def override_id(item: Mapping[str, Any]) -> int | None:
    """Resolve the first present identity field; malformed owners remain unowned."""

    for key in (CONF_ID, "sensor_id", "thermostat_id"):
        if key in item:
            return positive_resource_id(item[key])
    return None


def effective_override_items(value: Any) -> tuple[dict[str, Any], ...]:
    """Return one effective override per ID using the runtime last-row rule."""

    if not isinstance(value, list):
        return ()
    effective: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        item_id = override_id(item)
        if item_id is not None:
            effective[item_id] = item
    return tuple(effective.values())


def string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)


def safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def safe_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in keys:
        scalar = safe_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    return result


def finite_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None
