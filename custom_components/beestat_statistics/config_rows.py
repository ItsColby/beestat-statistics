"""Effective config-row identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import CONF_ID


def override_id(item: Mapping[str, Any]) -> int | None:
    """Return a normalized override ID across current and legacy row shapes."""

    for key in (CONF_ID, "sensor_id", "thermostat_id"):
        try:
            value = int(item.get(key, -1))
        except OverflowError, TypeError, ValueError:
            continue
        if value >= 0:
            return value
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
