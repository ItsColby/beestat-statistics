"""Bounded, reviewed quantity corrections over immutable source evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .hourly_history_contract import (
    canonical_json,
    digest,
    quantity_id,
    require_digest,
    utc,
)
from .hourly_history_values import HistorySeries, build_history_series
from .hourly_sources import _observation, _row_stamp
from .hourly_statistics import build_hourly_statistics

_HOUR = timedelta(hours=1)
_STEP = timedelta(minutes=5)


def _resource(descriptor: dict[str, Any]) -> str:
    sensor = descriptor["sensor_id"]
    return (
        f"runtime_sensor:{sensor}"
        if sensor is not None
        else f"runtime_thermostat:{descriptor['thermostat_id']}"
    )


def _window(start: str | datetime, end: str | datetime) -> tuple[datetime, datetime]:
    first, stop = utc(start, whole_hour=True), utc(end, whole_hour=True)
    if not first < stop <= first + timedelta(hours=24):
        raise ValueError("history_correction_dependency_invalid")
    return first, stop


def raw_witness(
    merged: dict[str, Any],
    descriptor: dict[str, Any],
    start: str | datetime,
    end: str | datetime,
) -> str:
    """Bind every grid slot, including absence and complete observation content."""
    first, stop = _window(start, end)
    resource = merged.get("resources", {}).get(_resource(descriptor), {})
    rows = {_row_stamp(row): _observation(row) for row in resource.get("rows", [])}
    slots = {}
    while first < stop:
        slots[first.isoformat()] = rows.get(first)
        first += _STEP
    return digest(slots)


def resolution_witness(
    merged: dict[str, Any],
    sources: list[dict[str, Any]],
    descriptor: dict[str, Any],
    start: str | datetime,
    end: str | datetime,
) -> str:
    """Bind distinct competing evidence and resolution, independent of row IDs."""
    first, stop = _window(start, end)
    key = _resource(descriptor)
    resource = merged.get("resources", {}).get(key, {})
    candidates = set()
    for source in sources:
        manifest = source["manifest"]
        if f"{manifest['resource']}:{manifest['resource_id']}" != key:
            continue
        for row in source["rows"]:
            stamp = _row_stamp(row)
            if stamp is not None and first <= stamp < stop:
                candidates.add(
                    (stamp.isoformat(), _observation(row), manifest["source_kind"])
                )
    return digest(
        {
            "candidates": sorted(candidates),
            "conflict_hours": sorted(
                x for x in resource.get("conflict_hours", []) if first <= utc(x) < stop
            ),
            "blocked_windows": sorted(
                (w["start"], w["end"], w["reason"])
                for w in resource.get("blocked_windows", [])
                if utc(w["start"]) < stop and utc(w["end"]) >= first
            ),
            "basis": sorted(
                (stamp, slot["basis"])
                for stamp, slot in resource.get("slots", {}).items()
                if first <= utc(stamp) < stop
            ),
            "first_provider": resource.get("first_provider"),
        }
    )


def _evidence(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"sha256", "locator"}:
        raise ValueError("history_correction_evidence_invalid")
    sha = require_digest(value["sha256"])
    locator = value["locator"]
    if not isinstance(locator, str) or not 1 <= len(locator) <= 1024:
        raise ValueError("history_correction_evidence_invalid")
    return sha, locator


def validate_document(
    document: dict[str, Any],
    binding: dict[str, Any],
    descriptors: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate the complete sealed contract against current identity and methods."""
    if (
        not isinstance(document, dict)
        or set(document)
        != {"format", "binding", "converter_version", "originals", "entries"}
        or document["format"] != "reviewed_quantity_correction_v1"
        or document["binding"] != binding
        or len(canonical_json(document)) > 8 * 1024 * 1024
    ):
        raise ValueError("history_correction_document_invalid")
    version = document["converter_version"]
    if not isinstance(version, str) or not 1 <= len(version) <= 128:
        raise ValueError("history_correction_converter_invalid")
    originals = document["originals"]
    if not isinstance(originals, list) or not 1 <= len(originals) <= 128:
        raise ValueError("history_correction_evidence_invalid")
    refs = [_evidence(item) for item in originals]
    if len(set(refs)) != len(refs):
        raise ValueError("history_correction_evidence_invalid")
    entries = document["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise ValueError("history_correction_entries_invalid")
    seen: set[tuple[str, datetime]] = set()
    for entry in entries:
        _validate_entry(entry, descriptors, set(refs), seen, binding.get("timezone"))
    return deepcopy(document)


def _validate_entry(
    entry: Any,
    descriptors: dict[str, dict[str, Any]],
    refs: set[tuple[str, str]],
    seen: set[tuple[str, datetime]],
    timezone: Any,
) -> None:
    fields = {
        "descriptor",
        "start",
        "action",
        "dependency_start",
        "dependency_end",
        "raw_witness",
        "resolution_witness",
        "qualification",
        "points",
        "predecessor",
        "reason",
        "interpretation",
    }
    if (
        not isinstance(entry, dict)
        or set(entry) != fields
        or not isinstance(entry["descriptor"], dict)
    ):
        raise ValueError("history_correction_entry_invalid")
    descriptor = entry["descriptor"]
    qid = quantity_id(descriptor)
    stable = (
        "quantity_id",
        "thermostat_id",
        "sensor_id",
        "quantity",
        "kind",
        "logical_unit",
        "method_version",
        "statistic_id",
        "representation",
    )
    current = descriptors.get(qid, {})
    if descriptor.get("quantity_id") != qid or any(
        descriptor.get(key) != current.get(key) for key in stable
    ):
        raise ValueError("history_correction_descriptor_changed")
    start = utc(entry["start"], whole_hour=True)
    if not isinstance(entry["start"], str) or entry["start"] != start.isoformat():
        raise ValueError("history_correction_entry_invalid")
    first, stop = _window(entry["dependency_start"], entry["dependency_end"])
    if not first <= start or start + _HOUR > stop or (qid, start) in seen:
        raise ValueError("history_correction_entry_invalid")
    seen.add((qid, start))
    for field in ("raw_witness", "resolution_witness"):
        require_digest(entry[field])
    if entry["predecessor"] is not None:
        require_digest(entry["predecessor"])
    evidence = entry["qualification"]
    if (
        not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 128
        or any(_evidence(item) not in refs for item in evidence)
    ):
        raise ValueError("history_correction_evidence_invalid")
    _validate_interpretation(entry["interpretation"], timezone)
    _validate_points_shape(entry, descriptor, start)


def _validate_interpretation(value: Any, timezone: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "timezone",
        "fold",
        "unit_conversion",
    }:
        raise ValueError("history_correction_interpretation_invalid")
    fold, conversion = value["fold"], value["unit_conversion"]
    if (
        not isinstance(timezone, str)
        or not timezone
        or value["timezone"] != timezone
        or (fold is not None and (type(fold) is not int or fold not in (0, 1)))
        or not isinstance(conversion, str)
        or not 1 <= len(conversion) <= 1024
    ):
        raise ValueError("history_correction_interpretation_invalid")


def _validate_points_shape(
    entry: dict[str, Any], descriptor: dict[str, Any], start: datetime
) -> None:
    action, points = entry["action"], entry["points"]
    if action not in {"replace", "hold", "withdraw"} or not isinstance(points, list):
        raise ValueError("history_correction_entry_invalid")
    reason = entry["reason"]
    if (
        action == "hold"
        and (not isinstance(reason, str) or not 1 <= len(reason) <= 1024)
    ) or (action != "hold" and reason is not None):
        raise ValueError("history_correction_entry_invalid")
    if action != "replace":
        if points:
            raise ValueError("history_correction_points_invalid")
        return
    if len(points) != 12 or any(not isinstance(p, dict) for p in points):
        raise ValueError("history_correction_points_invalid")
    field = "sensor_id" if descriptor["sensor_id"] is not None else "thermostat_id"
    expected = descriptor[field]
    for index, point in enumerate(points):
        if (
            _row_stamp(point) != start + index * _STEP
            or type(point.get(field)) is not int
            or point[field] != expected
        ):
            raise ValueError("history_correction_points_invalid")


def _replacement(entry: dict[str, Any], context: dict[str, Any]) -> HistorySeries:
    descriptor = entry["descriptor"]
    start = utc(entry["start"], whole_hour=True)
    sensor = descriptor["sensor_id"]
    thermostat = descriptor["thermostat_id"]
    rows = entry["points"]
    identity = context["identity"]
    v2 = descriptor["representation"]["v2_statistic_id"]
    built = build_hourly_statistics(
        {thermostat: rows} if sensor is None else {},
        {sensor: rows} if sensor is not None else {},
        context["config"],
        start=start,
        end=start + _HOUR,
        evaluated_at=utc(context["evaluated_at"]),
        source_end_by_thermostat={thermostat: start + _HOUR},
        existing_statistic_ids=(v2.removesuffix("_hourly_v2"),),
    )
    selected = tuple(item for item in built if item.statistic_id == v2)
    represented = build_history_series(
        selected, identity, saved={descriptor["quantity_id"]: descriptor}
    )
    if (
        len(represented) != 1
        or len(represented[0].hours) != 1
        or represented[0].hours[0].reason != "ready"
    ):
        raise ValueError("history_correction_points_unqualified")
    return represented[0]


def validate_points(document: dict[str, Any], context: dict[str, Any]) -> None:
    """Reject replacements that the ordinary sample builder cannot qualify."""
    for entry in document["entries"]:
        if entry["action"] == "replace":
            _replacement(entry, context)


def project(
    built: dict[str, HistorySeries],
    merged: dict[str, Any],
    sources: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    context: dict[str, Any],
) -> tuple[dict[str, HistorySeries], dict[str, Any]]:
    """Project only reviewed quantities while preserving the resolved raw input."""
    output = dict(built)
    resources = {
        qid: deepcopy(merged.get("resources", {}).get(_resource(item.descriptor), {}))
        for qid, item in built.items()
    }
    seen: set[tuple[str, datetime]] = set()
    for wrapper in documents:
        correction_id = require_digest(wrapper["correction_id"])
        document = wrapper["document"]
        if digest(document) != correction_id:
            raise ValueError("history_correction_digest_changed")
        indices = wrapper.get("entry_indices", list(range(len(document["entries"]))))
        if (
            not isinstance(indices, list)
            or len(set(indices)) != len(indices)
            or any(
                type(index) is not int or not 0 <= index < len(document["entries"])
                for index in indices
            )
        ):
            raise ValueError("history_correction_entries_invalid")
        for index in indices:
            entry = document["entries"][index]
            descriptor = entry["descriptor"]
            qid = descriptor["quantity_id"]
            start = utc(entry["start"], whole_hour=True)
            if (qid, start) in seen or qid not in built:
                raise ValueError("history_correction_overlap")
            seen.add((qid, start))
            if entry["action"] == "withdraw":
                continue
            first, stop = entry["dependency_start"], entry["dependency_end"]
            stale = (
                raw_witness(merged, descriptor, first, stop) != entry["raw_witness"]
                or resolution_witness(merged, sources, descriptor, first, stop)
                != entry["resolution_witness"]
            )
            candidate = (
                _replacement(entry, context).hours[0]
                if not stale and entry["action"] == "replace"
                else None
            )
            item = output[qid]
            if not any(hour.start == start for hour in item.hours):
                continue
            hours = tuple(
                (
                    candidate
                    if candidate is not None
                    else replace(
                        hour,
                        values=None,
                        valid_slots=0,
                        missing_slots=0,
                        invalid_slots=12,
                        duplicate_slots=0,
                        reason="source_timestamp_unplaced",
                    )
                )
                if hour.start == start
                else hour
                for hour in item.hours
            )
            output[qid] = replace(item, hours=hours)
            resource = resources[qid]
            resource["conflict_hours"] = [
                x for x in resource.get("conflict_hours", []) if utc(x) != start
            ]
            # Split intersecting windows around this hour so sibling hours remain blocked.
            windows = []
            for window in resource.get("blocked_windows", []):
                a, b = utc(window["start"]), utc(window["end"])
                if a < start and b >= start:
                    windows.append(
                        {
                            **window,
                            "end": (start - timedelta(microseconds=1)).isoformat(),
                        }
                    )
                if a < start + _HOUR <= b:
                    windows.append({**window, "start": (start + _HOUR).isoformat()})
                if b < start or a >= start + _HOUR:
                    windows.append(window)
            resource["blocked_windows"] = windows
            resource.setdefault("correction_ids", {})[start.isoformat()] = correction_id
    return output, resources
