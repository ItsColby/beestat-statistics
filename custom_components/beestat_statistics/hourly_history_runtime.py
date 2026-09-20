"""Routine source acquisition under the existing entry importer and its lock.

This helper owns no scheduler, credential, cache or writer. The importer supplies
its current client, guarded context and accepted history manager. Capture bytes
are integration-generated raw-point exports, not an assertion of provider wire
serialization. Only sealed changed-point projections become new source objects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .hourly_history_contract import MAX_SOURCE_BYTES, utc
from .hourly_history_delta import prepare_history_delta
from .hourly_sources import _row_stamp, async_stage_source
from .raw_points import async_read_raw_points, parse_raw_point_request

if TYPE_CHECKING:
    from . import BeestatStatisticsImporter


def _serialize_capture(response: dict[str, Any]) -> tuple[bytes, str]:
    """Serialize an exclusively owned acquisition without touching runtime state."""

    content = json.dumps(
        response, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("history_routine_acquisition_exceeds_limit")
    return content, sha256(content).hexdigest()


async def async_refresh_history(
    importer: BeestatStatisticsImporter,
    context: dict[str, Any],
    *,
    lookback_days: int,
) -> dict[str, Any]:
    """Capture one bounded acquisition and accept only its exact sealed delta."""
    now = utc(context["evaluated_at"]).replace(microsecond=0)
    current = now.replace(minute=0, second=0, microsecond=0)
    start, end = (
        current - timedelta(days=min(lookback_days, 45)),
        current + timedelta(hours=1),
    )
    baseline = await importer.hourly.async_history_source_points(
        {"start": start.isoformat(), "end": end.isoformat()}, context=context
    )
    context["check_current"]()
    acquisition_id = f"routine-{uuid4().hex}"
    incoming = await _capture(importer, context, start, now, acquisition_id)
    context["check_current"]()
    evaluated_at = datetime.now(UTC)
    delta = await importer._hass.async_add_executor_job(
        partial(
            prepare_history_delta,
            incoming,
            baseline,
            identity=context["identity"],
            start=start,
            end=end,
            evaluated_at=evaluated_at,
            acquisition_id=acquisition_id,
        )
    )
    context["check_current"]()
    if delta is None:
        # Reevaluate retained evidence at this pass's fixed evaluation. Closure
        # alone earns no credit: the manager still proves any native transition.
        return await importer.hourly.async_refresh_history(
            {"source_ids": [], "start": start.isoformat(), "end": end.isoformat()},
            context=context,
        )
    source_ids: list[str] = []
    for chunk in delta["chunks"]:
        context["check_current"]()
        receipt = await async_stage_source(
            importer.hourly._store,
            chunk["content"],
            chunk["manifest"],
            chunk["sha256"],
            context["identity"],
        )
        context["check_current"]()
        source_ids.append(receipt["source_id"])
    return await importer.hourly.async_refresh_history(
        {
            "source_ids": source_ids,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        context=context,
    )


async def _capture(
    importer: BeestatStatisticsImporter,
    context: dict[str, Any],
    start: datetime,
    end: datetime,
    acquisition_id: str,
) -> list[dict[str, Any]]:
    """Read complete ordered resource acquisitions before comparing any points."""
    current = importer._coordinator.data
    selected = importer.hourly.history_configuration(context)["quantities"]
    eligible = {
        item["quantity_id"]
        for item in context["descriptors"]
        if item["admission"] == "eligible"
    }
    resources: dict[tuple[str, int], int] = {}
    for descriptor in selected:
        if (
            descriptor["writer_status"] == "unselected"
            or descriptor["quantity_id"] not in eligible
        ):
            continue
        sensor = descriptor["sensor_id"]
        resource = "runtime_thermostat" if sensor is None else "runtime_sensor"
        resource_id = descriptor["thermostat_id"] if sensor is None else sensor
        resources[(resource, resource_id)] = descriptor["thermostat_id"]
    incoming: list[dict[str, Any]] = []
    for (resource, resource_id), thermostat_id in sorted(resources.items()):
        resource_chunks: list[dict[str, Any]] = []
        first = start
        while first < end:
            stop = min(end, first + timedelta(days=30))
            request = parse_raw_point_request(resource, resource_id, first, stop)
            runtime = importer._coordinator.beestat_config_entry.runtime_data
            identity = importer._raw_point_identity(request, runtime)
            response = await async_read_raw_points(importer._client, request, identity)
            context["check_current"]()
            if response.get("status") != "success":
                raise ValueError("history_routine_acquisition_unavailable")
            content, original_hash = await importer._hass.async_add_executor_job(
                _serialize_capture, response
            )
            context["check_current"]()
            # The provider horizon comes from the captured metadata, never the
            # query end or elapsed time. Parser validates every captured row.
            parent = next(
                row
                for row in current.thermostat_rows
                if str(row.get("thermostat_id", row.get("id"))) == str(thermostat_id)
            )
            source_end = _row_stamp({"timestamp": parent.get("data_end")})
            if source_end is None:
                raise ValueError("history_routine_source_horizon_unavailable")
            resource_chunks.append(
                {
                    "original_bytes": content,
                    "manifest": {
                        "contract_version": 3,
                        "config_entry_id": context["identity"]["entry_id"],
                        "api_base": context["identity"]["api_base"],
                        "account_anchors": context["identity"]["account_anchors"],
                        "resource": resource,
                        "resource_id": resource_id,
                        "thermostat_id": thermostat_id,
                        "source_kind": "provider",
                        "acquisition_id": acquisition_id,
                        "chunk_index": len(resource_chunks),
                        "chunk_count": 1,
                        "format": "json",
                        "start": first.isoformat(),
                        "end": stop.isoformat(),
                        "source_end": source_end.isoformat(),
                        "unit_contract": "beestat_points_v1",
                        "original_sha256": original_hash,
                        "original_byte_count": len(content),
                        "chunk_byte_offset": 0,
                    },
                }
            )
            first = stop
        # All ordered windows belong to one resource acquisition. Completion
        # is known only after its final response; original export bytes retain
        # each request's own timestamps and exact hash independently.
        acquired_at = datetime.now(UTC).isoformat()
        for chunk in resource_chunks:
            chunk["manifest"]["acquired_at"] = acquired_at
            chunk["manifest"]["chunk_count"] = len(resource_chunks)
        incoming.extend(resource_chunks)
    return incoming
