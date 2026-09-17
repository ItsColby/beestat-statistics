"""Bounded, read-only source point responses without import or repair effects."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .api import BeestatClient, BeestatRawReadError
from .config_model import BeestatConfig

RAW_POINT_MAX_DAYS = 31
RAW_POINT_MAX_ROWS = 10_000
RAW_POINT_MAX_BYTES = 8 * 1024 * 1024
type PointResource = Literal["runtime_thermostat", "runtime_sensor"]


@dataclass(frozen=True, slots=True)
class RawPointRequest:
    """One explicit resource and UTC window; provider `between` is inclusive."""

    resource: PointResource
    resource_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class RawPointIdentity:
    """Frozen configured and cached-provider identity, rechecked by the owner."""

    config_entry_id: str
    resource: PointResource
    resource_id: int
    thermostat_id: int
    metadata_fetched_at: str


def _utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Raw point timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def parse_raw_point_request(
    resource: str, resource_id: int, start: datetime | str, end: datetime | str
) -> RawPointRequest:
    """Validate all public bounds before contacting the provider."""

    selected_resource: PointResource
    if resource == "runtime_thermostat":
        selected_resource = "runtime_thermostat"
    elif resource == "runtime_sensor":
        selected_resource = "runtime_sensor"
    else:
        raise ValueError("Unsupported raw point resource")
    if (
        isinstance(resource_id, bool)
        or not isinstance(resource_id, int)
        or resource_id <= 0
    ):
        raise ValueError("Raw point resource_id must be a positive integer")
    start_utc, end_utc = _utc(start), _utc(end)
    if start_utc.microsecond or end_utc.microsecond:
        raise ValueError("Raw point timestamps must use whole seconds")
    if end_utc <= start_utc or end_utc - start_utc > timedelta(days=RAW_POINT_MAX_DAYS):
        raise ValueError("Raw point window must be positive and at most 31 days")
    return RawPointRequest(selected_resource, resource_id, start_utc, end_utc)


def _provider_row(
    rows: Sequence[dict[str, Any]], resource_id: int, id_field: str
) -> dict[str, Any]:
    matches = [
        row
        for row in rows
        if _resource_id(row.get(id_field, row.get("id"))) == resource_id
    ]
    if len(matches) != 1:
        raise ValueError("Raw point provider identity is absent or ambiguous")
    return matches[0]


def _resource_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def validate_raw_point_identity(
    request: RawPointRequest,
    config: BeestatConfig,
    thermostat_rows: Sequence[dict[str, Any]],
    sensor_rows: Sequence[dict[str, Any]],
    *,
    config_entry_id: str,
    metadata_fetched_at: datetime,
) -> RawPointIdentity:
    """Admit configured IDs only when their cached provider parent agrees."""

    thermostat_id = request.resource_id
    if request.resource == "runtime_sensor":
        sensors = [
            item for item in config.sensors if item.sensor_id == request.resource_id
        ]
        if len(sensors) != 1 or sensors[0].thermostat_id is None:
            raise ValueError(
                "Raw point sensor is not uniquely configured with a parent"
            )
        thermostat_id = sensors[0].thermostat_id
        sensor = _provider_row(sensor_rows, request.resource_id, "sensor_id")
        if _resource_id(sensor.get("thermostat_id")) != thermostat_id:
            raise ValueError("Raw point sensor parent identity disagrees")
    if sum(item.thermostat_id == thermostat_id for item in config.thermostats) != 1:
        raise ValueError("Raw point thermostat is not uniquely configured")
    _provider_row(thermostat_rows, thermostat_id, "thermostat_id")
    if not config_entry_id:
        raise ValueError("Raw point config entry identity is missing")
    return RawPointIdentity(
        config_entry_id,
        request.resource,
        request.resource_id,
        thermostat_id,
        _utc(metadata_fetched_at).isoformat(),
    )


def _row_count(data: object) -> tuple[int | None, str]:
    if data is None:
        return 0, "null"
    if isinstance(data, list):
        return len(data), "list"
    if isinstance(data, dict):
        if not data:
            return 0, "mapping"
        if all(isinstance(value, dict) or value is None for value in data.values()):
            return len(data), "mapping"
        return 1, "object"
    return None, "unsupported"


async def async_read_raw_points(
    client: BeestatClient, request: RawPointRequest, identity: RawPointIdentity
) -> dict[str, Any]:
    """Read fixed source methods only; never replace failures with empty data."""

    if (identity.resource, identity.resource_id) != (
        request.resource,
        request.resource_id,
    ):
        raise ValueError("Raw point request and verified identity disagree")
    response: dict[str, Any] = {
        "schema_version": 1,
        "identity": asdict(identity),
        "request": {
            "resource": request.resource,
            "method": "read",
            "resource_id": request.resource_id,
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
            "timestamp_operator": "between",
            "boundary": "inclusive",
        },
        "limits": {
            "max_days": RAW_POINT_MAX_DAYS,
            "max_rows": RAW_POINT_MAX_ROWS,
            "max_response_bytes": RAW_POINT_MAX_BYTES,
        },
        "completeness": {
            "transport_complete": False,
            "provider_complete": None,
            "sample_completeness": None,
            "provider_settlement": None,
            "pagination_indicated": False,
            "truncated": False,
            "reason": "Provider history completeness is not established by this read",
        },
        "started_at": datetime.now(UTC).isoformat(),
    }
    read = (
        client.async_read_runtime_thermostat
        if request.resource == "runtime_thermostat"
        else client.async_read_runtime_sensor
    )
    try:
        result = await read(
            request.resource_id,
            request.start.isoformat(),
            request.end.isoformat(),
            raw_response=True,
            max_response_bytes=RAW_POINT_MAX_BYTES,
        )
    except BeestatRawReadError as err:
        response.update(
            status="failed", error=str(err), attempts=[asdict(a) for a in err.attempts]
        )
    else:
        count, shape = _row_count(result.data)
        response.update(
            attempts=[asdict(a) for a in result.attempts],
            response_bytes=result.response_bytes,
            row_count=count,
            data_shape=shape,
        )
        response["completeness"].update(
            transport_complete=True,
            pagination_indicated=result.pagination_indicated,
        )
        if result.pagination_indicated:
            response["completeness"].update(
                provider_complete=False,
                truncated=True,
                reason="Provider indicated an incomplete response",
            )
            response.update(
                status="failed",
                error="Provider indicated truncation or additional pages; request a smaller window",
            )
        elif count is None:
            response.update(status="failed", error="Unsupported raw point data shape")
        elif count > RAW_POINT_MAX_ROWS:
            response.update(
                status="failed",
                error="Raw point row limit exceeded; request a smaller window",
            )
        else:
            response.update(status="success", data=result.data)
    response["finished_at"] = datetime.now(UTC).isoformat()
    try:
        encoded_size = len(
            json.dumps(response, ensure_ascii=True, allow_nan=False).encode()
        )
    except TypeError, ValueError:
        response.pop("data", None)
        response.update(status="failed", error="Raw point response is not finite JSON")
    else:
        if encoded_size > RAW_POINT_MAX_BYTES:
            response.pop("data", None)
            response.update(
                status="failed",
                error="Raw point response size limit exceeded; request a smaller window",
            )
    return response
