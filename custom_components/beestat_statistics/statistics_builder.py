"""Build Home Assistant external statistics from Beestat rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config_model import BeestatConfig, build_sensor_statistics as build_sensor_specs
from .const import (
    RUNTIME_FIELD_GROUPS,
    SUMMARY_MEAN_STATISTICS,
    SUMMARY_SUM_STATISTICS,
    STATISTIC_MEAN_TYPE_ARITHMETIC,
    STATISTIC_MEAN_TYPE_NONE,
    STATISTIC_SOURCE,
    STATISTIC_UNIT_CLASS_DURATION,
    THERMOSTAT_POINT_STATISTICS,
)


@dataclass(frozen=True, slots=True)
class StatisticsSeries:
    """One Home Assistant external statistic series."""

    metadata: dict[str, Any]
    statistics: list[dict[str, Any]]
    source_rows: int

    @property
    def statistic_id(self) -> str:
        """Return the external statistic ID."""

        return str(self.metadata["statistic_id"])

    @property
    def latest_start(self) -> datetime | None:
        """Return the latest imported start timestamp."""

        if not self.statistics:
            return None
        return self.statistics[-1]["start"]


@dataclass(frozen=True, slots=True)
class CumulativeStatisticSeed:
    """Existing Recorder cumulative values immediately before an import window."""

    start: datetime
    state: float
    sum: float


def cumulative_statistic_ids(config: BeestatConfig) -> tuple[str, ...]:
    """Return cumulative statistic IDs that require Recorder seeding."""

    statistic_ids: list[str] = []
    for thermostat in config.thermostats:
        for runtime_slug, _runtime_label, _fields in RUNTIME_FIELD_GROUPS:
            statistic_ids.append(
                f"{STATISTIC_SOURCE}:{thermostat.slug}_{runtime_slug}_runtime_hours"
            )
        for spec in SUMMARY_SUM_STATISTICS:
            statistic_ids.append(
                f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
            )
    return tuple(statistic_ids)


def apply_cumulative_seeds(
    series: list[StatisticsSeries],
    seeds: dict[str, CumulativeStatisticSeed],
) -> list[StatisticsSeries]:
    """Offset cumulative series built from a partial Beestat window."""

    adjusted: list[StatisticsSeries] = []
    for item in series:
        seed = seeds.get(item.statistic_id)
        if seed is None or not item.metadata.get("has_sum"):
            adjusted.append(item)
            continue

        stats: list[dict[str, Any]] = []
        for row in item.statistics:
            updated = dict(row)
            if (state := _as_float(updated.get("state"))) is not None:
                updated["state"] = round(seed.state + state, 6)
            if (sum_value := _as_float(updated.get("sum"))) is not None:
                updated["sum"] = round(seed.sum + sum_value, 6)
            stats.append(updated)

        adjusted.append(
            StatisticsSeries(
                metadata=item.metadata,
                statistics=stats,
                source_rows=item.source_rows,
            )
        )
    return adjusted


def build_statistics(
    summary_rows: list[dict[str, Any]],
    thermostat_rows_by_id: dict[int, list[dict[str, Any]]],
    sensor_rows_by_id: dict[int, list[dict[str, Any]]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build all Beestat statistics series for Home Assistant Recorder."""

    return [
        *build_runtime_statistics(summary_rows, local_tz, config),
        *build_summary_sum_statistics(summary_rows, local_tz, config),
        *build_summary_mean_statistics(summary_rows, local_tz, config),
        *build_thermostat_point_statistics(thermostat_rows_by_id, local_tz, config),
        *build_sensor_statistics(sensor_rows_by_id, local_tz, config),
    ]


def build_runtime_statistics(
    summary_rows: list[dict[str, Any]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build cumulative HVAC runtime statistics from daily summary rows."""

    rows_by_thermostat = _summary_rows_by_thermostat(summary_rows)
    series: list[StatisticsSeries] = []
    for thermostat in config.thermostats:
        rows = rows_by_thermostat.get(thermostat.thermostat_id, [])
        if not rows:
            continue
        for runtime_slug, runtime_label, fields in RUNTIME_FIELD_GROUPS:
            total_hours = 0.0
            stats: list[dict[str, Any]] = []
            for local_day, row in rows:
                total_hours += _seconds_for_fields(row, fields) / 3600
                stats.append(
                    {
                        "start": _local_midnight(local_day, local_tz),
                        "state": round(total_hours, 6),
                        "sum": round(total_hours, 6),
                        "last_reset": None,
                    }
                )
            series.append(
                StatisticsSeries(
                    metadata={
                        "has_sum": True,
                        "mean_type": STATISTIC_MEAN_TYPE_NONE,
                        "name": f"Beestat {thermostat.name} {runtime_label}",
                        "source": STATISTIC_SOURCE,
                        "statistic_id": (
                            f"{STATISTIC_SOURCE}:{thermostat.slug}_{runtime_slug}_runtime_hours"
                        ),
                        "unit_class": STATISTIC_UNIT_CLASS_DURATION,
                        "unit_of_measurement": "h",
                    },
                    statistics=stats,
                    source_rows=len(rows),
                )
            )
    return series


def build_summary_sum_statistics(
    summary_rows: list[dict[str, Any]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build cumulative sum statistics from Beestat thermostat summary rows."""

    rows_by_thermostat = _summary_rows_by_thermostat(summary_rows)
    series: list[StatisticsSeries] = []
    for thermostat in config.thermostats:
        rows = rows_by_thermostat.get(thermostat.thermostat_id, [])
        if not rows:
            continue
        for spec in SUMMARY_SUM_STATISTICS:
            total = 0.0
            stats: list[dict[str, Any]] = []
            for local_day, row in rows:
                value = _as_float(row.get(spec.field)) or 0.0
                total += value
                stats.append(
                    {
                        "start": _local_midnight(local_day, local_tz),
                        "state": round(total, 2),
                        "sum": round(total, 2),
                        "last_reset": None,
                    }
                )
            series.append(
                StatisticsSeries(
                    metadata={
                        "has_sum": True,
                        "mean_type": STATISTIC_MEAN_TYPE_NONE,
                        "name": f"Beestat {thermostat.name} {spec.name}",
                        "source": STATISTIC_SOURCE,
                        "statistic_id": (
                            f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
                        ),
                        "unit_class": spec.unit_class,
                        "unit_of_measurement": spec.unit,
                    },
                    statistics=stats,
                    source_rows=len(rows),
                )
            )
    return series


def build_summary_mean_statistics(
    summary_rows: list[dict[str, Any]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build daily mean statistics from Beestat thermostat summary rows."""

    rows_by_thermostat = _summary_rows_by_thermostat(summary_rows)
    series: list[StatisticsSeries] = []
    for thermostat in config.thermostats:
        rows = rows_by_thermostat.get(thermostat.thermostat_id, [])
        if not rows:
            continue
        for spec in SUMMARY_MEAN_STATISTICS:
            stats: list[dict[str, Any]] = []
            for local_day, row in rows:
                value = _as_float(row.get(spec.field))
                if value is None:
                    continue
                item = {
                    "start": _local_midnight(local_day, local_tz),
                    "mean": round(value, 2),
                }
                if spec.min_field is not None and (
                    min_value := _as_float(row.get(spec.min_field))
                ) is not None:
                    item["min"] = round(min_value, 2)
                if spec.max_field is not None and (
                    max_value := _as_float(row.get(spec.max_field))
                ) is not None:
                    item["max"] = round(max_value, 2)
                stats.append(item)
            if not stats:
                continue
            series.append(
                StatisticsSeries(
                    metadata={
                        "has_sum": False,
                        "mean_type": STATISTIC_MEAN_TYPE_ARITHMETIC,
                        "name": f"Beestat {thermostat.name} {spec.name}",
                        "source": STATISTIC_SOURCE,
                        "statistic_id": (
                            f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
                        ),
                        "unit_class": spec.unit_class,
                        "unit_of_measurement": spec.unit,
                    },
                    statistics=stats,
                    source_rows=len(stats),
                )
            )
    return series


def build_thermostat_point_statistics(
    thermostat_rows_by_id: dict[int, list[dict[str, Any]]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build daily mean/min/max statistics from runtime_thermostat rows."""

    series: list[StatisticsSeries] = []
    for thermostat in config.thermostats:
        rows = thermostat_rows_by_id.get(thermostat.thermostat_id, [])
        for spec in THERMOSTAT_POINT_STATISTICS:
            grouped: dict[date, list[float]] = {}
            source_count = 0
            for row in rows:
                value = _as_float(row.get(spec.field))
                local_day = _parse_timestamp_day(row.get("timestamp"), local_tz)
                if value is None or local_day is None:
                    continue
                source_count += 1
                grouped.setdefault(local_day, []).append(value)

            stats: list[dict[str, Any]] = []
            for local_day in sorted(grouped):
                values = grouped[local_day]
                stats.append(
                    {
                        "start": _local_midnight(local_day, local_tz),
                        "mean": round(sum(values) / len(values), 2),
                        "min": round(min(values), 2),
                        "max": round(max(values), 2),
                    }
                )
            if not stats:
                continue
            series.append(
                StatisticsSeries(
                    metadata={
                        "has_sum": False,
                        "mean_type": STATISTIC_MEAN_TYPE_ARITHMETIC,
                        "name": f"Beestat {thermostat.name} {spec.name}",
                        "source": STATISTIC_SOURCE,
                        "statistic_id": (
                            f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
                        ),
                        "unit_class": spec.unit_class,
                        "unit_of_measurement": spec.unit,
                    },
                    statistics=stats,
                    source_rows=source_count,
                )
            )
    return series


def build_sensor_statistics(
    sensor_rows_by_id: dict[int, list[dict[str, Any]]],
    local_tz: ZoneInfo,
    config: BeestatConfig,
) -> list[StatisticsSeries]:
    """Build daily mean/min/max statistics from 5-minute runtime_sensor rows."""

    series: list[StatisticsSeries] = []
    for spec in build_sensor_specs(config):
        grouped: dict[date, list[float]] = {}
        source_count = 0
        for row in sensor_rows_by_id.get(spec.sensor_id, []):
            value = _as_float(row.get(spec.field))
            local_day = _parse_timestamp_day(row.get("timestamp"), local_tz)
            if value is None or local_day is None:
                continue
            source_count += 1
            grouped.setdefault(local_day, []).append(value)

        stats: list[dict[str, Any]] = []
        for local_day in sorted(grouped):
            values = grouped[local_day]
            stats.append(
                {
                    "start": _local_midnight(local_day, local_tz),
                    "mean": round(sum(values) / len(values), 2),
                    "min": round(min(values), 2),
                    "max": round(max(values), 2),
                }
            )
        if not stats:
            continue
        series.append(
            StatisticsSeries(
                metadata={
                    "has_sum": False,
                    "mean_type": STATISTIC_MEAN_TYPE_ARITHMETIC,
                    "name": spec.name,
                    "source": STATISTIC_SOURCE,
                    "statistic_id": f"{STATISTIC_SOURCE}:{spec.statistic_suffix}",
                    "unit_class": spec.unit_class,
                    "unit_of_measurement": spec.unit,
                },
                statistics=stats,
                source_rows=source_count,
            )
        )
    return series


def _parse_summary_day(row: dict[str, Any]) -> date:
    value = row.get("date")
    if value is None:
        raise ValueError("runtime_thermostat_summary row is missing date")
    return date.fromisoformat(str(value)[:10])


def _summary_rows_by_thermostat(
    summary_rows: list[dict[str, Any]],
) -> dict[int, list[tuple[date, dict[str, Any]]]]:
    rows_by_thermostat: dict[int, list[tuple[date, dict[str, Any]]]] = {}
    for row in summary_rows:
        thermostat_id = _as_int(row.get("thermostat_id"))
        local_day = _parse_summary_day_or_none(row)
        if thermostat_id is None or local_day is None:
            continue
        rows_by_thermostat.setdefault(thermostat_id, []).append((local_day, row))
    for rows in rows_by_thermostat.values():
        rows.sort(key=lambda item: item[0].isoformat())
    return rows_by_thermostat


def _parse_summary_day_or_none(row: dict[str, Any]) -> date | None:
    try:
        return _parse_summary_day(row)
    except ValueError:
        return None


def _parse_timestamp(value: str, local_tz: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_tz)


def _parse_timestamp_day(value: Any, local_tz: ZoneInfo) -> date | None:
    if value is None:
        return None
    try:
        return _parse_timestamp(str(value), local_tz).date()
    except ValueError:
        return None


def _local_midnight(local_day: date, local_tz: ZoneInfo) -> datetime:
    return datetime.combine(local_day, time.min, local_tz)


def _seconds_for_fields(row: dict[str, Any], fields: tuple[str, ...]) -> float:
    return sum(_as_float(row.get(field)) or 0.0 for field in fields)


def _as_float(value: Any) -> float | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
