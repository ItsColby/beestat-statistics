"""Shared fixtures for hourly statistics tests."""

from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from custom_components.beestat_statistics.hourly_recorder import HourlyRecorder


def hourly_metadata(statistic_id: str, *, measurement: bool = False) -> dict[str, Any]:
    """Return Beestat external-statistics metadata for a counter or measurement."""
    return {
        "statistic_id": statistic_id,
        "source": "beestat",
        "name": "Hourly statistics fixture",
        "unit_of_measurement": "°F" if measurement else "h",
        "unit_class": "temperature" if measurement else "duration",
        "mean_type": 1 if measurement else 0,
        "has_sum": not measurement,
    }


async def known_beestat_ids(hass: HomeAssistant, recorder: HourlyRecorder) -> set[str]:
    """Read native Beestat statistic IDs after the adapter's queued imports."""
    await recorder.async_barrier()
    native = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_source="beestat")
    )
    return set(native)
