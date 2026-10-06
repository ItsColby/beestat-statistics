"""Import result models and pure helpers shared by the statistics importer."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as dt_date
from math import isfinite
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .config_model import BeestatConfig
from .config_model import (
    build_sensor_statistics as build_sensor_specs,
)
from .config_rows import positive_resource_id, row_resource_id
from .const import (
    API_BASE,
    CONF_API_BASE,
    DETAILED_RUNTIME_FIELDS,
    MAX_POINT_LOOKBACK_DAYS,
    MAX_WINDOW_DAYS,
    RUNTIME_FIELD_GROUPS,
    SUMMARY_MEAN_STATISTICS,
    SUMMARY_SUM_STATISTICS,
    THERMOSTAT_POINT_STATISTICS,
)
from .coordinator import BeestatRuntimeData, TemporalContext
from .hourly_statistics import HourlySeries
from .import_evidence import SkippedWindowEvidence
from .runtime import BeestatStatisticsConfigEntry
from .statistics_builder import CumulativeStatisticSeed, StatisticsSeries
from .url_validation import normalize_api_base


class UnknownThermostatError(ValueError):
    """Raised when a service asks to import an unconfigured thermostat."""

    def __init__(self, thermostat_id: int) -> None:
        super().__init__(f"Unknown Beestat thermostat ID: {thermostat_id}")
        self.thermostat_id = thermostat_id


@dataclass(frozen=True, slots=True)
class SummaryImportPlan:
    """How summary rows should be imported for one import pass."""

    rows: list[dict[str, Any]]
    seeds: dict[str, CumulativeStatisticSeed]
    mode: str
    window_start: dt_date | None
    window_end: dt_date | None
    overlap_days: int | None
    fallback_reason: str | None

    @classmethod
    def full(
        cls,
        rows: list[dict[str, Any]],
        *,
        fallback_reason: str,
    ) -> SummaryImportPlan:
        """Build an unseeded complete baseline with its existing fallback reason."""

        return cls(
            rows=rows,
            seeds={},
            mode="full",
            window_start=None,
            window_end=None,
            overlap_days=None,
            fallback_reason=fallback_reason,
        )


@dataclass(frozen=True, slots=True)
class PreparedImport:
    """One complete statistics import prepared before Recorder effects."""

    summary_plan: SummaryImportPlan
    summary_rows: list[dict[str, Any]]
    skipped_windows: SkippedWindowEvidence
    thermostat_rows_by_id: dict[int, list[dict[str, Any]]]
    sensor_rows_by_id: dict[int, list[dict[str, Any]]]
    series: list[StatisticsSeries]


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary of one import pass."""

    imported_series: int
    imported_rows: int
    source_rows: int
    skipped_windows: int
    skipped_runtime_thermostat_windows: int
    skipped_runtime_sensor_windows: int
    skipped_window_examples: tuple[dict[str, str], ...]
    latest_start_by_statistic_id: dict[str, str | None]
    summary_mode: str
    summary_window_start: str | None
    summary_window_end: str | None
    summary_overlap_days: int | None
    summary_fallback_reason: str | None
    cumulative_seed_count: int
    legacy_imported_series: int = 0
    legacy_imported_rows: int = 0
    hourly_imported_series: int | None = 0
    hourly_imported_rows: int | None = 0
    hourly_blocked_reason: str | None = None
    coverage_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class PreparedHourlyImport:
    """Detached hourly source evidence prepared before durable Recorder effects."""

    series: tuple[HourlySeries, ...]
    identity: dict[str, Any]
    source_rows: int
    skipped_windows: SkippedWindowEvidence
    ordinary_start: datetime | None
    eligible_resources: dict[str, dict[str, Any]]


def _combined_import_result(
    legacy: ImportResult | None,
    hourly: ImportResult | None,
    *,
    has_hourly: bool,
    hourly_blocked_reason: str | None,
) -> ImportResult:
    """Report acknowledged counts separately from uncertain hourly effects."""

    parts = [result for result in (legacy, hourly) if result is not None]
    return ImportResult(
        imported_series=sum(result.imported_series for result in parts),
        imported_rows=sum(result.imported_rows for result in parts),
        source_rows=sum(result.source_rows for result in parts),
        skipped_windows=sum(result.skipped_windows for result in parts),
        skipped_runtime_thermostat_windows=sum(
            result.skipped_runtime_thermostat_windows for result in parts
        ),
        skipped_runtime_sensor_windows=sum(
            result.skipped_runtime_sensor_windows for result in parts
        ),
        skipped_window_examples=tuple(
            example for result in parts for example in result.skipped_window_examples
        ),
        latest_start_by_statistic_id={
            key: value
            for result in parts
            for key, value in result.latest_start_by_statistic_id.items()
        },
        summary_mode=(
            "mixed"
            if has_hourly and legacy is not None
            else "hourly"
            if has_hourly
            else legacy.summary_mode
            if legacy is not None
            else "full"
        ),
        summary_window_start=legacy.summary_window_start if legacy else None,
        summary_window_end=legacy.summary_window_end if legacy else None,
        summary_overlap_days=legacy.summary_overlap_days if legacy else None,
        summary_fallback_reason=(
            hourly_blocked_reason
            or (hourly.summary_fallback_reason if hourly else None)
            or (legacy.summary_fallback_reason if legacy else None)
        ),
        cumulative_seed_count=legacy.cumulative_seed_count if legacy else 0,
        legacy_imported_series=legacy.imported_series if legacy else 0,
        legacy_imported_rows=legacy.imported_rows if legacy else 0,
        hourly_imported_series=(
            hourly.imported_series if hourly else None if hourly_blocked_reason else 0
        ),
        hourly_imported_rows=(
            hourly.imported_rows if hourly else None if hourly_blocked_reason else 0
        ),
        hourly_blocked_reason=hourly_blocked_reason,
        coverage_incomplete=bool(hourly_blocked_reason)
        or any(result.coverage_incomplete for result in parts),
    )


def _latest_cumulative_starts(
    hass: HomeAssistant,
    statistic_ids: tuple[str, ...],
) -> dict[str, datetime]:
    """Read the latest Recorder row for each cumulative statistic."""

    latest: dict[str, datetime] = {}
    for statistic_id in statistic_ids:
        rows = get_last_statistics(
            hass,
            1,
            statistic_id,
            False,
            {"state", "sum"},
        ).get(statistic_id, [])
        if not rows:
            continue
        if (start := _row_start_datetime(rows[-1])) is not None:
            latest[statistic_id] = start
    return latest


def _cumulative_seeds_during_period(
    hass: HomeAssistant,
    statistic_ids: tuple[str, ...],
    seed_start: datetime,
    window_start: datetime,
) -> dict[str, CumulativeStatisticSeed]:
    """Read Recorder cumulative values immediately before a window."""

    rows_by_id = statistics_during_period(
        hass,
        seed_start,
        window_start,
        set(statistic_ids),
        "hour",
        None,
        {"state", "sum"},
    )
    seeds: dict[str, CumulativeStatisticSeed] = {}
    for statistic_id, rows in rows_by_id.items():
        if not rows:
            continue
        row = rows[-1]
        start = _row_start_datetime(row)
        state = _row_float(row.get("state"))
        sum_value = _row_float(row.get("sum"))
        if start is None or state is None or sum_value is None:
            continue
        seeds[statistic_id] = CumulativeStatisticSeed(
            start=start,
            state=state,
            sum=sum_value,
        )
    return seeds


def _validate_thermostat_id(
    runtime_data: BeestatRuntimeData,
    thermostat_id: int | None,
) -> None:
    if thermostat_id is None:
        return
    configured_ids = {
        thermostat.thermostat_id for thermostat in runtime_data.config.thermostats
    }
    if thermostat_id not in configured_ids:
        raise UnknownThermostatError(thermostat_id)


def _filter_summary_rows_by_thermostat(
    rows: list[dict[str, Any]],
    thermostat_id: int | None,
) -> list[dict[str, Any]]:
    if thermostat_id is None:
        return rows
    return [
        row
        for row in rows
        if row_resource_id(row, "thermostat_id", "id") == thermostat_id
    ]


def _filter_series_statistics(
    series: list[StatisticsSeries],
    *,
    start_day: dt_date | None,
    end_day: dt_date | None,
    local_tz: ZoneInfo,
) -> list[StatisticsSeries]:
    filtered: list[StatisticsSeries] = []
    for item in series:
        stats = [
            row
            for row in item.statistics
            if _statistic_row_in_range(
                row,
                start_day=start_day,
                end_day=None if item.metadata.get("has_sum") else end_day,
                local_tz=local_tz,
            )
        ]
        filtered.append(
            StatisticsSeries(
                metadata=item.metadata,
                statistics=stats,
                source_rows=item.source_rows,
            )
        )
    return filtered


def _statistic_row_in_range(
    row: dict[str, Any],
    *,
    start_day: dt_date | None,
    end_day: dt_date | None,
    local_tz: ZoneInfo,
) -> bool:
    start = row.get("start")
    if not isinstance(start, datetime):
        return False
    local_day = start.astimezone(local_tz).date()
    if start_day is not None and local_day < start_day:
        return False
    return end_day is None or local_day <= end_day


def _point_window(
    lookback_days: int,
    local_tz: ZoneInfo,
    start_day: dt_date | None,
    end_day: dt_date | None,
    *,
    evaluated_at: datetime,
) -> tuple[datetime, datetime]:
    end = evaluated_at
    if end_day is not None:
        end = _local_midnight(end_day + timedelta(days=1), local_tz).astimezone(UTC)
    if start_day is None:
        local_start_day = end.astimezone(local_tz).date() - timedelta(
            days=lookback_days,
        )
    else:
        local_start_day = start_day
    start = _local_midnight(local_start_day, local_tz).astimezone(UTC)
    return start, end


def _hourly_utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Hourly bounds require an explicit UTC offset")
    result = value.astimezone(UTC)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Hourly bounds must be whole UTC hours")
    return result


def _validate_hourly_window(start: datetime, end: datetime) -> None:
    if not timedelta(0) < end - start <= timedelta(days=MAX_POINT_LOOKBACK_DAYS):
        raise ValueError(
            "Hourly bounds must span more than zero and at most 366 elapsed days"
        )


def _hourly_window(
    context: TemporalContext,
    *,
    lookback_days: int,
    rebuild_start: dt_date | None,
    rebuild_end: dt_date | None,
    epoch_start: datetime | None,
    bootstrap_start: datetime | None = None,
) -> tuple[datetime, datetime, datetime | None]:
    end = context.evaluated_at.astimezone(UTC).replace(
        minute=0, second=0, microsecond=0
    )
    if epoch_start is not None:
        start = _hourly_utc_hour(epoch_start)
    elif rebuild_start is not None:
        start = _hourly_utc_hour(_local_midnight(rebuild_start, context.local_tz))
    else:
        start = end - timedelta(days=lookback_days)
        if bootstrap_start is not None:
            start = min(start, _hourly_utc_hour(bootstrap_start))
    _validate_hourly_window(start, end)
    measurement_end = None
    if rebuild_end is not None:
        measurement_end = min(
            end,
            _hourly_utc_hour(
                _local_midnight(rebuild_end + timedelta(days=1), context.local_tz)
            ),
        )
        _validate_hourly_window(start, measurement_end)
    return start, end, measurement_end


def _observed_hourly_horizons(
    rows_by_id: Mapping[int, list[dict[str, Any]]], caps: Mapping[int, datetime]
) -> dict[int, datetime]:
    result: dict[int, datetime] = {}
    for thermostat_id, rows in rows_by_id.items():
        stamps = [
            stamp
            for row in rows
            if row_resource_id(row, "thermostat_id") == thermostat_id
            and isinstance(row.get("timestamp"), str)
            and (stamp := _parse_beestat_time(row["timestamp"])) is not None
            and not (stamp.minute % 5 or stamp.second or stamp.microsecond)
            and (thermostat_id not in caps or stamp <= caps[thermostat_id])
        ]
        if stamps:
            result[thermostat_id] = max(stamps)
    return result


def _hourly_retained_ids(
    config: BeestatConfig, retained: tuple[str, ...]
) -> tuple[str, ...]:
    # Retain selected detailed quantities even after a display-derived slug changes.
    quantities = {
        key
        for key, _label, _field in DETAILED_RUNTIME_FIELDS
        if any(value.endswith(f"_{key}_runtime_hours_hourly_v2") for value in retained)
    }
    return (
        *retained,
        *(
            f"beestat:{thermostat.slug}_{key}_runtime_hours_hourly_v2"
            for thermostat in config.thermostats
            for key in sorted(quantities)
        ),
    )


def _hourly_resource_identities(config: BeestatConfig) -> dict[str, dict[str, Any]]:
    quantities = {
        *(f"{key}_runtime_hours" for key, _label, _fields in RUNTIME_FIELD_GROUPS),
        *(f"{key}_runtime_hours" for key, _label, _field in DETAILED_RUNTIME_FIELDS),
        *(spec.statistic_suffix for spec in SUMMARY_MEAN_STATISTICS),
        *(spec.statistic_suffix for spec in SUMMARY_SUM_STATISTICS),
        *(spec.statistic_suffix for spec in THERMOSTAT_POINT_STATISTICS),
    }
    candidates = [
        (
            f"beestat:{thermostat.slug}_{quantity}_hourly_v2",
            {
                "thermostat_id": thermostat.thermostat_id,
                "sensor_id": None,
                "quantity": quantity,
            },
        )
        for thermostat in config.thermostats
        for quantity in quantities
    ]
    sensors = {sensor.sensor_id: sensor for sensor in config.sensors}
    candidates.extend(
        (
            f"beestat:{spec.statistic_suffix}_hourly_v2",
            {
                "thermostat_id": sensors[spec.sensor_id].thermostat_id,
                "sensor_id": spec.sensor_id,
                "quantity": spec.field,
            },
        )
        for spec in build_sensor_specs(config)
    )
    if len({key for key, _value in candidates}) != len(candidates):
        raise ValueError("Hourly statistic identity is ambiguous")
    return dict(candidates)


def _hourly_identity(
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData,
    series: tuple[HourlySeries, ...],
    *,
    selected_thermostat_id: int | None = None,
    require_account: bool = True,
) -> dict[str, Any]:
    anchors = sorted(
        {
            hashlib.sha256(str(resource_id).encode()).hexdigest()
            for row in data.thermostat_rows
            if (resource_id := row_resource_id(row, "thermostat_id", "id")) is not None
        }
    )
    if not anchors and require_account:
        raise ValueError("Hourly account identity is unavailable")
    parsed = urlsplit(normalize_api_base(entry.data.get(CONF_API_BASE, API_BASE)))
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    origin = f"https://{host.lower()}" + (
        f":{parsed.port}" if parsed.port not in (None, 443) else ""
    )
    resources = _hourly_resource_identities(data.config)
    selected = {item.statistic_id: resources[item.statistic_id] for item in series}
    return {
        "entry_id": entry.entry_id,
        "api_base": origin,
        "account_anchors": anchors,
        "resources": selected,
        "selected_thermostat_id": selected_thermostat_id,
    }


def _writer_identity(
    entry: BeestatStatisticsConfigEntry, data: BeestatRuntimeData
) -> dict[str, Any]:
    """Map eligible quantities separately from stable cached sensor topology."""

    identity = _hourly_identity(entry, data, (), require_account=False)
    identity["resources"] = _hourly_resource_identities(data.config)
    parents: dict[int, int | None] = {}
    for row in data.sensor_rows:
        sensor_id = positive_resource_id(row.get("sensor_id", row.get("id")))
        if sensor_id is None:
            continue
        parent = positive_resource_id(row.get("thermostat_id"))
        if sensor_id in parents and parents[sensor_id] != parent:
            raise ValueError("Sensor parent identity is ambiguous")
        parents[sensor_id] = parent
    for sensor in data.config.sensors:
        # Keep provider topology independent of configured overrides. The
        # manager checks both against adopted resources; unadmitted legacy
        # quantities retain their existing configuration semantics.
        parents.setdefault(sensor.sensor_id, sensor.thermostat_id)
    identity["sensor_parents"] = parents
    return identity


def _local_midnight(local_day: dt_date, local_tz: ZoneInfo) -> datetime:
    return datetime.combine(local_day, time.min, local_tz)


def _latest_summary_day(rows: list[dict[str, Any]]) -> dt_date | None:
    days = [_row_date(row.get("date")) for row in rows]
    valid_days = [item for item in days if item is not None]
    return max(valid_days) if valid_days else None


def _row_date(value: Any) -> dt_date | None:
    if value in (None, ""):
        return None
    try:
        return dt_date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _row_start_datetime(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("start")
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        timestamp = _row_float(value)
        if timestamp is None:
            return None
        try:
            parsed = datetime.fromtimestamp(timestamp, UTC)
        except OverflowError, OSError, ValueError:
            return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except OverflowError, OSError, ValueError:
        return None


def _row_float(value: Any) -> float | None:
    if value is None or value in ("", "unknown", "unavailable"):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None


def _format_day(value: dt_date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _iter_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=MAX_WINDOW_DAYS), end)
        windows.append((current, window_end))
        if window_end >= end:
            break
        current = window_end
    return windows


def _format_beestat_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _sensor_thermostat_map(rows: list[dict[str, Any]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in rows:
        sensor_id = row_resource_id(row, "sensor_id", "id")
        thermostat_id = row_resource_id(row, "thermostat_id")
        if sensor_id is not None and thermostat_id is not None:
            mapping[sensor_id] = thermostat_id
    return mapping


def _thermostat_data_end_map(rows: list[dict[str, Any]]) -> dict[int, datetime]:
    mapping: dict[int, datetime] = {}
    for row in rows:
        thermostat_id = row_resource_id(row, "thermostat_id", "id")
        data_end = _parse_beestat_time(row.get("data_end"))
        if thermostat_id is not None and data_end is not None:
            mapping[thermostat_id] = data_end
    return mapping


def _parse_beestat_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError, ValueError:
        return None


def _dedupe_rows(rows: list[dict[str, Any]], *, id_field: str) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key: tuple[Any, ...]
        if (runtime_sensor_id := row_resource_id(row, "runtime_sensor_id")) is not None:
            key = ("runtime_sensor_id", runtime_sensor_id)
        elif (
            runtime_thermostat_id := row_resource_id(row, "runtime_thermostat_id")
        ) is not None:
            key = ("runtime_thermostat_id", runtime_thermostat_id)
        elif (resource_id := row_resource_id(row, id_field)) is not None and (
            timestamp := _parse_beestat_time(row.get("timestamp"))
        ) is not None:
            key = (id_field, resource_id, "timestamp", timestamp)
        else:
            key = (
                "row",
                tuple(sorted((str(key), str(value)) for key, value in row.items())),
            )
        deduped[key] = row
    return sorted(
        (row for row in deduped.values() if not row.get("deleted")),
        key=lambda row: (str(row.get("timestamp", "")), str(row.get(id_field, ""))),
    )


def _format_start(series: StatisticsSeries) -> str | None:
    latest = series.latest_start
    return latest.isoformat() if latest else None
