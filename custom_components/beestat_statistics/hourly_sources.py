"""Bounded immutable source admission and deterministic qualified resolution.

No function here changes the active journal, marker, Recorder or provider. A
sealed manifest references original bytes; its digest is a lookup handle, never
authorization. The entry owner supplies current verified account/resource data.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from itertools import pairwise
from typing import Any, Protocol

from .hourly_history_contract import (
    CONTRACT_VERSION,
    MAX_BUNDLE_BYTES,
    MAX_JSON_DEPTH,
    MAX_ORIGINAL_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_CHUNKS,
    MAX_SOURCE_RESOURCES,
    MAX_SOURCE_ROWS,
    SourceManifest,
    bounds,
    digest,
    require_digest,
    utc,
)

_MANIFEST_FIELDS = frozenset(SourceManifest.__annotations__)
_CONFIDENCE = {"provider": "provider_ordered", "archive": "archive_qualified"}


class SourceStore(Protocol):
    """The immutable object portion of the existing HourlyStore."""

    async def async_write_object(self, kind: str, content: bytes) -> str: ...

    async def async_read_object(self, kind: str, digest: str) -> bytes: ...

    async def async_process_source_job[T](
        self, function: Callable[..., T], *args: Any
    ) -> T: ...


class SynchronousSourceStore(Protocol):
    """Executor-only immutable writes inside the native upload context."""

    def write_object(self, kind: str, content: bytes) -> str: ...


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("history_source_duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("history_source_nonfinite_json")


def _depth(value: Any) -> None:
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if level > MAX_JSON_DEPTH:
            raise ValueError("history_source_json_depth")
        if isinstance(item, dict):
            pending.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, level + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("history_source_nonfinite_json")


def _parse_json(text: str) -> Any:
    try:
        value = json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant
        )
    except RecursionError as err:
        raise ValueError("history_source_json_depth") from err
    _depth(value)
    return value


def _positive(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _manifest_identity(
    manifest: Mapping[str, Any], identity: Mapping[str, Any]
) -> None:
    anchors = manifest["account_anchors"]
    if (
        manifest["config_entry_id"] != identity.get("entry_id")
        or manifest["api_base"] != identity.get("api_base")
        or not isinstance(anchors, list)
        or not anchors
        or any(not isinstance(item, str) or not item for item in anchors)
        or len(set(anchors)) != len(anchors)
        or not set(anchors).intersection(identity.get("account_anchors", []))
    ):
        raise ValueError("history_source_account_mismatch")
    resource_id = manifest["resource_id"]
    thermostat_id = manifest["thermostat_id"]
    resource = manifest["resource"]
    resources = identity.get("resources")
    if not isinstance(resources, Mapping) or not resources:
        raise ValueError("history_source_resource_unverified")
    candidates = [item for item in resources.values() if isinstance(item, Mapping)]
    if resource == "runtime_thermostat":
        matched = [
            item
            for item in candidates
            if item.get("thermostat_id") == resource_id
            and item.get("sensor_id") is None
        ]
        if thermostat_id != resource_id or not matched:
            raise ValueError("history_source_resource_mismatch")
    else:
        matched = [item for item in candidates if item.get("sensor_id") == resource_id]
        if not matched or any(
            item.get("thermostat_id") != thermostat_id for item in matched
        ):
            raise ValueError("history_source_parent_mismatch")


def validate_manifest(
    manifest: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    """Detach an exact v3 declaration, matched to current physical ownership."""
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("history_source_manifest_fields")
    if (
        type(manifest["contract_version"]) is not int
        or manifest["contract_version"] != CONTRACT_VERSION
        or manifest["resource"] not in ("runtime_thermostat", "runtime_sensor")
        or manifest["source_kind"] not in _CONFIDENCE
        or manifest["format"] not in ("json", "jsonl")
        or manifest["unit_contract"] != "beestat_points_v1"
    ):
        raise ValueError("history_source_manifest_contract")
    for key in ("config_entry_id", "api_base", "acquisition_id"):
        if not isinstance(manifest[key], str) or not 0 < len(manifest[key]) <= 1024:
            raise ValueError("history_source_manifest_identity")
    for key in ("resource_id", "thermostat_id"):
        if type(manifest[key]) is not int or manifest[key] <= 0:
            raise ValueError("history_source_manifest_identity")
    count, index = manifest["chunk_count"], manifest["chunk_index"]
    if (
        type(count) is not int
        or type(index) is not int
        or not 1 <= count <= MAX_SOURCE_CHUNKS
        or not 0 <= index < count
    ):
        raise ValueError("history_source_manifest_chunks")
    _original_declaration(manifest)
    for key in ("start", "end", "acquired_at", "source_end"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ValueError("history_source_manifest_time")
    start, end = utc(manifest["start"]), utc(manifest["end"])
    acquired_at = utc(manifest["acquired_at"])
    if not start < end <= acquired_at or utc(manifest["source_end"]) > acquired_at:
        raise ValueError("history_source_manifest_time")
    _manifest_identity(manifest, identity)
    return deepcopy(manifest)


def _original_declaration(manifest: Mapping[str, Any]) -> None:
    require_digest(manifest["original_sha256"])
    size, offset = manifest["original_byte_count"], manifest["chunk_byte_offset"]
    if (
        type(size) is not int
        or type(offset) is not int
        or not 0 <= offset <= size <= MAX_ORIGINAL_BYTES
    ):
        raise ValueError("history_source_original_bounds")


def _original_chunk(content: bytes, manifest: Mapping[str, Any]) -> None:
    """Admit complete UTF-8 rows and bind their exact original byte location."""
    size, offset = manifest["original_byte_count"], manifest["chunk_byte_offset"]
    end = offset + len(content)
    if end > size or (not content and size):
        raise ValueError("history_source_original_bounds")
    if manifest["format"] == "json" and (offset or end != size):
        raise ValueError("history_source_json_requires_complete_original")
    if manifest["format"] == "jsonl" and end < size and not content.endswith(b"\n"):
        raise ValueError("history_source_chunk_requires_complete_lines")
    if (
        offset == 0
        and end == size
        and sha256(content).hexdigest() != manifest["original_sha256"]
    ):
        raise ValueError("history_source_original_digest_mismatch")


def _raw_envelope(value: dict[str, Any], manifest: dict[str, Any]) -> Any:
    complete = value.get("completeness")
    if (
        value.get("status") != "success"
        or not isinstance(complete, dict)
        or complete.get("transport_complete") is not True
        or complete.get("truncated") is not False
        or complete.get("pagination_indicated") is not False
    ):
        raise ValueError("history_source_incomplete_export")
    request, identity = value.get("request"), value.get("identity")
    if not isinstance(request, dict) or not isinstance(identity, dict) or not identity:
        raise ValueError("history_source_export_identity")
    expected = {
        "resource": manifest["resource"],
        "resource_id": manifest["resource_id"],
        "method": "read",
        "timestamp_operator": "between",
        "boundary": "inclusive",
    }
    if any(request.get(key) != item for key, item in expected.items()):
        raise ValueError("history_source_export_identity")
    if any(
        identity.get(key) != manifest[key]
        for key in ("config_entry_id", "resource", "resource_id", "thermostat_id")
    ):
        raise ValueError("history_source_export_identity")
    if any(
        not isinstance(request.get(key), str) or utc(request[key]) != utc(manifest[key])
        for key in ("start", "end")
    ):
        raise ValueError("history_source_export_window")
    return value.get("data")


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        is_mapping = "timestamp" not in value and all(
            isinstance(item, dict) or item is None for item in value.values()
        )
        value = list(value.values()) if is_mapping else [value]
    if not isinstance(value, list) or len(value) > MAX_SOURCE_ROWS:
        raise ValueError("history_source_row_limit_or_shape")
    # A null tombstone without an instant cannot be placed. Preserve it as an
    # invalid row so resolution blocks only this declared resource/window.
    if any(item is not None and not isinstance(item, dict) for item in value):
        raise ValueError("history_source_row_shape")
    return [item if item is not None else {} for item in value]


def _row_stamp(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        # The existing Beestat point contract defines offsetless source stamps
        # as UTC; manifest/envelope timestamps must remain explicitly zoned.
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else utc(parsed)
    except ValueError, OverflowError:
        return None


def _delta_envelope(
    value: dict[str, Any], manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate commitments without claiming discarded input was retained."""
    if (
        set(value)
        != {
            "format",
            "baseline_source_revision",
            "baseline_digest",
            "evaluation_version",
            "acquisition",
            "resource",
            "rows",
        }
        or manifest["source_kind"] != "provider"
    ):
        raise ValueError("history_source_delta_contract")
    if (
        value["format"] != "integration_delta_v1"
        or value["evaluation_version"] != "beestat_points_delta_v1"
    ):
        raise ValueError("history_source_delta_contract")
    require_digest(value["baseline_source_revision"])
    require_digest(value["baseline_digest"])
    if not isinstance(value["rows"], list) or len(value["rows"]) > MAX_SOURCE_ROWS:
        raise ValueError("history_source_delta_rows_invalid")
    resource = value["resource"]
    expected = {
        key: manifest[key] for key in ("resource", "resource_id", "thermostat_id")
    }
    if not isinstance(resource, dict) or digest(resource) != digest(expected):
        raise ValueError("history_source_delta_resource")
    _delta_acquisition(value["acquisition"], manifest)
    return _rows(value["rows"]), deepcopy(
        {key: item for key, item in value.items() if key != "rows"}
    )


def _delta_acquisition(acquisition: Any, manifest: Mapping[str, Any]) -> None:
    if not isinstance(acquisition, dict) or set(acquisition) != {
        "acquisition_id",
        "bounds",
        "evaluated_at",
        "original_chunks",
        "original_bytes_kind",
        "original_bytes_retained",
    }:
        raise ValueError("history_source_delta_acquisition")
    if (
        acquisition["acquisition_id"] != manifest["acquisition_id"]
        or acquisition["original_bytes_kind"] != "integration_json_export"
        or acquisition["original_bytes_retained"] is not False
        or utc(acquisition["evaluated_at"]) != utc(manifest["acquired_at"])
    ):
        raise ValueError("history_source_delta_acquisition")
    window = acquisition["bounds"]
    if not isinstance(window, dict) or set(window) != {"start", "end"}:
        raise ValueError("history_source_delta_bounds")
    if any(utc(window[key]) != utc(manifest[key]) for key in ("start", "end")):
        raise ValueError("history_source_delta_bounds")
    chunks = acquisition["original_chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_SOURCE_CHUNKS:
        raise ValueError("history_source_delta_chunks")
    total = sum(
        _delta_incoming(chunk, index, len(chunks), manifest)
        for index, chunk in enumerate(chunks)
    )
    if total > MAX_BUNDLE_BYTES:
        raise ValueError("history_source_bundle_limit")


def _delta_incoming(
    chunk: Any, index: int, count: int, manifest: Mapping[str, Any]
) -> int:
    if not isinstance(chunk, dict) or set(chunk) != {
        "sha256",
        "byte_count",
        "row_count",
        "manifest",
    }:
        raise ValueError("history_source_delta_commitment")
    require_digest(chunk["sha256"])
    size, rows = chunk["byte_count"], chunk["row_count"]
    if (
        type(size) is not int
        or type(rows) is not int
        or not 0 <= size <= MAX_SOURCE_BYTES
        or not 0 <= rows <= MAX_SOURCE_ROWS
    ):
        raise ValueError("history_source_delta_commitment")
    original = chunk["manifest"]
    if not isinstance(original, dict) or set(original) != _MANIFEST_FIELDS:
        raise ValueError("history_source_delta_commitment")
    fields = (
        "contract_version",
        "config_entry_id",
        "api_base",
        "account_anchors",
        "resource",
        "resource_id",
        "thermostat_id",
        "source_kind",
        "acquisition_id",
        "unit_contract",
    )
    if digest({key: original[key] for key in fields}) != digest(
        {key: manifest[key] for key in fields}
    ):
        raise ValueError("history_source_delta_commitment")
    if (
        type(original["chunk_index"]) is not int
        or type(original["chunk_count"]) is not int
        or type(original["chunk_byte_offset"]) is not int
        or type(original["original_byte_count"]) is not int
        or original["chunk_index"] != index
        or original["chunk_count"] != count
        or original["format"] != "json"
        or original["chunk_byte_offset"] != 0
        or original["original_sha256"] != chunk["sha256"]
        or original["original_byte_count"] != size
        or not utc(manifest["start"])
        <= utc(original["start"])
        < utc(original["end"])
        <= utc(manifest["end"])
        or utc(original["acquired_at"]) > utc(manifest["acquired_at"])
        or utc(original["source_end"]) > utc(original["acquired_at"])
    ):
        raise ValueError("history_source_delta_commitment")
    return size


def _source_payload(
    text: str, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    delta = None
    if manifest["format"] == "jsonl":
        values: list[Any] = []
        for line in text.splitlines():
            if line.strip():
                if len(values) >= MAX_SOURCE_ROWS:
                    raise ValueError("history_source_row_limit_or_shape")
                values.append(_parse_json(line))
        rows = _rows(values)
    else:
        value = _parse_json(text)
        if isinstance(value, dict) and value.get("format") == "integration_delta_v1":
            value, delta = _delta_envelope(value, manifest)
        elif isinstance(value, dict) and (
            "status" in value or "schema_version" in value
        ):
            value = _raw_envelope(value, manifest)
        elif isinstance(value, dict) and "data" in value:
            # The successful original provider envelope is also a retained
            # source shape. A declared failure or pagination is never data.
            if value.get("success", True) is not True or value.get("error"):
                raise ValueError("history_source_incomplete_export")
            if any(
                value.get(key)
                for key in ("has_more", "next", "next_page", "next_cursor", "truncated")
            ):
                raise ValueError("history_source_incomplete_export")
            value = value["data"]
        rows = _rows(value)
    return rows, delta


def parse_source(content: bytes, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse bounded original bytes, preserving order and invalid corrections."""
    return _parse_source_document(content, manifest)[0]


def _parse_source_document(
    content: bytes, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(content, bytes) or len(content) > MAX_SOURCE_BYTES:
        raise ValueError("history_source_byte_limit")
    _original_chunk(content, manifest)
    rows, delta = _source_payload(content.decode("utf-8"), manifest)
    first, last = utc(manifest["start"]), utc(manifest["end"])
    source_end = utc(manifest["source_end"])
    identity_field = (
        "thermostat_id" if manifest["resource"] == "runtime_thermostat" else "sensor_id"
    )
    for row in rows:
        if (
            identity_field in row
            and _positive(row[identity_field]) != manifest["resource_id"]
        ):
            raise ValueError("history_source_row_identity")
        if (
            "thermostat_id" in row
            and _positive(row["thermostat_id"]) != manifest["thermostat_id"]
        ):
            raise ValueError("history_source_row_parent")
        stamp = _row_stamp(row)
        if stamp is not None and not first <= stamp <= last:
            raise ValueError("history_source_row_outside_manifest")
        if stamp is not None and stamp > source_end:
            raise ValueError("history_source_row_after_source_end")
    return rows, delta


def _prepare_source(
    content: bytes,
    manifest: dict[str, Any],
    expected_sha256: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    expected = require_digest(expected_sha256)
    if not isinstance(content, bytes) or sha256(content).hexdigest() != expected:
        raise ValueError("history_source_digest_mismatch")
    checked = validate_manifest(manifest, identity)
    rows, delta = _parse_source_document(content, checked)
    return {
        "contract_version": CONTRACT_VERSION,
        "manifest": checked,
        "sha256": expected,
        "byte_count": len(content),
        "row_count": len(rows),
        "confidence": "integration_delta_v1"
        if delta is not None
        else _CONFIDENCE[checked["source_kind"]],
        "delta": delta,
        **_observations(rows),
    }


def _observations(rows: Sequence[dict[str, Any]]) -> dict[str, str | None]:
    stamps = [stamp for row in rows if (stamp := _row_stamp(row)) is not None]
    return {
        "observed_start": min(stamps).isoformat() if stamps else None,
        "observed_end": max(stamps).isoformat() if stamps else None,
    }


def stage_source(
    store: SynchronousSourceStore,
    content: bytes,
    manifest: dict[str, Any],
    expected_sha256: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Executor-only admission, completed before a native upload lease closes."""
    sealed = _prepare_source(content, manifest, expected_sha256, identity)
    if store.write_object("source", content) != expected_sha256:
        raise ValueError("history_source_write_digest_mismatch")
    source_id = store.write_object("manifest", _json_bytes(sealed))
    return {"source_id": source_id, **sealed}


async def async_stage_source(
    store: SourceStore,
    content: bytes,
    manifest: dict[str, Any],
    expected_sha256: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Seal already retained bytes without touching the active journal/marker."""
    sealed = await store.async_process_source_job(
        _prepare_source, content, manifest, expected_sha256, identity
    )
    if await store.async_write_object("source", content) != expected_sha256:
        raise ValueError("history_source_write_digest_mismatch")
    encoded = await store.async_process_source_job(_json_bytes, sealed)
    source_id = await store.async_write_object("manifest", encoded)
    return {"source_id": source_id, **sealed}


async def _load_manifest(
    store: SourceStore, source_id: str, identity: dict[str, Any]
) -> dict[str, Any]:
    raw = await store.async_read_object("manifest", require_digest(source_id))
    return await store.async_process_source_job(
        _parse_manifest, raw, source_id, identity
    )


def _parse_manifest(
    raw: bytes, source_id: str, identity: dict[str, Any]
) -> dict[str, Any]:
    sealed = _parse_json(raw.decode("utf-8"))
    if not isinstance(sealed, dict) or set(sealed) != {
        "contract_version",
        "manifest",
        "sha256",
        "byte_count",
        "row_count",
        "confidence",
        "observed_start",
        "observed_end",
        "delta",
    }:
        raise ValueError("history_source_seal_invalid")
    checked = validate_manifest(sealed["manifest"], identity)
    require_digest(sealed["sha256"])
    if (
        type(sealed["contract_version"]) is not int
        or sealed["contract_version"] != CONTRACT_VERSION
        or type(sealed["byte_count"]) is not int
        or not 0 <= sealed["byte_count"] <= MAX_SOURCE_BYTES
        or type(sealed["row_count"]) is not int
        or not 0 <= sealed["row_count"] <= MAX_SOURCE_ROWS
        or sealed["confidence"]
        != (
            "integration_delta_v1"
            if sealed["delta"] is not None
            else _CONFIDENCE[checked["source_kind"]]
        )
    ):
        raise ValueError("history_source_seal_invalid")
    _validate_observations(sealed)
    if sealed["delta"] is not None:
        _delta_envelope({**sealed["delta"], "rows": []}, checked)
    return {"source_id": source_id, **sealed}


def _validate_observations(source: Mapping[str, Any]) -> None:
    start, end = source["observed_start"], source["observed_end"]
    if start is None and end is None:
        return
    if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        raise ValueError("history_source_observations_invalid")
    manifest = source["manifest"]
    if (
        not utc(manifest["start"])
        <= utc(start)
        <= utc(end)
        <= min(utc(manifest["end"]), utc(manifest["source_end"]))
    ):
        raise ValueError("history_source_observations_invalid")


async def _load_payload(store: SourceStore, source: dict[str, Any]) -> dict[str, Any]:
    content = await store.async_read_object("source", source["sha256"])
    return await store.async_process_source_job(_parse_payload, content, source)


def _parse_payload(content: bytes, source: dict[str, Any]) -> dict[str, Any]:
    rows, delta = _parse_source_document(content, source["manifest"])
    if source["byte_count"] != len(content) or source["row_count"] != len(rows):
        raise ValueError("history_source_seal_invalid")
    if any(source[key] != value for key, value in _observations(rows).items()):
        raise ValueError("history_source_observations_invalid")
    if delta != source["delta"]:
        raise ValueError("history_source_delta_seal_invalid")
    return {**source, "rows": rows}


def _acquisition_key(manifest: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        manifest["resource"],
        manifest["resource_id"],
        manifest["source_kind"],
        manifest["acquisition_id"],
    )


def _validate_chunks(sources: Sequence[dict[str, Any]]) -> None:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        groups[_acquisition_key(source["manifest"])].append(source["manifest"])
    for manifests in groups.values():
        first = manifests[0]
        count = first["chunk_count"]
        if len(manifests) != count or {
            item["chunk_index"] for item in manifests
        } != set(range(count)):
            raise ValueError("history_source_chunks_incomplete")
        # Bounds can differ for ordered provider windows or JSONL archive
        # partitions, but acquisition identity/units/cutoff must be stable.
        fields = (
            "thermostat_id",
            "acquired_at",
            "source_end",
            "chunk_count",
            "unit_contract",
        )
        if any(any(item[key] != first[key] for key in fields) for item in manifests):
            raise ValueError("history_source_chunks_disagree")


async def async_load_source_bundle(
    store: SourceStore,
    source_ids: Sequence[str],
    identity: dict[str, Any],
    *,
    window: tuple[str | datetime, str | datetime] | None = None,
) -> list[dict[str, Any]]:
    """Read complete originals intersecting an optional half-open hour window.

    Every supplied declaration is validated first, including complete acquisition
    metadata. Unrelated original payloads are not read. Any selected original is
    read and hash-proved in full, even when only one of its chunks intersects.
    No retained object or reference is changed by a bounded read.
    """
    requested = bounds(*window) if window is not None else None
    declarations = await async_load_source_manifests(store, source_ids, identity)
    selected = await store.async_process_source_job(
        _select_bundle, declarations, requested
    )
    loaded = [await _load_payload(store, source) for source in selected]
    await _verify_originals(store, loaded)
    return loaded


async def async_load_catalog_source_bundle(
    store: SourceStore,
    source_ids: Sequence[str],
    identity: dict[str, Any],
    *,
    window: tuple[str | datetime, str | datetime],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan retained catalog metadata before applying the selected bundle cap.

    The writer supplies only catalogs/dependencies of its bounded operation;
    public explicit source IDs keep their separate 2,048-item request limit.
    Metadata pages may split an acquisition, so completeness is checked across
    the complete scan, before selected original payloads become available.
    """
    requested = bounds(*window)
    if (
        isinstance(source_ids, str)
        or not source_ids
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("history_source_bundle_ids")
    declarations: list[dict[str, Any]] = []
    for start in range(0, len(source_ids), MAX_SOURCE_CHUNKS):
        page = [
            await _load_manifest(store, source_id, identity)
            for source_id in source_ids[start : start + MAX_SOURCE_CHUNKS]
        ]
        declarations.extend(page)
    await store.async_process_source_job(_validate_chunks, declarations)
    selected = await store.async_process_source_job(
        _select_bundle, declarations, requested
    )
    loaded = [await _load_payload(store, source) for source in selected]
    await _verify_originals(store, loaded)
    return declarations, loaded


async def async_load_source_manifests(
    store: SourceStore, source_ids: Sequence[str], identity: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read all supplied acquisition declarations without original payload I/O."""
    if (
        isinstance(source_ids, str)
        or not 1 <= len(source_ids) <= MAX_SOURCE_CHUNKS
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("history_source_bundle_ids")
    declarations = [
        await _load_manifest(store, source_id, identity) for source_id in source_ids
    ]
    await store.async_process_source_job(_validate_chunks, declarations)
    return declarations


def _select_bundle(
    declarations: list[dict[str, Any]], window: tuple[datetime, datetime] | None
) -> list[dict[str, Any]]:
    selected = _window_sources(declarations, window)
    _bundle_limits(selected)
    return selected


def first_provider_points(sources: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Preserve observed admission boundaries independently of payload windows."""
    result: dict[str, str] = {}
    for source in sources:
        manifest = source["manifest"]
        observed = source["observed_start"]
        if manifest["source_kind"] != "provider" or observed is None:
            continue
        key = f"{manifest['resource']}:{manifest['resource_id']}"
        if key not in result or utc(observed) < utc(result[key]):
            result[key] = utc(observed).isoformat()
    return result


def _bundle_limits(sources: Sequence[dict[str, Any]]) -> None:
    if len(sources) > MAX_SOURCE_CHUNKS:
        raise ValueError("history_source_bundle_limit")
    resources: set[tuple[str, int]] = set()
    total = 0
    for source in sources:
        total += source["byte_count"]
        manifest = source["manifest"]
        resources.add((manifest["resource"], manifest["resource_id"]))
        if total > MAX_BUNDLE_BYTES or len(resources) > MAX_SOURCE_RESOURCES:
            raise ValueError("history_source_bundle_limit")


def _original_key(manifest: Mapping[str, Any]) -> tuple[Any, ...]:
    return (*_acquisition_key(manifest), manifest["original_sha256"])


def _window_sources(
    sources: list[dict[str, Any]], window: tuple[datetime, datetime] | None
) -> list[dict[str, Any]]:
    if window is None:
        return sources
    start, end = window
    selected = {
        _original_key(source["manifest"])
        for source in sources
        if utc(source["manifest"]["start"]) < end
        and utc(source["manifest"]["end"]) >= start
    }
    return [
        source for source in sources if _original_key(source["manifest"]) in selected
    ]


async def _verify_originals(
    store: SourceStore, sources: Sequence[dict[str, Any]]
) -> None:
    """Reproduce each original hash with bounded reads before exposing rows."""
    originals = await store.async_process_source_job(_original_groups, sources)
    for ordered in originals:
        first = ordered[0]["manifest"]
        digest_state = sha256()
        for source in ordered:
            content = await store.async_read_object("source", source["sha256"])
            await store.async_process_source_job(digest_state.update, content)
        if digest_state.hexdigest() != first["original_sha256"]:
            raise ValueError("history_source_original_digest_mismatch")


def _original_groups(sources: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    originals: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        manifest = source["manifest"]
        key = _original_key(manifest)
        originals[key].append(source)
    result = []
    for chunks in originals.values():
        ordered = sorted(chunks, key=lambda item: item["manifest"]["chunk_index"])
        first = ordered[0]["manifest"]
        offset = 0
        initial_index = first["chunk_index"]
        for index, source in enumerate(ordered):
            manifest = source["manifest"]
            if (
                manifest["chunk_index"] != initial_index + index
                or manifest["chunk_byte_offset"] != offset
                or manifest["original_byte_count"] != first["original_byte_count"]
                or manifest["format"] != first["format"]
            ):
                raise ValueError("history_source_original_chunk_sequence")
            offset += source["byte_count"]
        if offset != first["original_byte_count"]:
            raise ValueError("history_source_original_incomplete")
        result.append(ordered)
    return result


def _observation(row: Mapping[str, Any]) -> str:
    # Provider row IDs are provenance, not slot identity. Retain every physical
    # value, including invalid values and deletion flags, in equality checks.
    values = {
        key: value
        for key, value in row.items()
        if key not in {"id", "runtime_sensor_id", "runtime_thermostat_id", "timestamp"}
    }
    return digest(values)


def _hour(stamp: datetime) -> str:
    return stamp.replace(minute=0, second=0, microsecond=0).isoformat()


def _deleted(row: Mapping[str, Any]) -> bool:
    value = row.get("deleted")
    return (
        value.lower() in {"true", "1", "yes", "on"}
        if isinstance(value, str)
        else bool(value)
    )


def _ordered_points(sources: list[dict[str, Any]]) -> dict[str, Any]:
    manifests = [source["manifest"] for source in sources]
    first = manifests[0]
    points: dict[datetime, dict[str, Any]] = {}
    conflicts: set[str] = set()
    blocked: list[dict[str, str]] = []
    for source in sorted(sources, key=lambda item: item["manifest"]["chunk_index"]):
        manifest = source["manifest"]
        for row in source["rows"]:
            stamp = _row_stamp(row)
            if stamp is None:
                window = {
                    "start": manifest["start"],
                    "end": manifest["end"],
                    "reason": "unplaceable_timestamp",
                }
                if window not in blocked:
                    blocked.append(window)
                continue
            if stamp.minute % 5 or stamp.second or stamp.microsecond:
                conflicts.add(_hour(stamp))
                continue
            previous = points.get(stamp)
            same = previous is not None and _observation(
                previous["row"]
            ) == _observation(row)
            if previous is not None and not same and first["source_kind"] == "archive":
                conflicts.add(_hour(stamp))
                continue
            source_ids = (
                sorted(set(previous["source_ids"] + [source["source_id"]]))
                if same and previous is not None
                else [source["source_id"]]
            )
            confidence = (
                sorted(set(previous["confidence"] + [source["confidence"]]))
                if same and previous is not None
                else [source["confidence"]]
            )
            points[stamp] = {
                "row": row,
                "source_ids": source_ids,
                "confidence": confidence,
            }
    return {
        "manifest": first,
        "points": points,
        "conflict_hours": conflicts,
        "blocked_windows": blocked,
    }


def _resolve_slot(
    candidates: list[dict[str, Any]],
    *,
    archive_policy: bool,
    first_provider: datetime | None,
    stamp: datetime,
    provider_predecessors: Mapping[str, set[str]],
) -> tuple[dict[str, Any] | None, bool]:
    provider = [item for item in candidates if item["kind"] == "provider"]
    archives = [item for item in candidates if item["kind"] == "archive"]
    if provider:
        chosen = _provider_winners(provider, provider_predecessors)
        if not chosen:
            return None, True
        current = chosen[0]
        # An explicit authoritative deletion/invalid correction is preserved;
        # an older archive must never fill its slot by falling back.
        if not _deleted(current["row"]) and any(
            _observation(item["row"]) != _observation(current["row"])
            for item in archives
        ):
            return None, True
        equal = [
            item
            for item in candidates
            if _observation(item["row"]) == _observation(current["row"])
        ]
        return {
            **current,
            "source_ids": sorted(
                {source_id for item in equal for source_id in item["source_ids"]}
            ),
            "confidence": sorted(
                {
                    qualification
                    for item in equal
                    for qualification in item["confidence"]
                }
            ),
        }, False
    if not archive_policy or first_provider is None or stamp >= first_provider:
        return None, False
    if len({_observation(item["row"]) for item in archives}) != 1:
        return None, True
    current = archives[0]
    return {
        **current,
        "source_ids": sorted(
            {source_id for item in archives for source_id in item["source_ids"]}
        ),
        "confidence": ["archive_qualified"],
    }, False


def _provider_winners(
    candidates: list[dict[str, Any]], predecessors: Mapping[str, set[str]]
) -> list[dict[str, Any]]:
    """A present correction must dominate every differing present observation."""
    observations = {
        item["acquisition_id"]: _observation(item["row"]) for item in candidates
    }
    return [
        item
        for item in candidates
        if all(
            observation == observations[item["acquisition_id"]]
            or older in predecessors.get(item["acquisition_id"], set())
            for older, observation in observations.items()
        )
    ]


def merge_source_bundle(
    sources: Sequence[dict[str, Any]],
    *,
    archive_policy: bool = False,
    provider_order: Mapping[str, Sequence[str]] | None = None,
    provider_supersedes: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    first_provider: Mapping[str, str | datetime] | None = None,
) -> dict[str, Any]:
    """Resolve admitted observations without inventing precedence or deletion.

    Conflicting or unplaceable evidence is returned as explicit suppression
    metadata. The planner must apply it before producing verified hour rows.
    Empty provider acquisitions contribute evidence, not deletion tombstones.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        groups[_acquisition_key(source["manifest"])].append(source)
    resources: dict[str, Any] = {}
    acquisitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in groups.values():
        item = _ordered_points(group)
        manifest = item["manifest"]
        key = f"{manifest['resource']}:{manifest['resource_id']}"
        acquisitions[key].append(item)
    orders = _provider_orders(provider_order)
    supersedes = _provider_supersessions(provider_supersedes)
    roots = {
        resource: {
            item["manifest"]["acquisition_id"]
            for item in items
            if item["manifest"]["source_kind"] == "provider"
        }
        for resource, items in acquisitions.items()
    }
    precedence = _provider_precedence(orders, supersedes, roots)
    provider_starts = {
        key: utc(stamp).isoformat() for key, stamp in (first_provider or {}).items()
    }
    for key, items in acquisitions.items():
        first = items[0]["manifest"]
        slots: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        conflicts: set[str] = set()
        blocked: list[dict[str, str]] = []
        for item in items:
            manifest = item["manifest"]
            conflicts.update(item["conflict_hours"])
            blocked.extend(item["blocked_windows"])
            for stamp, point in item["points"].items():
                slots[stamp].append(
                    {
                        **point,
                        "kind": manifest["source_kind"],
                        "acquisition_id": manifest["acquisition_id"],
                    }
                )
        provider_stamps = [
            stamp
            for stamp, points in slots.items()
            if any(point["kind"] == "provider" for point in points)
        ]
        if key in provider_starts:
            provider_stamps.append(utc(provider_starts[key]))
        first_point = min(provider_stamps) if provider_stamps else None
        if first_point is not None:
            provider_starts[key] = first_point.isoformat()
        rows: list[dict[str, Any]] = []
        provenance: dict[str, Any] = {}
        for stamp, candidates in sorted(slots.items()):
            selected, conflict = _resolve_slot(
                candidates,
                archive_policy=archive_policy,
                first_provider=first_point,
                stamp=stamp,
                provider_predecessors=precedence.get(key, {}),
            )
            if conflict:
                conflicts.add(_hour(stamp))
            if selected is not None:
                rows.append(deepcopy(selected["row"]))
                provenance[stamp.isoformat()] = {
                    "source_ids": selected["source_ids"],
                    "confidence": selected["confidence"],
                    "basis": selected["kind"],
                }
        resources[key] = {
            "resource": first["resource"],
            "resource_id": first["resource_id"],
            "thermostat_id": first["thermostat_id"],
            "rows": rows,
            "slots": provenance,
            "conflict_hours": sorted(conflicts),
            "blocked_windows": blocked,
            "first_provider": first_point.isoformat()
            if first_point is not None
            else None,
        }
    source_ids = sorted(source["source_id"] for source in sources)
    return {
        "resources": resources,
        "source_ids": source_ids,
        "first_provider": provider_starts,
        "source_revision": digest(
            {
                "source_ids": source_ids,
                "archive_policy": archive_policy,
                "provider_order": orders,
                "provider_supersedes": supersedes,
                "first_provider": provider_starts,
            }
        ),
    }


def _provider_orders(
    order: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    """Validate an explicit reviewed correction chain; timestamps are no grant."""
    if order is None:
        return {}
    checked: dict[str, list[str]] = {}
    for resource, names in order.items():
        if re.fullmatch(
            r"runtime_(?:thermostat|sensor):[1-9][0-9]*", resource
        ) is None or isinstance(names, str):
            raise ValueError("history_source_provider_order_invalid")
        if (
            not names
            or any(not isinstance(name, str) or not name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError("history_source_provider_order_invalid")
        checked[resource] = list(names)
    return checked


def _provider_supersessions(
    relation: Mapping[str, Mapping[str, Sequence[str]]] | None,
) -> dict[str, dict[str, list[str]]]:
    """Bind exact predecessor declarations without creating a total ordering."""
    checked: dict[str, dict[str, list[str]]] = {}
    for resource, acquisitions in (relation or {}).items():
        if re.fullmatch(r"runtime_(?:thermostat|sensor):[1-9][0-9]*", resource) is None:
            raise ValueError("history_source_provider_supersedes_invalid")
        normalized: dict[str, list[str]] = {}
        for newer, older in acquisitions.items():
            if (
                not isinstance(newer, str)
                or not newer
                or isinstance(older, str)
                or any(not isinstance(name, str) or not name for name in older)
                or len(older) != len(set(older))
                or newer in older
            ):
                raise ValueError("history_source_provider_supersedes_invalid")
            normalized[newer] = sorted(older)
        checked[resource] = normalized
    return checked


def _provider_precedence(
    orders: Mapping[str, list[str]],
    supersedes: Mapping[str, dict[str, list[str]]],
    roots: Mapping[str, set[str]],
) -> dict[str, dict[str, set[str]]]:
    """Combine explicit order/relations, reject cycles and retain their closure."""
    result: dict[str, dict[str, set[str]]] = {}
    for resource in orders.keys() | supersedes.keys():
        edges = {
            newer: set(older) for newer, older in supersedes.get(resource, {}).items()
        }
        chain = orders.get(resource, [])
        for older, newer in pairwise(chain):
            edges.setdefault(newer, set()).add(older)
        result[resource] = _predecessor_closure(edges, roots.get(resource, set()))
    return result


def _predecessor_closure(
    edges: Mapping[str, set[str]], roots: set[str]
) -> dict[str, set[str]]:
    """Validate all edges but expand ancestry only for loaded acquisitions."""
    try:
        tuple(TopologicalSorter(edges).static_order())
    except CycleError as err:
        raise ValueError("history_source_provider_supersedes_cycle") from err
    resolved: dict[str, set[str]] = {}
    for node in roots:
        ancestors: set[str] = set()
        pending = list(edges.get(node, ()))
        while pending:
            older = pending.pop()
            if older not in ancestors:
                ancestors.add(older)
                pending.extend(edges.get(older, ()))
        resolved[node] = ancestors
    return resolved
