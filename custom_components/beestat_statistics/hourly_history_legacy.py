"""Read qualified legacy daily evidence through the existing Recorder adapter."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .hourly_history_contract import bounds, utc
from .hourly_import_plan import HourlyStatisticRow, RecorderSnapshot

_DAY = timedelta(days=1)
_READ_WINDOW = timedelta(days=20)
_METADATA_FIELDS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "unit_class",
    "mean_type",
    "has_sum",
)


async def async_read_legacy_days(
    recorder: Any,
    descriptor: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    context: dict[str, Any],
    selection: dict[str, Any],
    check_current: Callable[[], Awaitable[None]],
) -> list[dict[str, Any]]:
    """Return only closed, original-calendar days with native numeric evidence.

    The bounded adapter fences every read and rejects explicit native resets.
    Saved selection timezone and adoption time are required; a current timezone
    is never substituted for missing historical ownership. An ambiguous alias
    binding provides no fallback. Native failures propagate to the service
    boundary, and are never turned into zero or fabricated empty history.
    """

    first, stop = bounds(start, end)
    if not _calendar_owned(selection, context):
        return []
    aliases = descriptor.get("legacy_statistic_ids")
    if (
        not isinstance(aliases, list)
        or len(aliases) != 1
        or not isinstance(aliases[0], str)
        or not aliases[0].startswith("beestat:")
    ):
        return []
    alias = aliases[0]
    zone = ZoneInfo(selection["timezone"])
    days = _eligible_days(
        first, stop, zone, utc(context["evaluated_at"]), utc(selection["adopted_at"])
    )
    if not days:
        return []
    read_start = _midnight(days[0] - _DAY, zone)
    read_end = _midnight(days[-1] + _DAY, zone)
    expected = _expected_metadata(alias, descriptor)
    rows = await _read_rows(
        recorder, alias, read_start, read_end, expected, zone, check_current
    )
    result: list[dict[str, Any]] = []
    for day in days:
        row = rows.get(_midnight(day, zone))
        if row is None:
            continue
        numeric = _daily_numeric(descriptor, row, rows.get(_midnight(day - _DAY, zone)))
        if numeric is not None:
            result.append(
                {
                    "start": _midnight(day, zone).isoformat(),
                    "end": _midnight(day + _DAY, zone).isoformat(),
                    "timezone": selection["timezone"],
                    "timezone_revision": selection["timezone_revision"],
                    "calendar_verified": True,
                    "complete": True,
                    "closed": True,
                    "adoption_day": False,
                    "native_verified": True,
                    "source_ids": [alias],
                    "confidence": [
                        "legacy_native_daily_value",
                        "historical_sample_completeness_unproven",
                    ],
                    **numeric,
                }
            )
    await check_current()
    return result


def _calendar_owned(selection: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return (
        isinstance(selection.get("timezone"), str)
        and "timezone_revision" in selection
        and selection["timezone"] == context["timezone"]
        and selection["timezone_revision"] == context["timezone_revision"]
        and selection.get("adopted_at") is not None
    )


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return utc(datetime.combine(day, time.min, zone), whole_hour=True)


def _eligible_days(
    first: datetime,
    end: datetime,
    zone: ZoneInfo,
    evaluated_at: datetime,
    adopted_at: datetime,
) -> list[date]:
    day = first.astimezone(zone).date()
    adoption_day = adopted_at.astimezone(zone).date()
    days: list[date] = []
    while day < adoption_day and (start := _midnight(day, zone)) < end:
        stop = _midnight(day + _DAY, zone)
        if first <= start and stop <= min(end, evaluated_at):
            days.append(day)
        day += _DAY
    return days


def _expected_metadata(alias: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    kind = descriptor["kind"]
    if kind == "runtime":
        unit, unit_class = "h", "duration"
    elif kind == "degree_days":
        unit, unit_class = "degree days", None
    elif kind == "measurement":
        unit, unit_class = (
            descriptor["logical_unit"],
            descriptor["representation"]["unit_class"],
        )
    else:
        raise ValueError("history_legacy_quantity_kind_invalid")
    return {
        "statistic_id": alias,
        "source": "beestat",
        "unit_of_measurement": unit,
        "unit_class": unit_class,
        "mean_type": 1 if kind == "measurement" else 0,
        "has_sum": kind != "measurement",
    }


async def _read_rows(
    recorder: Any,
    alias: str,
    start: datetime,
    end: datetime,
    expected: dict[str, Any],
    zone: ZoneInfo,
    check_current: Callable[[], Awaitable[None]],
) -> dict[datetime, HourlyStatisticRow]:
    rows: dict[datetime, HourlyStatisticRow] = {}
    metadata: dict[str, Any] | None = None
    first = True
    while start < end:
        stop = min(start + _READ_WINDOW, end)
        await check_current()
        snapshot: RecorderSnapshot = await recorder.async_snapshot_range(
            alias, start, stop
        )
        await check_current()
        if not snapshot.complete:
            raise ValueError("history_legacy_snapshot_incomplete")
        actual = _metadata(snapshot.metadata)
        if not first and actual != metadata:
            raise ValueError("history_legacy_metadata_changed")
        if actual is not None and actual != expected:
            raise ValueError("history_legacy_metadata_conflict")
        metadata, first = actual, False
        _add_rows(rows, snapshot, start, stop, zone)
        start = stop
    return rows


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        any(field not in value for field in _METADATA_FIELDS)
        or isinstance(value["mean_type"], bool)
        or not isinstance(value["has_sum"], bool)
    ):
        raise ValueError("history_legacy_metadata_incomplete")
    return {key: value[key] for key in _METADATA_FIELDS}


def _add_rows(
    rows: dict[datetime, HourlyStatisticRow],
    snapshot: RecorderSnapshot,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> None:
    if snapshot.rows and snapshot.metadata is None:
        raise ValueError("history_legacy_rows_without_metadata")
    for row in snapshot.rows:
        stamp = utc(row.start, whole_hour=True)
        if not start <= stamp < end or stamp in rows:
            raise ValueError("history_legacy_snapshot_invalid")
        if stamp.astimezone(zone).timetz().replace(tzinfo=None) != time.min:
            raise ValueError("history_legacy_calendar_mismatch")
        rows[stamp] = row


def _daily_numeric(
    descriptor: Mapping[str, Any],
    row: HourlyStatisticRow,
    previous: HourlyStatisticRow | None,
) -> dict[str, Any] | None:
    if descriptor["kind"] == "measurement":
        return _measurement(row)
    if previous is None or not _counter(row) or not _counter(previous):
        return None
    assert row.sum is not None and previous.sum is not None
    if row.sum < previous.sum:
        return None
    return {
        "method": "legacy_daily_cumulative",
        "predecessor_valid": True,
        "previous_start": previous.start.isoformat(),
        "previous_sum": previous.sum,
        "sum": row.sum,
    }


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _counter(row: HourlyStatisticRow) -> bool:
    return (
        _finite(row.sum)
        and _finite(row.state)
        and row.sum is not None
        and row.sum >= 0
        and row.state == row.sum
        and row.mean is None
        and row.min is None
        and row.max is None
    )


def _measurement(row: HourlyStatisticRow) -> dict[str, Any] | None:
    if not _finite(row.mean) or row.sum is not None or row.state is not None:
        return None
    assert row.mean is not None
    if row.min is not None and (not _finite(row.min) or row.min > row.mean):
        return None
    if row.max is not None and (not _finite(row.max) or row.max < row.mean):
        return None
    return {
        "method": "legacy_sample_mean",
        "mean": row.mean,
        "min": row.min,
        "max": row.max,
    }
