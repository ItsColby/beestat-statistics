"""Prepare immutable routine source deltas against an explicitly bound baseline.

The original input is the integration's JSON export, not provider wire bytes.
The delta retains exact input-byte commitments and changed original row objects;
it deliberately does not claim to reconstruct or retain unchanged input bytes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from .hourly_history_contract import (
    MAX_BUNDLE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_CHUNKS,
    MAX_SOURCE_RESOURCES,
    MAX_SOURCE_ROWS,
    bounds,
    digest,
    require_digest,
    utc,
)
from .hourly_sources import (
    _manifest_identity,
    _observation,
    _row_stamp,
    _validate_chunks,
    parse_source,
    validate_manifest,
)

DELTA_FORMAT = "integration_delta_v1"
EVALUATION_VERSION = "beestat_points_delta_v1"


def prepare_history_delta(
    incoming_acquisitions: list[dict[str, Any]],
    baseline: dict[str, Any],
    *,
    identity: dict[str, Any],
    start: str | datetime,
    end: str | datetime,
    evaluated_at: str | datetime,
    acquisition_id: str,
) -> dict[str, Any] | None:
    """Resolve complete ordered input, then retain only changed observations.

    Each incoming item is ``{manifest: SourceManifest, original_bytes: bytes}``.
    Chunk indices define order within each physical resource and all chunks
    belong to the explicit acquisition_id. The manager must recompute the bound
    baseline under its existing writer lock before accepting these same bytes.
    Missing input rows never remove baseline rows. This helper changes neither
    input, admits no source, and supplies no precedence over old acquisitions.
    """

    first, stop = bounds(start, end)
    evaluation = utc(evaluated_at)
    if stop > evaluation.replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    ):
        raise ValueError("history_delta_evaluation_before_window")
    _baseline(baseline, identity, first, stop)
    incoming = _incoming(
        incoming_acquisitions, identity, first, stop, evaluation, acquisition_id
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in incoming:
        manifest = item["manifest"]
        grouped[_resource_key(manifest)].append(item)
    chunks: list[dict[str, Any]] = []
    changed_resources: list[str] = []
    changed_rows = 0
    for key, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: item["manifest"]["chunk_index"])
        resource = baseline["resources"].get(key)
        rows = _changed_rows(ordered, resource, first, stop)
        if not rows:
            continue
        prepared = _chunks(rows, ordered, baseline, evaluation)
        chunks.extend({"resource_key": key, **chunk} for chunk in prepared)
        changed_resources.append(key)
        changed_rows += len(rows)
    if not chunks:
        return None
    if (
        len(chunks) > MAX_SOURCE_CHUNKS
        or sum(len(item["content"]) for item in chunks) > MAX_BUNDLE_BYTES
    ):
        raise ValueError("history_delta_bundle_limit")
    return {
        "format": DELTA_FORMAT,
        "evaluation_version": EVALUATION_VERSION,
        "baseline_source_revision": baseline["source_revision"],
        "baseline_digest": baseline["baseline_digest"],
        "acquisition_id": acquisition_id,
        "start": first.isoformat(),
        "end": stop.isoformat(),
        "evaluated_at": evaluation.isoformat(),
        "changed_resources": changed_resources,
        "changed_rows": changed_rows,
        "chunks": chunks,
    }


def _public_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    anchors = identity.get("account_anchors")
    if (
        not isinstance(anchors, list)
        or not anchors
        or any(not isinstance(item, str) or not item for item in anchors)
    ):
        raise ValueError("history_delta_identity_invalid")
    if any(
        not isinstance(identity.get(key), str) or not identity[key]
        for key in ("entry_id", "api_base")
    ):
        raise ValueError("history_delta_identity_invalid")
    return {
        "entry_id": identity["entry_id"],
        "api_base": identity["api_base"],
        "account_anchors": sorted(set(anchors)),
    }


def _baseline(
    baseline: Mapping[str, Any],
    identity: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> None:
    if baseline.get("evaluation_version") != EVALUATION_VERSION:
        raise ValueError("history_delta_evaluation_version")
    require_digest(baseline.get("source_revision"))
    require_digest(baseline.get("baseline_digest"))
    if (
        digest(
            {key: value for key, value in baseline.items() if key != "baseline_digest"}
        )
        != baseline["baseline_digest"]
    ):
        raise ValueError("history_delta_baseline_changed")
    if digest(_public_identity(baseline.get("identity", {}))) != digest(
        _public_identity(identity)
    ):
        raise ValueError("history_delta_baseline_identity")
    if utc(baseline["start"]) != start or utc(baseline["end"]) != end:
        raise ValueError("history_delta_baseline_window")
    resources = baseline.get("resources")
    if not isinstance(resources, dict) or len(resources) > MAX_SOURCE_RESOURCES:
        raise ValueError("history_delta_baseline_resources")
    for key, resource in resources.items():
        if key != _resource_key(resource):
            raise ValueError("history_delta_baseline_resource_identity")
        _manifest_identity(
            {
                "config_entry_id": identity["entry_id"],
                "api_base": identity["api_base"],
                "account_anchors": identity["account_anchors"],
                **resource,
            },
            identity,
        )


def _resource_key(resource: Mapping[str, Any]) -> str:
    if resource.get("resource") not in ("runtime_thermostat", "runtime_sensor") or any(
        type(resource.get(key)) is not int or resource[key] <= 0
        for key in ("resource_id", "thermostat_id")
    ):
        raise ValueError("history_delta_resource_identity")
    return f"{resource['resource']}:{resource['resource_id']}"


def _incoming(
    incoming: list[dict[str, Any]],
    identity: dict[str, Any],
    start: datetime,
    end: datetime,
    evaluated_at: datetime,
    acquisition_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(incoming, list) or len(incoming) > MAX_SOURCE_CHUNKS:
        raise ValueError("history_delta_input_limit")
    if not isinstance(acquisition_id, str) or not 0 < len(acquisition_id) <= 1024:
        raise ValueError("history_delta_acquisition_identity")
    result: list[dict[str, Any]] = []
    resources: set[str] = set()
    total = 0
    for item in incoming:
        content = item["original_bytes"]
        manifest = validate_manifest(item["manifest"], identity)
        if (
            manifest["source_kind"] != "provider"
            or manifest["format"] != "json"
            or manifest["acquisition_id"] != acquisition_id
        ):
            raise ValueError("history_delta_acquisition_identity")
        if (
            not start <= utc(manifest["start"]) < utc(manifest["end"]) <= end
            or utc(manifest["acquired_at"]) > evaluated_at
        ):
            raise ValueError("history_delta_input_window")
        rows = parse_source(content, manifest)
        total += len(content)
        resources.add(_resource_key(manifest))
        if total > MAX_BUNDLE_BYTES or len(resources) > MAX_SOURCE_RESOURCES:
            raise ValueError("history_delta_input_limit")
        result.append(
            {
                "manifest": manifest,
                "rows": rows,
                "sha256": sha256(content).hexdigest(),
                "byte_count": len(content),
                "row_count": len(rows),
            }
        )
    _validate_chunks(result)
    return result


def _baseline_rows(
    resource: Mapping[str, Any] | None,
) -> dict[datetime, dict[str, Any]]:
    if resource is None:
        return {}
    rows: dict[datetime, dict[str, Any]] = {}
    for row in resource.get("rows", []):
        stamp = _row_stamp(row)
        if stamp is None or stamp in rows:
            raise ValueError("history_delta_baseline_rows_invalid")
        rows[stamp] = row
    for timestamp, slot in resource.get("slots", {}).items():
        row = slot.get("row")
        if row is None:
            continue
        stamp = _row_stamp(row)
        if stamp is None or stamp != utc(timestamp):
            raise ValueError("history_delta_baseline_rows_invalid")
        if stamp in rows and _observation(row) != _observation(rows[stamp]):
            raise ValueError("history_delta_baseline_rows_inconsistent")
        rows[stamp] = row
    return rows


def _changed_rows(
    items: list[dict[str, Any]],
    resource: Mapping[str, Any] | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    before = _baseline_rows(resource)
    resolved: dict[datetime, dict[str, Any]] = {}
    unplaced: list[dict[str, Any]] = []
    for item in items:
        for row in item["rows"]:
            stamp = _row_stamp(row)
            if stamp is None:
                unplaced.append(row)
            elif start <= stamp < end:
                # Last row wins even when the later observation is invalid or a
                # tombstone. No per-chunk filtering can erase that final state.
                resolved[stamp] = row
    changed = [
        row
        for stamp, row in sorted(resolved.items())
        if stamp not in before or _observation(before[stamp]) != _observation(row)
    ]
    return [*changed, *unplaced]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _chunks(
    rows: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    baseline: Mapping[str, Any],
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    original = incoming[0]["manifest"]
    start = min(utc(item["manifest"]["start"]) for item in incoming)
    end = max(utc(item["manifest"]["end"]) for item in incoming)
    envelope = {
        "format": DELTA_FORMAT,
        "baseline_source_revision": baseline["source_revision"],
        "baseline_digest": baseline["baseline_digest"],
        "evaluation_version": EVALUATION_VERSION,
        "acquisition": {
            "acquisition_id": original["acquisition_id"],
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "evaluated_at": evaluated_at.isoformat(),
            "original_chunks": [
                {
                    key: item[key]
                    for key in ("sha256", "byte_count", "row_count", "manifest")
                }
                for item in incoming
            ],
            "original_bytes_kind": "integration_json_export",
            "original_bytes_retained": False,
        },
        "resource": {
            key: original[key] for key in ("resource", "resource_id", "thermostat_id")
        },
        "rows": [],
    }
    payloads = _partition_rows(rows, envelope)
    result: list[dict[str, Any]] = []
    for index, (content, row_count) in enumerate(payloads):
        checksum = sha256(content).hexdigest()
        manifest = {
            **original,
            "chunk_index": index,
            "chunk_count": len(payloads),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "acquired_at": evaluated_at.isoformat(),
            "original_sha256": checksum,
            "original_byte_count": len(content),
            "chunk_byte_offset": 0,
        }
        # Validate the exact self-original shape against the source parser before
        # returning any preparation; this does not stage or admit the delta.
        if len(parse_source(content, manifest)) != row_count:
            raise ValueError("history_delta_payload_mismatch")
        result.append(
            {
                "content": content,
                "manifest": deepcopy(manifest),
                "sha256": checksum,
                "row_count": row_count,
            }
        )
    return result


def _partition_rows(
    rows: list[dict[str, Any]], envelope: dict[str, Any]
) -> list[tuple[bytes, int]]:
    base_size = len(_json_bytes(envelope))
    if base_size >= MAX_SOURCE_BYTES:
        raise ValueError("history_delta_metadata_limit")
    result: list[tuple[bytes, int]] = []
    current: list[dict[str, Any]] = []
    size = base_size
    for row in rows:
        row_size = len(_json_bytes(row))
        if base_size + row_size > MAX_SOURCE_BYTES:
            raise ValueError("history_delta_row_limit")
        if current and (
            len(current) == MAX_SOURCE_ROWS or size + row_size + 1 > MAX_SOURCE_BYTES
        ):
            result.append((_json_bytes({**envelope, "rows": current}), len(current)))
            current, size = [], base_size
        size += row_size + bool(current)
        current.append(row)
    if current:
        result.append((_json_bytes({**envelope, "rows": current}), len(current)))
    return result
