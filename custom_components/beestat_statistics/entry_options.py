"""Config-entry option mutation helpers."""

from __future__ import annotations

from datetime import date, datetime
import logging

from .api import BeestatApiError
from .config_payload import update_thermostat_override_options
from .const import (
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGED_DATE,
)


_LOGGER = logging.getLogger(__name__)


class FilterRuntimeSummaryUnavailable(BeestatApiError):
    """Raised when a click-time boundary cannot be established safely."""


async def async_set_filter_changed_date(
    coordinator,
    thermostat_id: int,
    changed_date: date,
) -> None:
    """Persist a manually selected filter date without a click-time baseline."""

    await _async_apply_filter_change(
        coordinator,
        thermostat_id,
        changed_date,
        change_day_runtime_baseline_seconds=None,
    )


async def async_mark_filter_changed(
    coordinator,
    thermostat_id: int,
    changed_at: datetime,
) -> None:
    """Reset filter runtime at the moment the native button is pressed."""

    if changed_at.tzinfo is None:
        raise ValueError("changed_at must be timezone-aware")
    await coordinator.async_refresh_runtime(skip_sync=False)
    changed_date = changed_at.astimezone(coordinator.local_tz).date()
    baseline_seconds = coordinator.filter_runtime_seconds_on_date(
        thermostat_id,
        changed_date,
    )
    if baseline_seconds is None:
        raise FilterRuntimeSummaryUnavailable(
            "Beestat did not return a current-day runtime summary for this thermostat"
        )
    await _async_apply_filter_change(
        coordinator,
        thermostat_id,
        changed_date,
        change_day_runtime_baseline_seconds=baseline_seconds,
    )


async def _async_apply_filter_change(
    coordinator,
    thermostat_id: int,
    changed_date: date,
    *,
    change_day_runtime_baseline_seconds: float | None,
) -> None:
    """Persist one filter change and refresh its derived runtime state."""

    entry = coordinator.config_entry
    new_options = update_thermostat_override_options(
        entry.data,
        entry.options,
        thermostat_id,
        {
            CONF_FILTER_CHANGED_DATE: changed_date.isoformat(),
            CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS: (
                change_day_runtime_baseline_seconds
            ),
        },
    )
    old_options = entry.options
    coordinator.hass.config_entries.async_update_entry(entry, options=new_options)
    try:
        coordinator.async_rebuild_runtime_from_cached_rows()
    except Exception:
        coordinator.hass.config_entries.async_update_entry(entry, options=old_options)
        raise
    try:
        dismissed = await coordinator.async_dismiss_filter_alerts(thermostat_id)
    except BeestatApiError as err:
        _LOGGER.warning(
            "Unable to dismiss Beestat filter alerts for thermostat_id=%s: %s",
            thermostat_id,
            err,
        )
    else:
        if dismissed:
            _LOGGER.info(
                "Dismissed %s Beestat filter alert(s) for thermostat_id=%s",
                dismissed,
                thermostat_id,
            )
