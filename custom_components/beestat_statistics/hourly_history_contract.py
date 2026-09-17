"""Versioned, dependency-light contract for qualified logical history.

Recorder identifiers describe storage representation. Consumers select explicit
physical quantity identities from configuration and never derive an identifier.
This module defines the additive v3 contract; v2 calls remain unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, NotRequired, TypedDict

CONTRACT_VERSION = 3
METHOD_VERSION = "five_minute_complete_hour_v3"
DAILY_POLICY = "complete_points_else_legacy_day"
MAX_QUANTITIES = 40
MAX_DAYS = 366
MAX_BUCKETS = 4096
MAX_BATCH_HOURS = 744
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ROWS = 10_000
MAX_ORIGINAL_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_CHUNKS = 2048
MAX_SOURCE_RESOURCES = 128
MAX_JSON_DEPTH = 16
MIN_FREE_BYTES = 256 * 1024 * 1024
SERVICE_STAGE_HOURLY_SOURCE = "stage_hourly_source"
SERVICE_PLAN_HOURLY_HISTORY = "plan_hourly_history"
SERVICE_APPLY_HOURLY_HISTORY = "apply_hourly_history"
_DIGEST = re.compile(r"[0-9a-f]{64}")

type QuantityKind = Literal["runtime", "degree_days", "measurement"]
type SourceKind = Literal["provider", "archive"]
type HistoryPeriod = Literal["hour", "day"]


class SourceManifest(TypedDict):
    """Caller declaration sealed with the exact original UTF-8 chunk bytes.

    An acquisition's increasing chunk_index preserves provider row ordering.
    Separate acquisitions do not acquire precedence merely through list order.
    The archive policy is an explicit admission qualification, not provider proof.
    """

    contract_version: int
    config_entry_id: str
    api_base: str
    account_anchors: list[str]
    resource: Literal["runtime_thermostat", "runtime_sensor"]
    resource_id: int
    thermostat_id: int
    source_kind: SourceKind
    acquisition_id: str
    chunk_index: int
    chunk_count: int
    format: Literal["json", "jsonl"]
    original_sha256: str
    original_byte_count: int
    chunk_byte_offset: int
    start: str
    end: str
    acquired_at: str
    source_end: str
    unit_contract: Literal["beestat_points_v1"]


class HistoryIdentity(TypedDict):
    entry_id: str
    api_base: str
    account_anchors: list[str]


class HistoryRepresentation(TypedDict):
    kind: Literal["arithmetic_mean"]
    native_field: Literal["mean"]
    unit_of_measurement: str
    unit_class: str | None
    logical_multiplier: float
    v2_statistic_id: str


class QuantityDescriptor(TypedDict):
    """Stable physical identity and exact representation/legacy mapping."""

    quantity_id: str
    thermostat_id: int
    sensor_id: int | None
    quantity: str
    kind: QuantityKind
    logical_unit: str
    method_version: str
    statistic_id: str
    legacy_statistic_ids: list[str]
    representation: HistoryRepresentation
    admission: Literal["eligible", "blocked"]
    writer_status: Literal["unselected", "reserved", "adopted"]
    blocked_reason: NotRequired[str]


type CoverageReason = Literal[
    "ready",
    "partial",
    "legacy_day",
    "missing",
    "invalid",
    "provisional",
    "pending",
    "conflict",
    "unassessed",
    "unverified_native",
    "blocked",
]


class HistoryOperation(TypedDict):
    status: Literal["unselected", "accepted", "in_progress", "completed", "blocked"]
    root_revision: int
    has_pending: bool
    contract_version: NotRequired[int]
    operation_id: NotRequired[str]
    plan_digest: NotRequired[str]
    cursor: NotRequired[int]
    batch_count: NotRequired[int]
    verified_batches: NotRequired[int]
    remaining_batches: NotRequired[int]
    error: NotRequired[str | None]


class HistoryBucket(TypedDict):
    """One half-open bucket; absent evidence is null, never fabricated zero."""

    start: str
    end: str
    value: float | None
    min: float | None
    max: float | None
    reason: CoverageReason
    failure_reason: str | None
    valid_slots: int
    expected_slots: int
    verified_hours: int
    expected_hours: int
    closed_hours: int
    observed_amount: float | None
    complete_total: float | None
    source_basis: Literal["points", "legacy_daily", "unavailable"]
    method_basis: str
    confidence: list[str]
    source_ids: list[str]
    eligible_intervals: list[list[str]]
    calendar_start: NotRequired[str]
    calendar_end: NotRequired[str]
    calendar_hours: NotRequired[int]
    point_observed_amount: NotRequired[float | None]
    observed_intervals: NotRequired[list[list[str]]]
    coverage_reasons: NotRequired[dict[str, int]]
    legacy_reason: NotRequired[
        Literal["legacy_calendar_mismatch", "legacy_day_not_eligible"]
    ]


class HistorySummary(TypedDict):
    """Whole-query summary per quantity; repeated page copies MUST NOT be added."""

    scope: Literal["query"]
    start: str
    end: str
    verified_hours: int
    expected_hours: int
    closed_hours: int
    eligible_hours: int
    eligible_buckets: int
    legacy_days: int
    coverage_reasons: dict[str, int]
    observed_amount: NotRequired[float | None]
    complete_total: NotRequired[float | None]
    eligible_amount: NotRequired[float | None]
    observed_mean: NotRequired[float | None]
    observed_min: NotRequired[float | None]
    observed_max: NotRequired[float | None]
    summary_basis: NotRequired[Literal["verified_point_hours"]]


class HistoryPagination(TypedDict):
    offset: int
    next_offset: int | None
    has_more: bool
    total_buckets: int


class HistorySeriesResponse(TypedDict):
    descriptor: QuantityDescriptor
    buckets: list[HistoryBucket]
    summary: HistorySummary


class HistoryResponse(TypedDict):
    contract_version: Literal[3]
    identity: HistoryIdentity
    config_revision: str
    root_revision: int
    source_revision: str | None
    coverage_revision: int
    method_version: str
    daily_policy: Literal["complete_points_else_legacy_day"]
    period: HistoryPeriod
    start: str
    end: str
    evaluated_at: str
    timezone: str
    timezone_revision: int
    native_verification: Literal["bounded_readback"]
    operation: HistoryOperation
    view_token: str
    pagination: HistoryPagination
    series: list[HistorySeriesResponse]


def digest(value: Any) -> str:
    """Bind finite JSON values without Python's bool/int equality ambiguity."""
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def require_digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("history_digest_invalid")
    return value


def utc(value: str | datetime, *, whole_hour: bool = False) -> datetime:
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("history_timestamp_requires_offset")
    stamp = stamp.astimezone(UTC)
    if whole_hour and (stamp.minute or stamp.second or stamp.microsecond):
        raise ValueError("history_timestamp_requires_whole_hour")
    return stamp


def bounds(start: str | datetime, end: str | datetime) -> tuple[datetime, datetime]:
    first, stop = utc(start, whole_hour=True), utc(end, whole_hour=True)
    if not first < stop <= first + timedelta(days=MAX_DAYS):
        raise ValueError("history_window_invalid")
    return first, stop


def quantity_id(resource: Mapping[str, Any]) -> str:
    """Identify the physical quantity within its explicit entry/account scope."""
    thermostat = resource.get("thermostat_id")
    sensor = resource.get("sensor_id")
    quantity = resource.get("quantity")
    if (
        type(thermostat) is not int
        or thermostat <= 0
        or (sensor is not None and (type(sensor) is not int or sensor <= 0))
        or not isinstance(quantity, str)
        or re.fullmatch(r"[a-z][a-z0-9_]*", quantity) is None
    ):
        raise ValueError("history_quantity_identity_invalid")
    owner = f"thermostat:{thermostat}" if sensor is None else f"sensor:{sensor}"
    return f"{owner}:{quantity}"


def capabilities() -> dict[str, Any]:
    """Return an additive contract descriptor, without claiming data admission."""
    return {
        "contract_version": CONTRACT_VERSION,
        "method_version": METHOD_VERSION,
        "daily_policy": DAILY_POLICY,
        "services": {
            "configuration": "beestat_statistics.get_configuration",
            "coverage": "beestat_statistics.get_hourly_coverage",
            "stage": f"beestat_statistics.{SERVICE_STAGE_HOURLY_SOURCE}",
            "plan": f"beestat_statistics.{SERVICE_PLAN_HOURLY_HISTORY}",
            "apply": f"beestat_statistics.{SERVICE_APPLY_HOURLY_HISTORY}",
        },
        "periods": ["hour", "day"],
        "limits": {
            "quantities": MAX_QUANTITIES,
            "days": MAX_DAYS,
            "page_buckets": MAX_BUCKETS,
            "batch_hours": MAX_BATCH_HOURS,
            "source_bytes": MAX_SOURCE_BYTES,
            "source_rows": MAX_SOURCE_ROWS,
            "original_bytes": MAX_ORIGINAL_BYTES,
            "bundle_bytes": MAX_BUNDLE_BYTES,
            "source_chunks": MAX_SOURCE_CHUNKS,
            "source_resources": MAX_SOURCE_RESOURCES,
        },
        "native_verification": "bounded_readback",
        "source_settlement": "not_inferred",
        "pagination": "view_token_and_offset",
        "runtime_amount": "component_hours",
        "degree_days_base_fahrenheit": 65,
    }
