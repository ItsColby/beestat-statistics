"""Effective config-row identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
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
