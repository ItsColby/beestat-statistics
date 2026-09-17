"""Pure, coverage-qualified hourly observations for a proposed Recorder import.

This module does not submit statistics or turn hourly increments into sums.
Source list order decides corrections: the last resource/instant row wins,
including deleted rows and rows whose quantity is invalid.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import fsum, isfinite
from typing import Any

from .config_model import BeestatConfig, build_sensor_statistics
from .config_rows import positive_resource_id
from .const import (
    DETAILED_RUNTIME_FIELDS,
    RUNTIME_FIELD_GROUPS,
    STATISTIC_MEAN_TYPE_ARITHMETIC,
    STATISTIC_MEAN_TYPE_NONE,
    STATISTIC_SOURCE,
    STATISTIC_UNIT_CLASS_DURATION,
    STATISTIC_UNIT_CLASS_TEMPERATURE,
    SUMMARY_MEAN_STATISTICS,
    SUMMARY_SUM_STATISTICS,
    THERMOSTAT_POINT_STATISTICS,
    SummaryMeanStatistic,
    ThermostatPointStatistic,
)
from .temperature import absolute_temperature_value

_HOUR = timedelta(hours=1)
_STEP = timedelta(minutes=5)
_SUCCESSOR = "_hourly_v2"
_ACCESSORIES = frozenset(
    {"off", "humidifier", "dehumidifier", "ventilator", "economizer"}
)


@dataclass(frozen=True, slots=True)
class HourlyBucket:
    """One UTC hour and its quantity-specific source coverage.

    Duplicate slots count distinct instants with more than one source row.
    Missing and invalid slots partition the twelve expected slots with valid
    slots; duplicates are independent and the final row remains authoritative.
    """

    start: datetime
    values: dict[str, float] | None
    valid_slots: int
    missing_slots: int
    invalid_slots: int
    duplicate_slots: int
    reason: str


@dataclass(frozen=True, slots=True)
class HourlySeries:
    """One successor series, with evidence but no Recorder side effects."""

    metadata: dict[str, Any]
    hours: tuple[HourlyBucket, ...]
    source_rows: int
    rejected_timestamps: int = 0
    blocked_reason: str | None = None

    @property
    def statistic_id(self) -> str:
        """Return the proposed successor ID."""
        return str(self.metadata["statistic_id"])


@dataclass(frozen=True, slots=True)
class _Quantity:
    suffix: str
    name: str
    unit: str
    unit_class: str | None
    fields: tuple[str, ...]
    kind: str = "measurement"
    optional_runtime: bool = False

    @property
    def statistic_id(self) -> str:
        return f"{STATISTIC_SOURCE}:{self.suffix}{_SUCCESSOR}"

    @property
    def cumulative(self) -> bool:
        return self.kind in {"runtime", "heating_degree_days", "cooling_degree_days"}


@dataclass(frozen=True, slots=True)
class _Points:
    rows: dict[datetime, dict[str, Any]]
    duplicates: frozenset[datetime]
    source_rows: int
    rejected_timestamps: int
    identity_invalid: bool


def build_hourly_statistics(
    thermostat_rows_by_id: Mapping[int, list[dict[str, Any]]],
    sensor_rows_by_id: Mapping[int, list[dict[str, Any]]],
    config: BeestatConfig,
    *,
    start: datetime,
    end: datetime,
    evaluated_at: datetime,
    source_end_by_thermostat: Mapping[int, datetime],
    existing_statistic_ids: Collection[str] = (),
    start_by_statistic_id: Mapping[str, datetime] | None = None,
    measurement_end: datetime | None = None,
) -> tuple[HourlySeries, ...]:
    """Plan complete closed UTC hours, bounded to at most 366 elapsed days.

    Bounds are inclusive start/exclusive end and must land on UTC hours.
    The source horizon is the last observed interval *start*, not acquisition
    time. A missing horizon blocks eligibility. VOC emits metadata only while
    its source unit is unresolved. Misrouted resource identities block the
    affected series instead of being silently assigned to the mapping key.
    Per-series bounds restrict quality evidence as well as hourly values;
    measurement_end does not shorten cumulative source windows.
    """
    start, end, evaluated_at = _bounds(start, end, evaluated_at)
    starts = {
        statistic_id: _window_bound(value, start, end)
        for statistic_id, value in (start_by_statistic_id or {}).items()
    }
    measurement_stop = (
        end if measurement_end is None else _window_bound(measurement_end, start, end)
    )
    point_cache: dict[tuple[str, int, datetime, datetime], _Points] = {}

    def quantity_points(
        quantity: _Quantity,
        rows: list[dict[str, Any]],
        id_field: str,
        resource_id: int,
    ) -> tuple[_Points, datetime, datetime]:
        quantity_end = end if quantity.cumulative else measurement_stop
        quantity_start = min(starts.get(quantity.statistic_id, start), quantity_end)
        key = (id_field, resource_id, quantity_start, quantity_end)
        if key not in point_cache:
            point_cache[key] = _points(
                rows, id_field, resource_id, quantity_start, quantity_end
            )
        return point_cache[key], quantity_start, quantity_end

    series: list[HourlySeries] = []
    for thermostat in config.thermostats:
        horizon = source_end_by_thermostat.get(thermostat.thermostat_id)
        for quantity in _thermostat_quantities(thermostat.slug, thermostat.name):
            points, quantity_start, quantity_end = quantity_points(
                quantity,
                thermostat_rows_by_id.get(thermostat.thermostat_id, []),
                "thermostat_id",
                thermostat.thermostat_id,
            )
            if _retain_quantity(quantity, points, existing_statistic_ids):
                series.append(
                    _series(
                        quantity,
                        points,
                        quantity_start,
                        quantity_end,
                        evaluated_at,
                        horizon,
                    )
                )
    sensor_thermostats = {
        sensor.sensor_id: sensor.thermostat_id for sensor in config.sensors
    }
    for spec in build_sensor_statistics(config):
        thermostat_id = sensor_thermostats[spec.sensor_id]
        horizon = (
            source_end_by_thermostat.get(thermostat_id)
            if thermostat_id is not None
            else None
        )
        quantity = _Quantity(
            spec.statistic_suffix,
            spec.name,
            spec.unit,
            spec.unit_class,
            (spec.field,),
            "occupancy" if spec.field == "occupancy" else "measurement",
        )
        points, quantity_start, quantity_end = quantity_points(
            quantity,
            sensor_rows_by_id.get(spec.sensor_id, []),
            "sensor_id",
            spec.sensor_id,
        )
        series.append(
            _series(
                quantity,
                points,
                quantity_start,
                quantity_end,
                evaluated_at,
                horizon,
                blocked_reason=(
                    "voc_unit_unresolved" if spec.field == "voc_concentration" else None
                ),
            )
        )
    return tuple(series)


def _window_bound(value: datetime, start: datetime, end: datetime) -> datetime:
    value = _aware_utc(value)
    if value.minute or value.second or value.microsecond or not start <= value <= end:
        raise ValueError("Series bounds must be exact UTC hours within acquired bounds")
    return value


def _bounds(
    start: datetime, end: datetime, evaluated_at: datetime
) -> tuple[datetime, datetime, datetime]:
    start, end, evaluated_at = (
        _aware_utc(value) for value in (start, end, evaluated_at)
    )
    if any(value.minute or value.second or value.microsecond for value in (start, end)):
        raise ValueError("Hourly bounds must be exact UTC hours")
    if end <= start or end - start > timedelta(days=366):
        raise ValueError("Hourly bounds must cover more than zero and at most 366 days")
    return start, end, evaluated_at


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("An aware datetime is required")
    return value.astimezone(UTC)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        ).astimezone(UTC)
    except ValueError, OverflowError:
        return None


def _deleted(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _points(
    rows: list[dict[str, Any]],
    id_field: str,
    resource_id: int,
    start: datetime,
    end: datetime,
) -> _Points:
    accepted: dict[datetime, dict[str, Any]] = {}
    duplicates: set[datetime] = set()
    rejected = 0
    identity_invalid = False
    for row in rows:
        stamp = _timestamp(row.get("timestamp"))
        if stamp is None:
            rejected += 1
            continue
        if not start <= stamp < end:
            continue
        if stamp.minute % 5 or stamp.second or stamp.microsecond:
            rejected += 1
            continue
        if id_field in row and positive_resource_id(row[id_field]) != resource_id:
            identity_invalid = True
            continue
        if stamp in accepted:
            duplicates.add(stamp)
        accepted[stamp] = row
    return _Points(
        accepted, frozenset(duplicates), len(rows), rejected, identity_invalid
    )


def _thermostat_quantities(slug: str, name: str) -> tuple[_Quantity, ...]:
    quantities: list[_Quantity] = []
    runtime_fields = (
        *RUNTIME_FIELD_GROUPS,
        *((key, label, (field,)) for key, label, field in DETAILED_RUNTIME_FIELDS),
    )
    for key, label, fields in runtime_fields:
        suffix = f"{slug}_{key}_runtime_hours"
        quantities.append(
            _Quantity(
                suffix,
                f"Beestat {name} {label}",
                "h",
                STATISTIC_UNIT_CLASS_DURATION,
                fields,
                "runtime",
                optional_runtime=key not in {"cool", "heat", "fan"},
            )
        )
    quantities.extend(
        _Quantity(
            f"{slug}_{spec.statistic_suffix}",
            f"Beestat {name} {spec.name}",
            spec.unit,
            spec.unit_class,
            ("outdoor_temperature",),
            spec.statistic_suffix,
        )
        for spec in SUMMARY_SUM_STATISTICS
    )
    measurement_specs: tuple[SummaryMeanStatistic | ThermostatPointStatistic, ...] = (
        *SUMMARY_MEAN_STATISTICS,
        *THERMOSTAT_POINT_STATISTICS,
    )
    quantities.extend(
        _Quantity(
            f"{slug}_{spec.statistic_suffix}",
            f"Beestat {name} {spec.name}",
            spec.unit,
            spec.unit_class,
            (spec.field.removeprefix("avg_"),),
        )
        for spec in measurement_specs
    )
    return tuple(quantities)


def _retain_quantity(
    quantity: _Quantity, points: _Points, existing: Collection[str]
) -> bool:
    return (
        not quantity.optional_runtime
        or quantity.statistic_id in existing
        or quantity.statistic_id.removesuffix(_SUCCESSOR) in existing
        or any(
            not _deleted(row.get("deleted"))
            and (_runtime_seconds(row, quantity.fields[0]) or 0) > 0
            for row in points.rows.values()
        )
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except ValueError, TypeError, OverflowError:
        return None
    return result if isfinite(result) else None


def _seconds(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and 0 <= result <= 300 else None


def _runtime_seconds(row: dict[str, Any], field: str) -> float | None:
    field = field.removeprefix("sum_")
    if field.startswith("compressor_"):
        mode = row.get("compressor_mode")
        if not isinstance(mode, str) or mode not in {"off", "cool", "heat"}:
            return None
        _, target_mode, stage = field.split("_")
        value = _exclusive_stage(row, "compressor", stage)
        if value is None or (mode == "off" and value != 0):
            return None
        return value if mode == target_mode else 0.0
    if field.startswith("auxiliary_heat_"):
        return _exclusive_stage(row, "auxiliary_heat", field[-1])
    if field in _ACCESSORIES:
        accessory = row.get("accessory_type")
        value = _seconds(row.get("accessory"))
        if (
            not isinstance(accessory, str)
            or accessory not in _ACCESSORIES
            or value is None
            or (accessory == "off" and value != 0)
        ):
            return None
        return value if accessory == field else 0.0
    return _seconds(row.get(field))


def _exclusive_stage(row: dict[str, Any], prefix: str, stage: str) -> float | None:
    first, second = (_seconds(row.get(f"{prefix}_{index}")) for index in (1, 2))
    if first is None or second is None or first + second > 300:
        return None
    return first if stage == "1" else second


def _value(row: dict[str, Any], quantity: _Quantity) -> float | None:
    if _deleted(row.get("deleted")):
        return None
    if quantity.kind == "runtime":
        values = [_runtime_seconds(row, field) for field in quantity.fields]
        return (
            fsum(value / 3600 for value in values if value is not None)
            if all(value is not None for value in values)
            else None
        )
    raw = row.get(quantity.fields[0])
    if quantity.kind == "occupancy":
        if raw is True or raw == 1:
            return 100.0
        return 0.0 if raw is False or raw == 0 else None
    value = _number(raw)
    if quantity.kind in {"heating_degree_days", "cooling_degree_days"}:
        value = absolute_temperature_value(value, "°F", tenth_fahrenheit_source=True)
        if value is None:
            return None
        difference = (
            65 - value if quantity.kind == "heating_degree_days" else value - 65
        )
        return max(difference, 0) / 288
    if quantity.unit_class == STATISTIC_UNIT_CLASS_TEMPERATURE:
        return absolute_temperature_value(
            value, quantity.unit, tenth_fahrenheit_source=True
        )
    return _measurement_range(value, quantity.fields[0])


def _measurement_range(value: float | None, field: str) -> float | None:
    """Reject proven quantity-invalid observations without clipping them."""
    if value is None:
        return None
    if field in {"indoor_humidity", "outdoor_humidity", "air_quality"}:
        return value if 0 <= value <= 100 else None
    if field == "co2_concentration" and value < 0:
        return None
    return value


def _series(
    quantity: _Quantity,
    points: _Points,
    start: datetime,
    end: datetime,
    evaluated_at: datetime,
    horizon: datetime | None,
    *,
    blocked_reason: str | None = None,
) -> HourlySeries:
    metadata = {
        "has_sum": quantity.cumulative,
        "mean_type": (
            STATISTIC_MEAN_TYPE_NONE
            if quantity.cumulative
            else STATISTIC_MEAN_TYPE_ARITHMETIC
        ),
        "name": f"{quantity.name} Hourly",
        "source": STATISTIC_SOURCE,
        "statistic_id": quantity.statistic_id,
        "unit_class": quantity.unit_class,
        "unit_of_measurement": quantity.unit,
    }
    if points.identity_invalid:
        blocked_reason = blocked_reason or "resource_identity_mismatch"
    if blocked_reason is not None:
        return HourlySeries(
            metadata, (), points.source_rows, points.rejected_timestamps, blocked_reason
        )
    if horizon is not None:
        horizon = _aware_utc(horizon)
    hours: list[HourlyBucket] = []
    cursor = start
    while cursor < end:
        hours.append(_bucket(quantity, points, cursor, evaluated_at, horizon))
        cursor += _HOUR
    return HourlySeries(
        metadata,
        tuple(hours),
        points.source_rows,
        points.rejected_timestamps,
        "source_horizon_unavailable" if horizon is None else None,
    )


def _bucket(
    quantity: _Quantity,
    points: _Points,
    start: datetime,
    evaluated_at: datetime,
    horizon: datetime | None,
) -> HourlyBucket:
    stamps = tuple(start + index * _STEP for index in range(12))
    present = [points.rows[stamp] for stamp in stamps if stamp in points.rows]
    values = [value for row in present if (value := _value(row, quantity)) is not None]
    invalid, missing = len(present) - len(values), 12 - len(present)
    result = None
    if horizon is None or stamps[-1] > horizon or start + _HOUR > evaluated_at:
        reason = "provisional"
    elif invalid:
        reason = "invalid_slots"
    elif missing:
        reason = "missing_slots"
    else:
        reason = "ready"
        result = _aggregate(values, quantity.cumulative)
        if result is None:
            reason = "invalid_slots"
    return HourlyBucket(
        start,
        result,
        len(values),
        missing,
        invalid,
        sum(stamp in points.duplicates for stamp in stamps),
        reason,
    )


def _aggregate(values: list[float], cumulative: bool) -> dict[str, float] | None:
    try:
        if cumulative:
            result = {"increment": fsum(values)}
        else:
            # Scale before summing so finite large measurements do not overflow.
            scale = max(abs(value) for value in values)
            mean = (
                scale * (fsum(value / scale for value in values) / len(values))
                if scale
                else 0.0
            )
            result = {"mean": mean, "min": min(values), "max": max(values)}
    except OverflowError:
        return None
    return result if all(isfinite(value) for value in result.values()) else None
