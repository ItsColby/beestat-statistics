"""Config-entry option mutation helpers."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from .api import exception_fingerprint
from .config_payload import (
    effective_thermostat_override,
    update_thermostat_override_options,
)
from .const import (
    CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT,
    CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END,
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGE_EVENT,
    CONF_FILTER_CHANGED_AT,
    CONF_FILTER_CHANGED_DATE,
)
from .filter_action import FilterChangeEvent, parse_filter_change_event

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .coordinator import BeestatRuntimeDataCoordinator
    from .runtime import BeestatStatisticsConfigEntry


def resolve_filter_change_timestamp(value: datetime, local_tz: ZoneInfo) -> datetime:
    """Resolve one exact filter-change timestamp to UTC without DST guessing."""

    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    if value.tzinfo is not None:
        raise ValueError("filter-change local time is ambiguous or does not exist")

    local_wall_time = value.replace(fold=0)
    candidates = {
        candidate.astimezone(UTC)
        for fold in (0, 1)
        if (candidate := local_wall_time.replace(tzinfo=local_tz, fold=fold))
        .astimezone(UTC)
        .astimezone(local_tz)
        .replace(tzinfo=None, fold=0)
        == local_wall_time
    }
    if len(candidates) != 1:
        raise ValueError("filter-change local time is ambiguous or does not exist")
    return candidates.pop()


async def async_set_filter_changed_date(
    coordinator: BeestatRuntimeDataCoordinator,
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
        dismiss_alerts=False,
        event=_filter_change_event(
            coordinator, thermostat_id, changed_date, None, "date"
        ),
    )


async def async_mark_filter_changed(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    changed_at: datetime,
    *,
    dismiss_alerts: bool = True,
    source: Literal["button", "service", "repair"] = "button",
    request_id: str | None = None,
    expected_boundary: tuple[datetime | None, date | None, str | None] | None = None,
    accepted_interval: tuple[datetime, datetime] | None = None,
) -> dict[str, Any]:
    """Record a replacement or correction, with an optional optimistic guard."""

    if coordinator.is_closed:
        raise asyncio.CancelledError
    if changed_at.tzinfo is None:
        raise ValueError("changed_at must be timezone-aware")
    changed_at = changed_at.astimezone(UTC)
    changed_date = changed_at.astimezone(coordinator.local_tz).date()
    request_id = request_id or uuid4().hex
    current = _saved_filter_options(coordinator, thermostat_id)
    previous_event = parse_filter_change_event(current.get(CONF_FILTER_CHANGE_EVENT))
    if previous_event is not None and previous_event.request_id == request_id:
        if (
            previous_event.changed_at != changed_at.isoformat()
            or previous_event.source != source
        ):
            raise FilterChangeConflictError("request_id_reused")
        return _filter_change_response(
            coordinator, thermostat_id, changed_at, request_id, "already_recorded"
        )
    if accepted_interval is not None:
        earliest, latest = accepted_interval
        if not earliest <= changed_at <= latest:
            raise FilterChangeConflictError("filter_change_boundary_out_of_range")
    if expected_boundary is not None:
        expected_at, expected_date, expected_request_id = expected_boundary
        if (
            current.get(CONF_FILTER_CHANGED_AT) != _isoformat_or_none(expected_at)
            or current.get(CONF_FILTER_CHANGED_DATE)
            != (expected_date.isoformat() if expected_date is not None else None)
            or (previous_event.request_id if previous_event is not None else None)
            != expected_request_id
        ):
            raise FilterChangeConflictError("filter_change_boundary_conflict")
        if source != "repair" and (
            (expected_at is not None and changed_at <= expected_at)
            or (expected_date is not None and changed_date < expected_date)
        ):
            raise FilterChangeConflictError("filter_change_not_after_prior")
    event = _filter_change_event(
        coordinator, thermostat_id, changed_date, changed_at, source, request_id
    )
    # Guard, complete option merge, and persistence contain no await. The first
    # yield occurs only after this action owns a durable boundary and receipt.
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
        event=event,
    )
    try:
        await coordinator.async_refresh_runtime(
            skip_sync=False,
            summary_window=True,
        )
    except Exception as err:  # noqa: BLE001 - the physical change is already durable
        _LOGGER.warning(
            "Saved filter change; exact Beestat runtime boundary remains pending (%s)",
            exception_fingerprint(err),
        )
        coordinator.async_schedule_filter_boundary_reconcile()
    return _filter_change_response(
        coordinator, thermostat_id, changed_at, request_id, "recorded"
    )


class FilterChangeConflictError(ValueError):
    """The caller's prior boundary or request identity no longer owns this cycle."""


def saved_filter_boundary(
    coordinator: BeestatRuntimeDataCoordinator, thermostat_id: int
) -> tuple[datetime | None, date | None, str | None]:
    """Read the persisted guard, independent of a possibly stale projection."""

    row = _saved_filter_options(coordinator, thermostat_id)
    event = parse_filter_change_event(row.get(CONF_FILTER_CHANGE_EVENT))
    changed_at = row.get(CONF_FILTER_CHANGED_AT)
    changed_date = row.get(CONF_FILTER_CHANGED_DATE)
    return (
        datetime.fromisoformat(changed_at).astimezone(UTC)
        if changed_at is not None
        else None,
        date.fromisoformat(changed_date) if changed_date is not None else None,
        event.request_id if event is not None else None,
    )


def _saved_filter_options(
    coordinator: BeestatRuntimeDataCoordinator, thermostat_id: int
) -> dict[str, Any]:
    entry = cast("BeestatStatisticsConfigEntry", coordinator.config_entry)
    return effective_thermostat_override(entry.data, entry.options, thermostat_id) or {}


def _filter_change_event(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    changed_date: date,
    changed_at: datetime | None,
    source: Literal["button", "service", "repair", "date"],
    request_id: str | None = None,
) -> FilterChangeEvent:
    previous = _saved_filter_options(coordinator, thermostat_id)
    prior_event = parse_filter_change_event(previous.get(CONF_FILTER_CHANGE_EVENT))
    return FilterChangeEvent(
        action="replacement" if source in {"button", "service"} else "correction",
        source=source,
        request_id=request_id or uuid4().hex,
        prior_request_id=prior_event.request_id if prior_event is not None else None,
        prior_changed_at=previous.get(CONF_FILTER_CHANGED_AT),
        prior_changed_date=previous.get(CONF_FILTER_CHANGED_DATE),
        changed_at=_isoformat_or_none(changed_at),
        changed_date=changed_date.isoformat(),
        recorded_at=datetime.now(UTC).isoformat(),
    )


def _filter_change_response(
    coordinator: BeestatRuntimeDataCoordinator,
    thermostat_id: int,
    requested_at: datetime,
    request_id: str,
    status: str,
) -> dict[str, Any]:
    saved = _saved_filter_options(coordinator, thermostat_id)
    event = parse_filter_change_event(saved.get(CONF_FILTER_CHANGE_EVENT))
    requested_date = requested_at.astimezone(coordinator.local_tz).date().isoformat()
    if (
        event is None
        or event.request_id != request_id
        or saved.get(CONF_FILTER_CHANGED_AT) != requested_at.isoformat()
        or saved.get(CONF_FILTER_CHANGED_DATE) != requested_date
    ):
        status = "superseded"
    boundary_status = "legacy_date_only"
    if saved.get(CONF_FILTER_CHANGED_AT) is not None:
        boundary_status = (
            "finalized"
            if saved.get(CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT) is not None
            and saved.get(CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS) is not None
            else "pending_data"
        )
    return {
        "schema_version": 1,
        "status": status,
        "config_entry_id": cast(
            "BeestatStatisticsConfigEntry", coordinator.config_entry
        ).entry_id,
        "thermostat_id": thermostat_id,
        "request_id": request_id,
        "requested_changed_at": requested_at.isoformat(),
        "changed_at": saved.get(CONF_FILTER_CHANGED_AT),
        "changed_date": saved.get(CONF_FILTER_CHANGED_DATE),
        "boundary_status": boundary_status,
    }


async def _async_apply_filter_change(
    coordinator: BeestatRuntimeDataCoordinator,
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
    event: FilterChangeEvent,
) -> None:
    """Persist one filter change and refresh its derived runtime state."""

    if coordinator.is_closed:
        raise asyncio.CancelledError
    entry = cast("BeestatStatisticsConfigEntry", coordinator.config_entry)
    new_options = update_thermostat_override_options(
        entry.data,
        entry.options,
        thermostat_id,
        {
            CONF_FILTER_CHANGED_DATE: changed_date.isoformat(),
            CONF_FILTER_CHANGED_AT: _isoformat_or_none(changed_at),
            CONF_FILTER_CHANGE_EVENT: event.as_dict(),
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
                exception_fingerprint(err),
            )
    else:
        try:
            await coordinator.async_refresh_runtime(skip_sync=True)
        except Exception:
            if (
                not coordinator.is_closed
                and rollback_on_refresh_error
                and entry.options == new_options
            ):
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
            exception_fingerprint(err),
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
    return value.astimezone(UTC).isoformat()
