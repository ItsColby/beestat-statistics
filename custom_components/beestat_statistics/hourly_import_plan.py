"""Preview hourly successor imports without Recorder I/O or production activation.

Snapshot completeness is an explicit caller assertion, never inferred from a
successful query or an empty result. This module supplies no write authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from typing import Any

from .hourly_statistics import HourlySeries

_HOUR = timedelta(hours=1)
_METADATA_FIELDS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "unit_class",
    "mean_type",
    "has_sum",
)


@dataclass(frozen=True, slots=True)
class HourlyStatisticRow:
    """A detached normalized row; timestamps are aware whole UTC hours."""

    start: datetime
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    state: float | None = None
    sum: float | None = None


@dataclass(frozen=True, slots=True)
class RecorderSnapshot:
    """Supplied successor metadata and rows, with explicit acquisition coverage.

    Complete means all rows from the hour preceding the requested window through
    the latest retained row were acquired. It includes an authoritative empty result
    for a new ID. A caller must establish this using a serialized native read; the
    pure planner cannot prove that assertion or that the snapshot remains current.
    """

    rows: tuple[HourlyStatisticRow, ...] = ()
    metadata: Mapping[str, Any] | None = None
    complete: bool = False


@dataclass(frozen=True, slots=True)
class CumulativeCheckpoint:
    """Caller-owned continuity proof, independent of a rolling fetch window.

    Activation must persist these values after verified native readback, never
    derive a replacement epoch from the oldest currently fetchable raw timestamp.
    None is valid for last_verified_hour only before the epoch's first import.
    """

    epoch_start: datetime
    last_verified_hour: datetime | None = None


@dataclass(frozen=True, slots=True)
class HourCoverage:
    """Detached evidence for one requested hour, including omitted hours."""

    start: datetime
    reason: str
    valid_slots: int
    missing_slots: int
    invalid_slots: int
    duplicate_slots: int


@dataclass(frozen=True, slots=True)
class SeriesImportPlan:
    """Calculated rows and reasons requiring reconciliation before any submission."""

    statistic_id: str
    calculated_rows: tuple[HourlyStatisticRow, ...]
    blocking_reasons: tuple[str, ...]
    stale_starts: tuple[datetime, ...]
    continuity_break: datetime | None
    coverage: tuple[HourCoverage, ...]
    source_rows: int
    rejected_timestamps: int

    @property
    def legacy_statistic_id(self) -> str:
        """Identify the preserved legacy series without querying or changing it."""

        return self.statistic_id.removesuffix("_hourly_v2")

    @property
    def unblocked_rows(self) -> tuple[HourlyStatisticRow, ...]:
        """Return locally reconciled rows, not authorized or native-verified writes."""

        return () if self.blocking_reasons else self.calculated_rows


def plan_hourly_import(
    series: tuple[HourlySeries, ...],
    *,
    snapshots: Mapping[str, RecorderSnapshot],
    checkpoints: Mapping[str, CumulativeCheckpoint],
) -> tuple[SeriesImportPlan, ...]:
    """Reconcile supplied hourly buckets against detached native row projections.

    Checkpoints must be supplied explicitly. No legacy seed, invented baseline,
    automatic reset, deletion or epoch allocation is performed. Cumulative gaps
    withhold the series' entire batch, including its calculable prefix.
    """

    ids = [item.statistic_id for item in series]
    if len(ids) != len(set(ids)):
        raise ValueError("Hourly successor statistic IDs must be unique")
    return tuple(
        _plan_series(item, snapshots.get(item.statistic_id), checkpoints)
        for item in series
    )


def _plan_series(
    item: HourlySeries,
    snapshot: RecorderSnapshot | None,
    checkpoints: Mapping[str, CumulativeCheckpoint],
) -> SeriesImportPlan:
    if not item.statistic_id.startswith("beestat:") or not item.statistic_id.endswith(
        "_hourly_v2"
    ):
        raise ValueError("Only explicit hourly successor IDs may be planned")
    starts = [_utc_hour(hour.start) for hour in item.hours]
    if any(right != left + _HOUR for left, right in pairwise(starts)):
        raise ValueError("Coverage must contain every requested hour in order")
    reasons: set[str] = set()
    if item.blocked_reason:
        reasons.add(item.blocked_reason)
    if item.rejected_timestamps:
        reasons.add("unplaced_source_rows")
    if any(
        hour.reason == "ready"
        and (hour.valid_slots != 12 or hour.missing_slots or hour.invalid_slots)
        for hour in item.hours
    ):
        reasons.add("invalid_source_coverage")
    existing = _snapshot_rows(item, snapshot, reasons)
    if not starts:
        reasons.add("empty_source_window")
        return _result(item, (), reasons, set(), None)
    cumulative = bool(item.metadata.get("has_sum"))
    if cumulative:
        rows, gap = _cumulative_rows(item, existing, checkpoints, reasons)
    else:
        rows = _measurement_rows(item)
        gap = None
    if item.blocked_reason:
        rows = ()
    stale = _stale_rows(item, existing, rows, gap)
    if stale:
        reasons.add("surviving_stale_rows")
    return _result(item, rows, reasons, stale, gap)


def _result(
    item: HourlySeries,
    rows: tuple[HourlyStatisticRow, ...],
    reasons: set[str],
    stale: set[datetime],
    gap: datetime | None,
) -> SeriesImportPlan:
    return SeriesImportPlan(
        item.statistic_id,
        rows,
        tuple(sorted(reasons)),
        tuple(sorted(stale)),
        gap,
        tuple(
            HourCoverage(
                _utc_hour(hour.start),
                hour.reason,
                hour.valid_slots,
                hour.missing_slots,
                hour.invalid_slots,
                hour.duplicate_slots,
            )
            for hour in item.hours
        ),
        item.source_rows,
        item.rejected_timestamps,
    )


def _snapshot_rows(
    item: HourlySeries,
    snapshot: RecorderSnapshot | None,
    reasons: set[str],
) -> dict[datetime, HourlyStatisticRow]:
    if snapshot is None or not snapshot.complete:
        reasons.add("incomplete_recorder_snapshot")
    if snapshot is None:
        return {}
    if snapshot.metadata is None:
        if snapshot.rows:
            reasons.add("missing_recorder_metadata")
    elif any(
        key not in snapshot.metadata or snapshot.metadata[key] != item.metadata.get(key)
        for key in _METADATA_FIELDS
    ):
        reasons.add("recorder_metadata_mismatch")
    result: dict[datetime, HourlyStatisticRow] = {}
    for row in snapshot.rows:
        try:
            start = _utc_hour(row.start)
        except ValueError:
            reasons.add("invalid_recorder_snapshot")
            continue
        if start in result or not _valid_row(row, bool(item.metadata.get("has_sum"))):
            reasons.add("invalid_recorder_snapshot")
        result[start] = row
    return result


def _valid_row(row: HourlyStatisticRow, cumulative: bool) -> bool:
    required = (row.state, row.sum) if cumulative else (row.mean, row.min, row.max)
    if not all(_finite(value) for value in required):
        return False
    if cumulative:
        return (
            row.state is not None
            and row.state >= 0
            and row.sum is not None
            and row.sum >= 0
        )
    return (
        row.min is not None
        and row.mean is not None
        and row.max is not None
        and row.min <= row.mean <= row.max
    )


def _measurement_rows(item: HourlySeries) -> tuple[HourlyStatisticRow, ...]:
    rows = []
    for hour in item.hours:
        if hour.reason != "ready" or hour.values is None:
            continue
        row = HourlyStatisticRow(
            _utc_hour(hour.start),
            mean=hour.values.get("mean"),
            min=hour.values.get("min"),
            max=hour.values.get("max"),
        )
        if not _valid_row(row, False):
            raise ValueError("Ready measurement bucket has invalid statistics")
        rows.append(row)
    return tuple(rows)


def _cumulative_basis(
    item: HourlySeries,
    existing: Mapping[datetime, HourlyStatisticRow],
    checkpoints: Mapping[str, CumulativeCheckpoint],
) -> tuple[float, float] | None:
    first = _utc_hour(item.hours[0].start)
    checkpoint = checkpoints.get(item.statistic_id)
    if checkpoint is None:
        return None
    epoch = _utc_hour(checkpoint.epoch_start)
    if epoch > first:
        return None
    if epoch == first:
        # An epoch cannot silently replace a known earlier cumulative segment.
        return None if any(start < first for start in existing) else (0.0, 0.0)
    verified = checkpoint.last_verified_hour
    if verified is None or _utc_hour(verified) < first - _HOUR:
        return None
    seed = existing.get(first - _HOUR)
    if seed is None or not _valid_row(seed, True):
        return None
    assert seed.state is not None and seed.sum is not None
    return seed.state, seed.sum


def _cumulative_rows(
    item: HourlySeries,
    existing: Mapping[datetime, HourlyStatisticRow],
    checkpoints: Mapping[str, CumulativeCheckpoint],
    reasons: set[str],
) -> tuple[tuple[HourlyStatisticRow, ...], datetime | None]:
    basis = _cumulative_basis(item, existing, checkpoints)
    if basis is None:
        reasons.add("unproven_cumulative_basis")
        return (), _utc_hour(item.hours[0].start)
    state, total = basis
    rows = []
    for hour in item.hours:
        if hour.reason != "ready" or hour.values is None:
            reasons.add("cumulative_source_gap")
            return tuple(rows), _utc_hour(hour.start)
        increment = hour.values.get("increment")
        if not _finite(increment) or increment is None or increment < 0:
            reasons.add("invalid_cumulative_increment")
            return tuple(rows), _utc_hour(hour.start)
        state += increment
        total += increment
        if not isfinite(state) or not isfinite(total):
            reasons.add("cumulative_overflow")
            return tuple(rows), _utc_hour(hour.start)
        rows.append(HourlyStatisticRow(_utc_hour(hour.start), state=state, sum=total))
    return tuple(rows), None


def _stale_rows(
    item: HourlySeries,
    existing: Mapping[datetime, HourlyStatisticRow],
    calculated: tuple[HourlyStatisticRow, ...],
    gap: datetime | None,
) -> set[datetime]:
    first = _utc_hour(item.hours[0].start)
    end = _utc_hour(item.hours[-1].start) + _HOUR
    by_start = {row.start: row for row in calculated}
    if not item.metadata.get("has_sum"):
        return {
            start
            for start in existing
            if first <= start < end and start not in by_start
        }
    stale = {start for start in existing if gap is not None and start >= gap}
    changed = any(
        start not in existing
        or (row.state, row.sum) != (existing[start].state, existing[start].sum)
        for start, row in by_start.items()
    )
    if changed:
        # A correction is safe only when the complete affected native suffix is
        # recalculated. Never hide a changed increment in a later correction jump.
        stale.update(start for start in existing if start >= end)
    return stale


def _utc_hour(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("An aware timestamp is required")
    normalized = value.astimezone(UTC)
    if normalized.minute or normalized.second or normalized.microsecond:
        raise ValueError("A whole UTC hour is required")
    return normalized


def _finite(value: float | None) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False
