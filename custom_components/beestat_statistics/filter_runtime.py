"""Source coverage and conservative observed filter runtime, without I/O."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import ceil, floor, fsum, isfinite
from typing import Any
from zoneinfo import ZoneInfo

_STEP = 300


@dataclass(frozen=True, slots=True)
class ChangeDayObservation:
    """One bounded raw day; absent intervals remain unknown exposure."""

    observed_seconds: float | None
    gap_seconds: float
    boundary_uncertainty_seconds: float
    boundary_status: str
    baseline_seconds: float | None
    source_data_end: datetime | None


@dataclass(frozen=True, slots=True)
class FilterRuntimeObservation:
    """Observed exposure and the independent quality of its source coverage."""

    observed_seconds: float | None
    coverage: str
    unknown_interval_seconds: float | None
    boundary_uncertainty_seconds: float
    boundary_status: str
    source_data_end: datetime | None
    # Source gaps and boundary uncertainty, excluding elapsed unreported time.
    source_unknown_interval_seconds: float | None = None

    @property
    def observed_hours(self) -> float | None:
        """Round down so the displayed observed counter remains a lower bound."""
        if self.observed_seconds is None:
            return None
        return floor(self.observed_seconds / 360) / 10

    @property
    def is_lower_bound(self) -> bool:
        return (
            self.coverage != "complete"
            or self.unknown_interval_seconds != 0
            or (self.observed_seconds is not None and self.observed_seconds % 360 != 0)
        )

    def threshold_reached(self, lifetime_hours: float) -> bool | None:
        if self.observed_seconds is None:
            return None
        threshold = lifetime_hours * 3600
        if self.observed_seconds >= threshold:
            return True
        if (
            self.unknown_interval_seconds is not None
            and self.observed_seconds + self.unknown_interval_seconds < threshold
        ):
            return False
        return None


@dataclass(frozen=True, slots=True)
class RecentRuntimeRate:
    """A rate from complete past local days only."""

    hours_per_day: float | None
    window_start: date
    window_end: date
    complete_days: int
    excluded_days: int


def next_filter_uncertainty_deadline(
    observation: FilterRuntimeObservation,
    *,
    lifetime_hours: float,
    evaluated_at: datetime,
) -> datetime | None:
    """Reevaluate when unreported exposure can invalidate a not-due proof."""
    if (
        observation.source_data_end is None
        or observation.observed_seconds is None
        or observation.unknown_interval_seconds is None
        or observation.threshold_reached(lifetime_hours) is not False
    ):
        return None
    # The normalized unknown amount already includes the rounded source horizon
    # and replacement boundary. Additional unreported time grows at most 1 s/s.
    remaining = lifetime_hours * 3600 - (
        observation.observed_seconds + observation.unknown_interval_seconds
    )
    if not isfinite(remaining):
        return None
    try:
        # Never round a still-positive proof margin down to an immediate callback.
        return evaluated_at + timedelta(microseconds=ceil(remaining * 1_000_000))
    except OverflowError:
        return None


def parse_source_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except ValueError, OverflowError:
        return None


def finite_nonnegative(value: Any, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    if not isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        return None
    return result


def local_day_bounds(day: date, local_tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Use UTC elapsed time so DST days have 276 or 300 five-minute slots."""
    return (
        datetime.combine(day, time.min, local_tz).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, local_tz).astimezone(UTC),
    )


def _summary_days(rows: Iterable[Mapping[str, Any]]) -> dict[date, Mapping[str, Any]]:
    effective: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            continue
        effective[day] = row
    return {
        day: row for day, row in effective.items() if not _deleted(row.get("deleted"))
    }


def _deleted(value: Any) -> bool:
    return (
        value.lower() in {"true", "1", "yes", "on"}
        if isinstance(value, str)
        else bool(value)
    )


def _point_rows(rows: Iterable[Mapping[str, Any]]) -> dict[datetime, float | None]:
    effective: dict[datetime, float | None] = {}
    for row in rows:
        timestamp = parse_source_datetime(row.get("timestamp"))
        if timestamp is None or timestamp.timestamp() % _STEP:
            continue
        effective[timestamp] = (
            None
            if _deleted(row.get("deleted"))
            else finite_nonnegative(row.get("fan"), _STEP)
        )
    return effective


def assess_change_day(
    rows: Iterable[Mapping[str, Any]],
    changed_at: datetime,
    *,
    local_tz: ZoneInfo,
    source_data_end: datetime | None,
    evaluated_at: datetime,
) -> ChangeDayObservation:
    """Count only intervals wholly after the click, never prorating a bucket."""
    changed_at = changed_at.astimezone(UTC)
    start, end = local_day_bounds(changed_at.astimezone(local_tz).date(), local_tz)
    points = _point_rows(rows)
    actual_end = max(
        (
            stamp
            for stamp, fan in points.items()
            if fan is not None and start <= stamp < end
        ),
        default=None,
    )
    horizon = min(end, evaluated_at.astimezone(UTC))
    if source_data_end is not None:
        horizon = min(
            horizon, source_data_end.astimezone(UTC) + timedelta(seconds=_STEP)
        )
    first = datetime.fromtimestamp(ceil(changed_at.timestamp() / _STEP) * _STEP, UTC)
    stop = datetime.fromtimestamp(floor(horizon.timestamp() / _STEP) * _STEP, UTC)
    expected = max(0, int((stop - first).total_seconds()) // _STEP)
    values = [
        fan
        for stamp, fan in points.items()
        if first <= stamp < stop and fan is not None
    ]
    gap = max(0, expected - len(values)) * _STEP
    uncertainty = max(
        0.0,
        min(
            (first - changed_at).total_seconds(), (horizon - changed_at).total_seconds()
        ),
    )
    click_bucket = datetime.fromtimestamp(
        floor(changed_at.timestamp() / _STEP) * _STEP, UTC
    )
    boundary_status = (
        "finalized"
        if points.get(click_bucket) is not None
        else "source_gap"
        if horizon >= click_bucket + timedelta(seconds=_STEP)
        else "pending_data"
    )
    nearest = datetime.fromtimestamp(
        floor((changed_at.timestamp() + 150) / _STEP) * _STEP, UTC
    )
    prefix = [
        fan
        for stamp, fan in points.items()
        if start <= stamp < nearest and fan is not None
    ]
    prefix_count = max(0, int((nearest - start).total_seconds()) // _STEP)
    baseline = (
        fsum(prefix)
        if len(prefix) == prefix_count and boundary_status == "finalized"
        else None
    )
    if baseline is None and boundary_status == "finalized":
        boundary_status = "source_gap"
    return ChangeDayObservation(
        observed_seconds=fsum(values) if values else None,
        gap_seconds=float(gap),
        boundary_uncertainty_seconds=uncertainty,
        boundary_status=boundary_status,
        baseline_seconds=baseline,
        source_data_end=actual_end,
    )


def _daily_runtime(
    row: Mapping[str, Any] | None,
    duration: float,
    expected: int,
) -> tuple[float | None, float | None]:
    if row is None:
        return None, float(expected * _STEP)
    count = finite_nonnegative(row.get("count"), expected)
    if count is not None and not count.is_integer():
        count = None
    fan = finite_nonnegative(row.get("sum_fan"), duration)
    if fan is None or (count is not None and fan > count * _STEP):
        return None, None
    return fan, (expected - count) * _STEP if count is not None else None


def _daily_observations(
    rows: Iterable[Mapping[str, Any]],
    *,
    changed_date: date,
    local_tz: ZoneInfo,
    horizon: datetime,
) -> Iterable[tuple[date, float | None, float | None]]:
    days = _summary_days(rows)
    day = changed_date + timedelta(days=1)
    last_day = horizon.astimezone(local_tz).date()
    while day <= last_day:
        start, end = local_day_bounds(day, local_tz)
        elapsed = max(0.0, (min(end, horizon) - start).total_seconds())
        expected = floor(elapsed / _STEP)
        if expected:
            fan, gap = _daily_runtime(
                days.get(day), (end - start).total_seconds(), expected
            )
            yield day, fan, gap
        day += timedelta(days=1)


def _source_horizon(
    evaluated_at: datetime, source_data_end: datetime | None
) -> datetime:
    horizon = evaluated_at.astimezone(UTC)
    if source_data_end is not None:
        horizon = min(
            horizon, source_data_end.astimezone(UTC) + timedelta(seconds=_STEP)
        )
    return datetime.fromtimestamp(floor(horizon.timestamp() / _STEP) * _STEP, UTC)


def _boundary_observation(
    changed_date: date,
    changed_at: datetime | None,
    change_day: ChangeDayObservation | None,
    local_tz: ZoneInfo,
) -> FilterRuntimeObservation:
    if changed_at is None:
        start, end = local_day_bounds(changed_date, local_tz)
        uncertainty = (end - start).total_seconds()
        return FilterRuntimeObservation(
            None, "complete", uncertainty, uncertainty, "legacy_date_only", None
        )
    if change_day is None:
        return FilterRuntimeObservation(None, "unknown", None, 0, "pending_data", None)
    return FilterRuntimeObservation(
        change_day.observed_seconds,
        "partial" if change_day.gap_seconds else "complete",
        change_day.gap_seconds + change_day.boundary_uncertainty_seconds,
        change_day.boundary_uncertainty_seconds,
        change_day.boundary_status,
        change_day.source_data_end,
    )


def build_filter_runtime_observation(
    rows: Iterable[Mapping[str, Any]],
    *,
    changed_date: date | None,
    changed_at: datetime | None,
    change_day: ChangeDayObservation | None,
    source_data_end: datetime | None,
    evaluated_at: datetime,
    local_tz: ZoneInfo,
) -> FilterRuntimeObservation:
    """Combine one raw change day with daily summaries, without lifetime point reads."""
    today = evaluated_at.astimezone(local_tz).date()
    if changed_date is None or changed_date > today:
        return FilterRuntimeObservation(
            None, "unknown", None, 0, "pending_data", source_data_end
        )
    boundary = _boundary_observation(changed_date, changed_at, change_day, local_tz)
    values = (
        [boundary.observed_seconds] if boundary.observed_seconds is not None else []
    )
    unknown = boundary.unknown_interval_seconds if source_data_end is not None else None
    source_gaps = boundary.coverage == "partial"
    horizon = _source_horizon(evaluated_at, source_data_end)
    for _day, fan, gap in _daily_observations(
        rows, changed_date=changed_date, local_tz=local_tz, horizon=horizon
    ):
        if fan is not None:
            values.append(fan)
        if gap is None:
            unknown = None
        elif gap > 0:
            source_gaps = True
            if unknown is not None:
                unknown += gap
    total = fsum(values) if values else None
    coverage = (
        "unknown"
        if unknown is None or total is None
        else "partial"
        if source_gaps
        else "complete"
    )
    source_unknown = unknown
    if unknown is not None and source_data_end is not None:
        # A completed cloud bucket is not observation of the still-unreported tail.
        earliest_tail = changed_at or local_day_bounds(changed_date, local_tz)[1]
        unknown += max(
            0.0,
            (
                evaluated_at.astimezone(UTC)
                - max(horizon, earliest_tail.astimezone(UTC))
            ).total_seconds(),
        )
    return FilterRuntimeObservation(
        total,
        coverage,
        unknown,
        boundary.boundary_uncertainty_seconds,
        boundary.boundary_status,
        source_data_end,
        source_unknown_interval_seconds=source_unknown,
    )


def build_recent_runtime_rate(
    rows: Iterable[Mapping[str, Any]],
    *,
    today: date,
    local_tz: ZoneInfo,
    window_days: int,
) -> RecentRuntimeRate:
    """Exclude current, missing, malformed and incomplete days from the mean."""
    days = _summary_days(rows)
    first = today - timedelta(days=window_days)
    last = today - timedelta(days=1)
    values: list[float] = []
    day = first
    while day < today:
        start, end = local_day_bounds(day, local_tz)
        duration = (end - start).total_seconds()
        row = days.get(day)
        if row is not None:
            count = finite_nonnegative(row.get("count"))
            fan = finite_nonnegative(row.get("sum_fan"), duration)
            if count == duration / _STEP and fan is not None:
                values.append(fan)
        day += timedelta(days=1)
    return RecentRuntimeRate(
        round(fsum(values) / 3600 / len(values), 2) if values else None,
        first,
        last,
        len(values),
        window_days - len(values),
    )


def observed_threshold_date(
    rows: Iterable[Mapping[str, Any]],
    *,
    changed_date: date | None,
    change_day: ChangeDayObservation | None,
    lifetime_hours: float,
    local_tz: ZoneInfo,
    source_data_end: datetime | None,
    evaluated_at: datetime,
) -> date | None:
    """First source day proving the observed lower bound reached the limit."""
    if changed_date is None or changed_date > evaluated_at.astimezone(local_tz).date():
        return None
    total = (change_day.observed_seconds or 0.0) if change_day is not None else 0.0
    if total >= lifetime_hours * 3600:
        return changed_date
    for day, fan, _gap in _daily_observations(
        rows,
        changed_date=changed_date,
        local_tz=local_tz,
        horizon=_source_horizon(evaluated_at, source_data_end),
    ):
        if fan is not None:
            total += fan
        if total >= lifetime_hours * 3600:
            return day
    return None
