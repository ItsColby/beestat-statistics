"""Pure, coverage-qualified projection of detached v3 history evidence.

The manager owns acquisition, native fencing, journal reconciliation and revision
checks. This module never reads or writes those owners. A native row alone is
not proof that a complete, admitted source hour was committed successfully.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .hourly_history_contract import (
    CONTRACT_VERSION,
    DAILY_POLICY,
    MAX_BUCKETS,
    MAX_QUANTITIES,
    METHOD_VERSION,
    bounds,
    digest,
    utc,
)

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_METADATA_FIELDS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "unit_class",
    "mean_type",
    "has_sum",
)
_REASONS = {
    "missing_slots": "missing",
    "invalid_slots": "invalid",
    "source_conflict": "conflict",
}
_PROOF_REASONS = frozenset(
    {
        "ready",
        "missing",
        "invalid",
        "provisional",
        "pending",
        "conflict",
        "unassessed",
        "unverified_native",
        "blocked",
    }
)
_KNOWN_FAILURES = frozenset(
    {
        "voc_unit_unresolved",
        "resource_identity_mismatch",
        "source_timestamp_unplaced",
        "source_horizon_unsettled",
        "source_conflict",
        "invalid_slots",
        "missing_slots",
        "source_evidence_invalid",
    }
)


@dataclass(frozen=True)
class _Query:
    quantities: tuple[str, ...]
    start: datetime
    end: datetime
    period: str
    timezone: str
    timezone_revision: Any
    evaluated_at: datetime
    page_size: int
    offset: int


def history_response(
    request: dict[str, Any], material: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Project an immutable manager snapshot without refreshing or reconciling it.

    ``material`` contains descriptors, expected_metadata/native_metadata by
    quantity, hours/native_rows/legacy_days lists by quantity, pending_affected
    half-open windows, and root/source/coverage revisions. Proofs have an explicit
    committed flag, status, slot counts, native expected fields and provenance.
    Follow-up pages repeat the returned evaluated_at and view_token; neither
    the live clock nor a different page size changes the captured evaluation.
    """

    query = _query(request, context)
    descriptors = _descriptors(material, query.quantities)
    view_token = _view_token(query, material, context)
    if request.get("view_token") is not None and request["view_token"] != view_token:
        raise ValueError("history_view_changed")
    per_quantity = (
        (query.end - query.start) // _HOUR
        if query.period == "hour"
        else len(_day_windows(query))
    )
    total = per_quantity * len(descriptors)
    if query.offset >= total:
        raise ValueError("history_offset_out_of_range")
    stop = min(total, query.offset + query.page_size)
    page = _page(query, descriptors, material, per_quantity, stop)
    return {
        "contract_version": CONTRACT_VERSION,
        "identity": {
            key: context.get("identity", {}).get(key)
            for key in ("entry_id", "api_base", "account_anchors")
        },
        "config_revision": context["config_revision"],
        "root_revision": material["root_revision"],
        "source_revision": material["source_revision"],
        "coverage_revision": material["coverage_revision"],
        "method_version": METHOD_VERSION,
        "daily_policy": DAILY_POLICY,
        "period": query.period,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
        "evaluated_at": query.evaluated_at.isoformat(),
        "timezone": query.timezone,
        "timezone_revision": query.timezone_revision,
        "native_verification": material.get("native_verification", "bounded_readback"),
        "operation": material.get("operation"),
        "view_token": view_token,
        "pagination": {
            "offset": query.offset,
            "next_offset": stop if stop < total else None,
            "has_more": stop < total,
            "total_buckets": total,
        },
        "series": page,
    }


def _query(request: Mapping[str, Any], context: Mapping[str, Any]) -> _Query:
    if (
        type(request.get("contract_version")) is not int
        or request["contract_version"] != CONTRACT_VERSION
    ):
        raise ValueError("history_contract_invalid")
    quantities = request.get("quantity_ids")
    if (
        not isinstance(quantities, list)
        or not 1 <= len(quantities) <= MAX_QUANTITIES
        or any(not isinstance(item, str) or not item for item in quantities)
        or len(set(quantities)) != len(quantities)
    ):
        raise ValueError("history_quantities_invalid")
    start, end = bounds(request["start"], request["end"])
    period = request.get("period", "hour")
    if (
        period not in ("hour", "day")
        or request.get("daily_policy", DAILY_POLICY) != DAILY_POLICY
    ):
        raise ValueError("history_policy_invalid")
    page_size = _integer(request.get("page_size", MAX_BUCKETS), 1, MAX_BUCKETS)
    offset = _integer(request.get("offset", 0), 0, 40 * 366 * 24)
    evaluated_at = _evaluation(request, context, offset)
    timezone = context["timezone"]
    ZoneInfo(timezone)  # Validate the calendar owner even for an hourly response.
    return _Query(
        tuple(quantities),
        start,
        end,
        period,
        timezone,
        context["timezone_revision"],
        evaluated_at,
        page_size,
        offset,
    )


def _evaluation(
    request: Mapping[str, Any], context: Mapping[str, Any], offset: int
) -> datetime:
    if (offset or request.get("view_token") is not None) and (
        not isinstance(request.get("view_token"), str) or "evaluated_at" not in request
    ):
        raise ValueError("history_page_requires_view_and_evaluation")
    now = utc(context["evaluated_at"])
    evaluation = utc(request.get("evaluated_at", now))
    if evaluation > now:
        raise ValueError("history_evaluation_in_future")
    return evaluation


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("history_page_invalid")
    return value


def _descriptors(
    material: Mapping[str, Any], quantities: tuple[str, ...]
) -> list[dict[str, Any]]:
    available: dict[str, dict[str, Any]] = {}
    for descriptor in material.get("descriptors", []):
        identifier = descriptor["quantity_id"]
        if identifier in available:
            raise ValueError("history_duplicate_quantity")
        if descriptor.get("kind") not in ("runtime", "degree_days", "measurement"):
            raise ValueError("history_quantity_kind_invalid")
        if descriptor.get("method_version") != METHOD_VERSION:
            raise ValueError("history_quantity_method_invalid")
        available[identifier] = descriptor
    if any(identifier not in available for identifier in quantities):
        raise ValueError("history_quantity_unknown")
    return [available[identifier] for identifier in quantities]


def _view_token(
    query: _Query, material: Mapping[str, Any], context: Mapping[str, Any]
) -> str:
    return digest(
        {
            "contract_version": CONTRACT_VERSION,
            "method_version": METHOD_VERSION,
            "daily_policy": DAILY_POLICY,
            "quantity_ids": query.quantities,
            "start": query.start.isoformat(),
            "end": query.end.isoformat(),
            "period": query.period,
            "evaluated_at": query.evaluated_at.isoformat(),
            "timezone": query.timezone,
            "timezone_revision": query.timezone_revision,
            "config_revision": context["config_revision"],
            "identity": context.get("identity", {}),
            "material": material,
        }
    )


def _page(
    query: _Query,
    descriptors: list[dict[str, Any]],
    material: Mapping[str, Any],
    per_quantity: int,
    stop: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cursor = 0
    for descriptor in descriptors:
        next_cursor = cursor + per_quantity
        if query.offset < next_cursor and stop > cursor:
            item = _series(query, descriptor, material)
            result.append(
                {
                    **item,
                    "buckets": item["buckets"][
                        max(0, query.offset - cursor) : stop - cursor
                    ],
                }
            )
        cursor = next_cursor
    return result


def _series(
    query: _Query, descriptor: dict[str, Any], material: Mapping[str, Any]
) -> dict[str, Any]:
    identifier = descriptor["quantity_id"]
    proofs = _by_start(material.get("hours", {}).get(identifier, []))
    native = _by_start(material.get("native_rows", {}).get(identifier, []))
    pending = _pending_windows(material.get("pending_affected", {}).get(identifier, []))
    holds = _source_holds(material.get("source_holds", {}).get(identifier, []))
    metadata_ok = _metadata_agrees(
        descriptor,
        material.get("expected_metadata", {}).get(identifier),
        material.get("native_metadata", {}).get(identifier),
    )
    hours = [
        _hour(
            query,
            descriptor,
            stamp,
            proofs.get(stamp),
            native.get(stamp),
            metadata_ok,
            pending,
            holds,
        )
        for stamp in _hour_starts(query.start, query.end)
    ]
    buckets = hours
    if query.period == "day":
        legacy = _by_start(material.get("legacy_days", {}).get(identifier, []))
        buckets = [
            _day(query, descriptor, first, end, hours, legacy.get(first))
            for first, end in _day_windows(query)
        ]
    return {
        "descriptor": dict(descriptor),
        "buckets": buckets,
        "summary": _summary(query, descriptor, hours, buckets),
    }


def _by_start(rows: list[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
    result: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        start = utc(row["start"], whole_hour=True)
        if start in result:
            raise ValueError("history_duplicate_hour")
        result[start] = row
    return result


def _pending_windows(windows: list[list[str]]) -> tuple[tuple[datetime, datetime], ...]:
    return tuple(bounds(window[0], window[1]) for window in windows)


def _source_holds(
    holds: list[dict[str, Any]],
) -> list[tuple[datetime, datetime, list[str]]]:
    result = []
    for hold in holds:
        start, end = utc(hold["start"]), utc(hold["end"])
        if start >= end:
            raise ValueError("history_source_hold_invalid")
        result.append((start, end, _strings(hold.get("source_ids", []))))
    return result


def _hour_starts(start: datetime, end: datetime) -> list[datetime]:
    return [start + index * _HOUR for index in range((end - start) // _HOUR)]


def _metadata_agrees(descriptor: Mapping[str, Any], expected: Any, native: Any) -> bool:
    if not isinstance(expected, Mapping) or not isinstance(native, Mapping):
        return False
    if expected.get("statistic_id") != descriptor["statistic_id"]:
        return False
    return all(
        field in expected
        and field in native
        and _same_scalar(expected[field], native[field])
        for field in _METADATA_FIELDS
    )


def _same_scalar(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and bool(expected == actual)
    return bool(expected == actual)


def _hour(
    query: _Query,
    descriptor: Mapping[str, Any],
    start: datetime,
    proof: dict[str, Any] | None,
    native: dict[str, Any] | None,
    metadata_ok: bool,
    pending: tuple[tuple[datetime, datetime], ...],
    holds: list[tuple[datetime, datetime, list[str]]],
) -> dict[str, Any]:
    end = start + _HOUR
    reason = _hour_reason(query, descriptor, start, proof, native, metadata_ok, pending)
    active_holds = [
        source_ids for first, stop, source_ids in holds if first < end and stop > start
    ]
    if active_holds and reason != "pending":
        reason = "conflict"
    bucket = _empty_bucket(start, end, query.evaluated_at, reason)
    bucket["failure_reason"] = _failure_reason(descriptor, proof, reason)
    if proof is not None:
        bucket.update(
            {
                "valid_slots": _slot_count(proof.get("valid_slots", 0)),
                "confidence": _strings(proof.get("confidence", [])),
                "source_ids": _strings(proof.get("source_ids", [])),
            }
        )
    if active_holds:
        bucket["failure_reason"] = "source_conflict"
        bucket["source_ids"] = sorted(set(bucket["source_ids"]).union(*active_holds))
    if reason != "ready" or native is None:
        return bucket
    value = _logical_value(descriptor["kind"], native["mean"])
    amount = value if descriptor["kind"] != "measurement" else None
    bucket.update(
        {
            "value": value,
            "min": native.get("min") if descriptor["kind"] == "measurement" else None,
            "max": native.get("max") if descriptor["kind"] == "measurement" else None,
            "verified_hours": 1,
            "observed_amount": amount,
            "complete_total": amount,
            "source_basis": "points",
            "method_basis": METHOD_VERSION,
            "eligible_intervals": [[start.isoformat(), end.isoformat()]],
        }
    )
    return bucket


def _hour_reason(
    query: _Query,
    descriptor: Mapping[str, Any],
    start: datetime,
    proof: dict[str, Any] | None,
    native: dict[str, Any] | None,
    metadata_ok: bool,
    pending: tuple[tuple[datetime, datetime], ...],
) -> str:
    if any(first < start + _HOUR and end > start for first, end in pending):
        return "pending"
    if (
        descriptor.get("admission") == "blocked"
        or descriptor.get("quantity") == "voc_concentration"
    ):
        return "blocked"
    if start + _HOUR > query.evaluated_at:
        return "provisional"
    if proof is None:
        return "unverified_native" if native is not None else "unassessed"
    status = proof.get("status", "unassessed")
    if not isinstance(status, str) or not status:
        raise ValueError("history_proof_status_invalid")
    if status != "ready":
        normalized = _REASONS.get(status, status)
        return normalized if normalized in _PROOF_REASONS else "blocked"
    if proof.get("committed") is not True or not metadata_ok:
        return "unverified_native"
    if proof.get("valid_slots") != 12 or proof.get("expected_slots") != 12:
        return "invalid"
    if not _native_matches(descriptor, proof, native):
        return "unverified_native"
    return "ready"


def _failure_reason(
    descriptor: Mapping[str, Any], proof: Mapping[str, Any] | None, reason: str
) -> str | None:
    value = (
        descriptor.get("blocked_reason")
        if descriptor.get("admission") == "blocked"
        else (proof or {}).get("status")
    )
    if descriptor.get("quantity") == "voc_concentration":
        value = "voc_unit_unresolved"
    if isinstance(value, str) and value in _KNOWN_FAILURES:
        return str(value)
    return "source_evidence_invalid" if reason == "blocked" else None


def _native_matches(
    descriptor: Mapping[str, Any], proof: Mapping[str, Any], native: Any
) -> bool:
    if (
        not isinstance(native, Mapping)
        or _finite(native.get("mean")) is None
        or _finite(proof.get("mean")) is None
    ):
        return False
    if any(native.get(field) is not None for field in ("state", "sum")):
        return False
    if any(native.get(field) != proof.get(field) for field in ("mean", "min", "max")):
        return False
    if descriptor["kind"] == "measurement":
        return _measurement_valid(native)
    return native.get("min") is None and native.get("max") is None


def _measurement_valid(row: Mapping[str, Any]) -> bool:
    mean, low, high = (_finite(row.get(field)) for field in ("mean", "min", "max"))
    return (
        mean is not None
        and low is not None
        and high is not None
        and low <= mean <= high
    )


def _logical_value(kind: str, native_mean: Any) -> float:
    value = _finite(native_mean)
    if value is None:
        raise ValueError("history_nonfinite_value")
    return value / {"runtime": 100, "degree_days": 24, "measurement": 1}[kind]


def _empty_bucket(
    start: datetime, end: datetime, evaluated_at: datetime, reason: str
) -> dict[str, Any]:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "value": None,
        "min": None,
        "max": None,
        "reason": reason,
        "failure_reason": None,
        "valid_slots": 0,
        "expected_slots": 12,
        "verified_hours": 0,
        "expected_hours": 1,
        "closed_hours": int(end <= evaluated_at),
        "observed_amount": None,
        "complete_total": None,
        "source_basis": "unavailable",
        "method_basis": METHOD_VERSION,
        "confidence": [],
        "source_ids": [],
        "eligible_intervals": [],
    }


def _slot_count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 12:
        raise ValueError("history_slot_count_invalid")
    return value


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("history_provenance_invalid")
    return sorted(set(value))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _sum(values: list[float]) -> float | None:
    if not values:
        return None
    try:
        value = math.fsum(values)
    except OverflowError as err:
        raise ValueError("history_aggregate_overflow") from err
    if not math.isfinite(value):
        raise ValueError("history_aggregate_overflow")
    return value


def _day_windows(query: _Query) -> list[tuple[datetime, datetime]]:
    zone = ZoneInfo(query.timezone)
    day = query.start.astimezone(zone).date()
    windows: list[tuple[datetime, datetime]] = []
    while (first := datetime.combine(day, time.min, zone).astimezone(UTC)) < query.end:
        end = datetime.combine(day + _DAY, time.min, zone).astimezone(UTC)
        if any(
            stamp.minute or stamp.second or stamp.microsecond for stamp in (first, end)
        ):
            raise ValueError("history_calendar_splits_utc_hour")
        if end <= first:
            raise ValueError("history_calendar_invalid")
        windows.append((first, end))
        day += _DAY
    return windows


def _day(
    query: _Query,
    descriptor: Mapping[str, Any],
    first: datetime,
    end: datetime,
    hours: list[dict[str, Any]],
    legacy: dict[str, Any] | None,
) -> dict[str, Any]:
    start, stop = max(query.start, first), min(query.end, end)
    first_index = (start - query.start) // _HOUR
    included = hours[first_index : first_index + (stop - start) // _HOUR]
    verified = [item for item in included if item["reason"] == "ready"]
    complete_points = (
        start == first and stop == end and len(verified) == (end - first) // _HOUR
    )
    bucket = _point_day(query, descriptor, start, stop, included, verified)
    bucket.update(
        {
            "calendar_start": first.isoformat(),
            "calendar_end": end.isoformat(),
            "calendar_hours": (end - first) // _HOUR,
        }
    )
    if complete_points:
        bucket.update({"reason": "ready", "eligible_intervals": _intervals(verified)})
        bucket["complete_total"] = bucket["observed_amount"]
        return bucket
    fallback = _legacy_value(query, descriptor, first, end, legacy)
    if (
        start == first
        and stop == end
        and fallback is not None
        and all(
            item["reason"] not in {"pending", "conflict", "blocked"}
            for item in included
        )
    ):
        return _legacy_day(bucket, descriptor, first, end, legacy, fallback)
    if legacy is not None:
        bucket["legacy_reason"] = _legacy_reason(query, legacy)
    return bucket


def _legacy_reason(query: _Query, legacy: Mapping[str, Any]) -> str:
    if (
        legacy.get("timezone") != query.timezone
        or legacy.get("timezone_revision") != query.timezone_revision
        or legacy.get("calendar_verified") is not True
    ):
        return "legacy_calendar_mismatch"
    return "legacy_day_not_eligible"


def _point_day(
    query: _Query,
    descriptor: Mapping[str, Any],
    start: datetime,
    end: datetime,
    included: list[dict[str, Any]],
    verified: list[dict[str, Any]],
) -> dict[str, Any]:
    amount = descriptor["kind"] != "measurement"
    total = _sum([item["value"] for item in verified])
    value = total if amount or total is None else total / len(verified)
    reason = "partial" if verified else _absent_day_reason(included)
    bucket = _empty_bucket(start, end, query.evaluated_at, reason)
    bucket.update(
        {
            "value": value,
            "min": min((item["min"] for item in verified), default=None)
            if not amount
            else None,
            "max": max((item["max"] for item in verified), default=None)
            if not amount
            else None,
            "valid_slots": sum(item["valid_slots"] for item in included),
            "expected_slots": len(included) * 12,
            "verified_hours": len(verified),
            "expected_hours": len(included),
            "closed_hours": sum(item["closed_hours"] for item in included),
            "observed_amount": total if amount else None,
            "point_observed_amount": total if amount else None,
            "source_basis": "points" if verified else "unavailable",
            "confidence": sorted(
                {label for item in included for label in item["confidence"]}
            ),
            "source_ids": sorted(
                {identifier for item in included for identifier in item["source_ids"]}
            ),
            "observed_intervals": _intervals(verified),
            "coverage_reasons": dict(Counter(item["reason"] for item in included)),
            "failure_reason": next(
                (
                    item["failure_reason"]
                    for item in included
                    if item["failure_reason"] is not None
                ),
                None,
            ),
        }
    )
    return bucket


def _absent_day_reason(hours: list[dict[str, Any]]) -> str:
    reasons = {item["reason"] for item in hours}
    return next(
        (
            reason
            for reason in (
                "pending",
                "blocked",
                "conflict",
                "invalid",
                "provisional",
                "unverified_native",
                "missing",
                "unassessed",
            )
            if reason in reasons
        ),
        "unassessed",
    )


def _intervals(hours: list[dict[str, Any]]) -> list[list[str]]:
    return [[item["start"], item["end"]] for item in hours]


def _legacy_value(
    query: _Query,
    descriptor: Mapping[str, Any],
    first: datetime,
    end: datetime,
    legacy: dict[str, Any] | None,
) -> float | None:
    if (
        legacy is None
        or end > query.evaluated_at
        or not _legacy_qualified(query, first, end, legacy)
    ):
        return None
    if descriptor["kind"] == "measurement":
        if legacy.get(
            "method"
        ) != "legacy_sample_mean" or not _legacy_measurement_valid(legacy):
            return None
        return _finite(legacy["mean"])
    return _legacy_difference(query, first, legacy)


def _legacy_measurement_valid(row: Mapping[str, Any]) -> bool:
    mean = _finite(row.get("mean"))
    if mean is None:
        return False
    for field in ("min", "max"):
        if row.get(field) is None:
            continue
        value = _finite(row[field])
        if (
            value is None
            or (field == "min" and value > mean)
            or (field == "max" and value < mean)
        ):
            return False
    return True


def _legacy_qualified(
    query: _Query, first: datetime, end: datetime, legacy: Mapping[str, Any]
) -> bool:
    expected = {
        "timezone": query.timezone,
        "timezone_revision": query.timezone_revision,
        "calendar_verified": True,
        "complete": True,
        "closed": True,
        "adoption_day": False,
        "native_verified": True,
    }
    if any(
        key not in legacy
        or type(legacy[key]) is not type(value)
        or legacy[key] != value
        for key, value in expected.items()
    ):
        return False
    if utc(legacy["start"]) != first or utc(legacy["end"]) != end:
        return False
    return bool(_strings(legacy.get("source_ids", [])))


def _legacy_difference(
    query: _Query, first: datetime, legacy: Mapping[str, Any]
) -> float | None:
    if (
        legacy.get("method") != "legacy_daily_cumulative"
        or legacy.get("predecessor_valid") is not True
    ):
        return None
    previous, current = _finite(legacy.get("previous_sum")), _finite(legacy.get("sum"))
    if (
        previous is None
        or current is None
        or not 0 <= previous <= current
        or "previous_start" not in legacy
    ):
        return None
    zone = ZoneInfo(query.timezone)
    prior = datetime.combine(
        first.astimezone(zone).date() - _DAY, time.min, zone
    ).astimezone(UTC)
    if utc(legacy["previous_start"]) != prior:
        return None
    return current - previous


def _legacy_day(
    bucket: dict[str, Any],
    descriptor: Mapping[str, Any],
    first: datetime,
    end: datetime,
    legacy: dict[str, Any] | None,
    value: float,
) -> dict[str, Any]:
    assert legacy is not None
    amount = descriptor["kind"] != "measurement"
    return {
        **bucket,
        "value": value,
        "min": legacy.get("min") if not amount else None,
        "max": legacy.get("max") if not amount else None,
        "reason": "legacy_day",
        "source_basis": "legacy_daily",
        "method_basis": legacy["method"],
        "observed_amount": value if amount else None,
        "complete_total": value if amount else None,
        "confidence": _strings(legacy.get("confidence", [])),
        "source_ids": _strings(legacy["source_ids"]),
        "eligible_intervals": [
            [stamp.isoformat(), (stamp + _HOUR).isoformat()]
            for stamp in _hour_starts(first, end)
        ],
    }


def _summary(
    query: _Query,
    descriptor: Mapping[str, Any],
    hours: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
) -> dict[str, Any]:
    verified = [item for item in hours if item["reason"] == "ready"]
    eligible = [item for item in buckets if item["eligible_intervals"]]
    common = {
        "scope": "query",
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
        "verified_hours": len(verified),
        "expected_hours": len(hours),
        "closed_hours": sum(item["closed_hours"] for item in hours),
        "eligible_hours": sum(len(item["eligible_intervals"]) for item in eligible),
        "eligible_buckets": len(eligible),
        "legacy_days": sum(item["source_basis"] == "legacy_daily" for item in buckets),
        "coverage_reasons": dict(Counter(item["reason"] for item in hours)),
    }
    if descriptor["kind"] != "measurement":
        total = _sum(
            [
                item["observed_amount"]
                for item in buckets
                if item["observed_amount"] is not None
            ]
        )
        return {
            **common,
            "observed_amount": total,
            "complete_total": total if len(eligible) == len(buckets) else None,
            "eligible_amount": _sum([item["value"] for item in eligible]),
        }
    total = _sum([item["value"] for item in verified])
    return {
        **common,
        "observed_mean": None if total is None else total / len(verified),
        "observed_min": min((item["min"] for item in verified), default=None),
        "observed_max": max((item["max"] for item in verified), default=None),
        "summary_basis": "verified_point_hours",
    }
