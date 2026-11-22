"""Root contract of independent-hour history: validation, status and partition.

A durable root is adopted only after ``validate_root`` accepts it; ``status``
and ``partition`` read an adopted root. Nothing here reads or writes the Store
or Recorder.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from .const import HOURLY_V2_SUFFIX
from .hourly_correction_store import validate_index
from .hourly_history_contract import (
    MAX_BATCH_HOURS,
    METHOD_VERSION,
    bounds,
    digest,
    quantity_id,
    require_digest,
    unmapped_sensor,
)
from .hourly_import_core import (
    MARKER,
    HourlyImportError,
    WriterPartition,
    _native,
    _partition_resources,
)

_HOUR = timedelta(hours=1)

_ROOT_VERSIONS = (3, 4)


def sealed(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    result.pop("integrity", None)
    return {**result, "integrity": digest(result)}


def validate_root(manager: Any, state: dict[str, Any]) -> None:
    """Validate references and the sole intent before any root can be adopted."""
    try:
        body = {key: value for key, value in state.items() if key != "integrity"}
        if (
            state["integrity"] != digest(body)
            or state["version"] not in _ROOT_VERSIONS
            or state["entry_id"] != manager._entry.entry_id
            or type(state["revision"]) is not int
            or state["revision"] < 0
            or state["phase"] not in {"prepared", "hourly"}
        ):
            raise ValueError("history_root_header_invalid")
        require_digest(state["token"])
        identity = state["identity"]
        if (
            identity["entry_id"] != state["entry_id"]
            or not identity["api_base"]
            or not identity["account_anchors"]
        ):
            raise ValueError("history_identity_invalid")
        _validate_history(state["history"])
        if state["version"] == 3 and "corrections" in state["history"]:
            raise ValueError("history_correction_requires_v4")
        if state["version"] == 4:
            validate_index(state["history"]["corrections"])
            if set(state["history"]["corrections"]) - set(
                state["history"]["selections"]
            ):
                raise ValueError("history_correction_owner_missing")
        if state.get("legacy_v2") is not None:
            raise ValueError("history_v2_retired")
        _validate_operation(state["operation"])
        _validate_history_intent(state)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise HourlyImportError("history_root_missing_corrupt_or_unsupported") from err


def _validate_history(history: dict[str, Any]) -> None:
    native_ids: set[str] = set()
    for key, selected in history["selections"].items():
        descriptor = selected["descriptor"]
        if key != quantity_id(descriptor) or key != descriptor["quantity_id"]:
            raise ValueError("history_selection_invalid")
        if (
            descriptor["statistic_id"] in native_ids
            or descriptor["method_version"] != METHOD_VERSION
        ):
            raise ValueError("history_selection_collision")
        if (
            selected["metadata"]["statistic_id"] != descriptor["statistic_id"]
            or selected["metadata"]["has_sum"] is not False
        ):
            raise ValueError("history_metadata_invalid")
        native_ids.add(descriptor["statistic_id"])
        for reference in selected["proofs"].values():
            require_digest(reference)
    for reference in (
        *history["source_ids"],
        *history.get("source_catalog", {}).values(),
        *history.get("invalidation_fences", ()),
    ):
        require_digest(reference)
    for months in history.get("source_holds", {}).values():
        for reference in months.values():
            require_digest(reference)
    if (
        type(history["coverage_revision"]) is not int
        or history["coverage_revision"] < 0
    ):
        raise ValueError("history_coverage_revision_invalid")


def _validate_operation(operation: dict[str, Any]) -> None:
    require_digest(operation["object"])
    require_digest(operation["plan_digest"])
    if operation["status"] not in {
        "accepted",
        "in_progress",
        "completed",
        "blocked",
    }:
        raise ValueError("history_operation_status_invalid")
    if (
        type(operation["cursor"]) is not int
        or type(operation["batch_count"]) is not int
        or not 0 <= operation["cursor"] <= operation["batch_count"]
    ):
        raise ValueError("history_operation_cursor_invalid")


def _validate_history_intent(state: dict[str, Any]) -> None:
    operation = state["operation"]
    pending = state.get("pending")
    if pending and pending.get("generation") == 3:
        body = {key: value for key, value in pending.items() if key != "digest"}
        if (
            pending["digest"] != digest(body)
            or pending["operation_id"] != operation["operation_id"]
            or pending["index"] != operation["cursor"]
        ):
            raise ValueError("history_pending_invalid")
        require_digest(pending["batch_object"])
        # The intent names its sealed, hash-verified batch object and range.
        # Intents saved by earlier releases embed the whole batch instead.
        batch = pending.get("batch", pending)
        first, end = bounds(batch["start"], batch["end"])
        if (end - first) / _HOUR > MAX_BATCH_HOURS or not isinstance(
            batch["quantity_id"], str
        ):
            raise ValueError("history_pending_bounds_invalid")
        if "batch" in pending:
            if (
                len(batch["before"]) > MAX_BATCH_HOURS
                or len(batch["rows"]) > MAX_BATCH_HOURS
            ):
                raise ValueError("history_pending_bounds_invalid")
            for collection in (batch["before"], batch["rows"]):
                stamps = [_native(row).start for row in collection]
                if stamps != sorted(set(stamps)) or any(
                    not first <= stamp < end for stamp in stamps
                ):
                    raise ValueError("history_pending_rows_invalid")
            if digest(batch) != pending["batch_object"]:
                raise ValueError("history_pending_object_invalid")
    elif pending:
        raise ValueError("history_pending_generation_invalid")


def status(state: dict[str, Any] | None) -> dict[str, Any]:
    if state is None or state.get("version") not in _ROOT_VERSIONS:
        return {"status": "unselected", "root_revision": 0, "has_pending": False}
    operation = state["operation"]
    return {
        "contract_version": 3,
        "status": operation["status"],
        "operation_id": operation["operation_id"],
        "plan_digest": operation["plan_digest"],
        "cursor": operation["cursor"],
        "batch_count": operation["batch_count"],
        "verified_batches": operation["cursor"],
        "remaining_batches": operation["batch_count"] - operation["cursor"],
        "root_revision": state["revision"],
        "error": operation.get("error"),
        "has_pending": state["phase"] == "prepared"
        or operation["status"] in {"accepted", "in_progress"},
    }


def partition(manager: Any, identity: dict[str, Any]) -> WriterPartition:
    state = manager._state
    if state is None or state.get("version") not in _ROOT_VERSIONS:
        raise HourlyImportError("history_partition_requires_v3")
    _identity_matches(state["identity"], identity)
    marker = manager._entry.data.get(MARKER)
    if marker != {"version": state["version"], "token": state["token"]} and not (
        state["phase"] == "prepared" and marker == state.get("previous_marker")
    ):
        raise HourlyImportError("history_marker_mismatch")
    records = list(state["history"]["selections"].values())
    physical = {quantity_id(item["descriptor"]) for item in records}
    alias_owners = {
        alias: quantity_id(item["descriptor"])
        for item in records
        for alias in item["descriptor"]["legacy_statistic_ids"]
    }
    for base, resource in identity["resources"].items():
        alias_owner = alias_owners.get(base.removesuffix(HOURLY_V2_SUFFIX))
        if alias_owner is not None and (
            unmapped_sensor(resource) or alias_owner != quantity_id(resource)
        ):
            raise HourlyImportError("history_legacy_alias_rebound")
    _current, parents = _partition_resources(identity)
    for item in records:
        descriptor = item["descriptor"]
        sensor_id = descriptor["sensor_id"]
        if sensor_id in parents and parents[sensor_id] != descriptor["thermostat_id"]:
            raise HourlyImportError("history_sensor_parent_changed")
    return WriterPartition(
        frozenset(
            base.removesuffix(HOURLY_V2_SUFFIX)
            for base in identity["resources"]
            if unmapped_sensor(identity["resources"][base])
            or quantity_id(identity["resources"][base]) not in physical
        ),
        True,
        "history_operation_pending"
        if state.get("pending") or state["operation"]["status"] != "completed"
        else None,
    )


def _identity_matches(saved: dict[str, Any], current: dict[str, Any]) -> None:
    if (
        saved.get("entry_id") != current.get("entry_id")
        or saved.get("api_base") != current.get("api_base")
        or not set(saved.get("account_anchors", ()))
        & set(current.get("account_anchors", ()))
    ):
        raise HourlyImportError("history_identity_changed")
