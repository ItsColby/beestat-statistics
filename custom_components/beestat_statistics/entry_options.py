"""Config-entry option mutation helpers."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .config_payload import update_thermostat_override_options
from .const import (
    CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT,
    CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END,
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGED_AT,
    CONF_FILTER_CHANGED_DATE,
)

_LOGGER = logging.getLogger(__name__)


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
        changed_at=None,
        change_day_runtime_baseline_seconds=None,
        boundary_reconciled_at=None,
        boundary_source_data_end=None,
        rebuild_from_cached_rows=False,
    )


async def async_mark_filter_changed(
    coordinator,
    thermostat_id: int,
    changed_at: datetime,
    *,
    dismiss_alerts: bool = True,
) -> None:
    """Reset filter runtime at the moment the native button is pressed."""

    if changed_at.tzinfo is None:
        raise ValueError("changed_at must be timezone-aware")
    changed_at = changed_at.astimezone(timezone.utc)
    changed_date = changed_at.astimezone(coordinator.local_tz).date()
    await _async_apply_filter_change(
        coordinator,
        thermostat_id,
        changed_date,
        changed_at=changed_at,
        change_day_runtime_baseline_seconds=None,
        boundary_reconciled_at=None,
        boundary_source_data_end=None,
        rebuild_from_cached_rows=True,
        rollback_on_refresh_error=False,
        dismiss_alerts=dismiss_alerts,
    )
    try:
        await coordinator.async_refresh_runtime(
            skip_sync=False,
            summary_window=True,
        )
    except Exception as err:  # noqa: BLE001 - the physical change is already durable
        _LOGGER.warning(
            "Saved filter change; exact Beestat runtime boundary remains pending (%s)",
            type(err).__name__,
        )
        coordinator.async_schedule_filter_boundary_reconcile()


async def _async_apply_filter_change(
    coordinator,
    thermostat_id: int,
    changed_date: date,
    *,
    changed_at: datetime | None,
    change_day_runtime_baseline_seconds: float | None,
    boundary_reconciled_at: datetime | None,
    boundary_source_data_end: datetime | None,
    rebuild_from_cached_rows: bool,
    rollback_on_refresh_error: bool = True,
    dismiss_alerts: bool = True,
) -> None:
    """Persist one filter change and refresh its derived runtime state."""

    entry = coordinator.config_entry
    new_options = update_thermostat_override_options(
        entry.data,
        entry.options,
        thermostat_id,
        {
            CONF_FILTER_CHANGED_DATE: changed_date.isoformat(),
            CONF_FILTER_CHANGED_AT: _isoformat_or_none(changed_at),
            CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS: (
                change_day_runtime_baseline_seconds
            ),
            CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT: _isoformat_or_none(
                boundary_reconciled_at
            ),
            CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END: _isoformat_or_none(
                boundary_source_data_end
            ),
        },
    )
    old_options = entry.options
    coordinator.hass.config_entries.async_update_entry(entry, options=new_options)
    if rebuild_from_cached_rows:
        try:
            coordinator.async_rebuild_runtime_from_cached_rows()
        except Exception as err:  # noqa: BLE001 - persistence is the primary contract
            _LOGGER.warning(
                "Saved filter change but cached runtime state could not be rebuilt (%s)",
                type(err).__name__,
            )
    else:
        try:
            await coordinator.async_refresh_runtime(skip_sync=True)
        except Exception:
            if rollback_on_refresh_error:
                coordinator.hass.config_entries.async_update_entry(
                    entry,
                    options=old_options,
                )
            raise
    if not dismiss_alerts:
        return
    try:
        dismissed = await coordinator.async_dismiss_filter_alerts(thermostat_id)
    except Exception as err:  # noqa: BLE001 - dismissal is explicitly best-effort
        _LOGGER.warning(
            "Unable to dismiss Beestat filter alerts after a filter change; "
            "the local filter change was saved (%s)",
            type(err).__name__,
        )
    else:
        if dismissed:
            _LOGGER.info(
                "Dismissed %s Beestat filter alert(s) after a filter change",
                dismissed,
            )


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("filter boundary timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()
