"""Independent-hour history through the existing manager's one durable journal.

This helper has no scheduler, provider client, lock or mutable owner of its own.
The entry importer serializes calls; all root commits use HourlyImportManager._save.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Any, cast

from .hourly_history_contract import (
    DAILY_POLICY,
    MAX_BATCH_HOURS,
    MAX_QUANTITIES,
    MAX_SOURCE_CHUNKS,
    METHOD_VERSION,
    bounds,
    capabilities,
    digest,
    quantity_id,
    require_digest,
    utc,
)
from .hourly_history_legacy import async_read_legacy_days
from .hourly_history_values import build_history_series
from .hourly_import import (
    MARKER,
    HourlyImportError,
    HourlyReconciliationError,
    WriterPartition,
    _metadata,
    _native,
    _partition_resources,
    _row,
    _validated_records,
)
from .hourly_sources import (
    _row_stamp,
    async_load_catalog_source_bundle,
    async_load_source_bundle,
    first_provider_points,
    merge_source_bundle,
)
from .hourly_statistics import build_hourly_statistics

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_FIELDS = ("entry_id", "api_base", "account_anchors")


def encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sealed(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    result.pop("integrity", None)
    return {**result, "integrity": digest(result)}


def legacy_document(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project the old document while retaining exactly one root pending slot."""
    if state is None or state.get("version") != 3:
        return state
    old = deepcopy(state.get("legacy_v2"))
    if old is not None:
        old["revision"] = state["revision"]
        pending = state.get("pending")
        old["pending"] = (
            pending["intent"] if pending and pending.get("generation") == 2 else None
        )
        old = sealed(old)
    return old


def wrap_legacy_save(root: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    if root.get("operation", {}).get("status") in {
        "accepted",
        "in_progress",
        "blocked",
    }:
        raise HourlyImportError("history_operation_owns_writer")
    result = deepcopy(root)
    legacy = deepcopy(old)
    pending = legacy.get("pending")
    legacy["pending"] = None
    result["legacy_v2"] = sealed(legacy)
    result["pending"] = {"generation": 2, "intent": pending} if pending else None
    result["revision"] += 1
    return result


def validate_root(manager: Any, state: dict[str, Any]) -> None:
    """Validate references and the sole intent before any root can be adopted."""
    try:
        body = {key: value for key, value in state.items() if key != "integrity"}
        if (
            state["integrity"] != digest(body)
            or state["version"] != 3
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
        physical = _validate_history(state["history"])
        old = legacy_document(state)
        if old:
            manager._validate(old)
            if any(
                quantity_id(record["resource"]) in physical
                for record in _validated_records(old).values()
            ):
                raise ValueError("history_physical_owner_collision")
        _validate_operation(state["operation"])
        _validate_history_intent(state)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise HourlyImportError("history_root_missing_corrupt_or_unsupported") from err


def _validate_history(history: dict[str, Any]) -> set[str]:
    native_ids: set[str] = set()
    physical: set[str] = set()
    for key, selected in history["selections"].items():
        descriptor = selected["descriptor"]
        if (
            key != quantity_id(descriptor)
            or key != descriptor["quantity_id"]
            or descriptor["quantity"] == "voc_concentration"
        ):
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
        physical.add(key)
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
    return physical


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
        batch = pending["batch"]
        first, end = bounds(batch["start"], batch["end"])
        if (
            (end - first) / _HOUR > MAX_BATCH_HOURS
            or len(batch["before"]) > MAX_BATCH_HOURS
            or len(batch["rows"]) > MAX_BATCH_HOURS
        ):
            raise ValueError("history_pending_bounds_invalid")
        for collection in (batch["before"], batch["rows"]):
            stamps = [_native(row).start for row in collection]
            if stamps != sorted(set(stamps)) or any(
                not first <= stamp < end for stamp in stamps
            ):
                raise ValueError("history_pending_rows_invalid")
        if sha256(encoded(batch)).hexdigest() != pending["batch_object"]:
            raise ValueError("history_pending_object_invalid")
    elif pending and pending.get("generation") != 2:
        raise ValueError("history_pending_generation_invalid")


def status(state: dict[str, Any] | None) -> dict[str, Any]:
    if state is None or state.get("version") != 3:
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
    if state is None or state.get("version") != 3:
        raise HourlyImportError("history_partition_requires_v3")
    _identity_matches(state["identity"], identity)
    marker = manager._entry.data.get(MARKER)
    if marker != {"version": 3, "token": state["token"]} and not (
        state["phase"] == "prepared" and marker == state.get("previous_marker")
    ):
        raise HourlyImportError("history_marker_mismatch")
    records = list(state["history"]["selections"].values())
    frozen = {
        alias
        for item in records
        for alias in item["descriptor"]["legacy_statistic_ids"]
    }
    physical = {quantity_id(item["descriptor"]) for item in records}
    alias_owners = {
        alias: quantity_id(item["descriptor"])
        for item in records
        for alias in item["descriptor"]["legacy_statistic_ids"]
    }
    v2_physical: set[str] = set()
    old = legacy_document(state)
    if old:
        for base, record in _validated_records(old).items():
            physical.add(quantity_id(record["resource"]))
            v2_physical.add(quantity_id(record["resource"]))
            frozen.add(base.removesuffix("_hourly_v2"))
            alias_owners[base.removesuffix("_hourly_v2")] = quantity_id(
                record["resource"]
            )
    selected: set[str] = set()
    for base, resource in identity["resources"].items():
        alias_owner = alias_owners.get(base.removesuffix("_hourly_v2"))
        if alias_owner is not None and alias_owner != quantity_id(resource):
            raise HourlyImportError("history_legacy_alias_rebound")
        if quantity_id(resource) in physical:
            frozen.add(base.removesuffix("_hourly_v2"))
        if quantity_id(resource) in v2_physical:
            selected.add(base)
    _current, parents = _partition_resources(identity)
    for item in records:
        descriptor = item["descriptor"]
        sensor_id = descriptor["sensor_id"]
        if sensor_id in parents and parents[sensor_id] != descriptor["thermostat_id"]:
            raise HourlyImportError("history_sensor_parent_changed")
    return WriterPartition(
        frozenset(
            base.removesuffix("_hourly_v2")
            for base in identity["resources"]
            if quantity_id(identity["resources"][base]) not in physical
        ),
        frozenset(selected),
        frozenset(frozen),
        True,
        "history_operation_pending"
        if state.get("pending")
        or state["operation"]["status"] != "completed"
        or (old or {}).get("pending_selection")
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


def _validate_consumers(consumer: dict[str, Any]) -> None:
    if (
        not isinstance(consumer, dict)
        or consumer.get("contract_version") != 3
        or not isinstance(consumer.get("consumers"), list)
        or not 1 <= len(consumer["consumers"]) <= 16
    ):
        raise HourlyImportError("history_consumer_contract_invalid")
    seen: set[str] = set()
    for item in consumer["consumers"]:
        if (
            item.get("history_contract") != 3
            or item.get("daily_policy") != DAILY_POLICY
            or not all(
                isinstance(item.get(key), str) and 0 < len(item[key]) <= 256
                for key in ("consumer_id", "version")
            )
            or item["consumer_id"] in seen
        ):
            raise HourlyImportError("history_consumer_contract_invalid")
        seen.add(item["consumer_id"])


def _validate_owned_rows(before: dict[str, Any], proof_hours: dict[str, Any]) -> None:
    for stamp, row in before.items():
        if (
            row
            and any(
                row.get(field) is not None
                for field in ("mean", "min", "max", "state", "sum")
            )
            and not proof_hours.get(stamp, {}).get("row")
        ):
            raise HourlyImportError("history_unowned_native_row")


def month_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    result = []
    while start < end:
        next_month = (
            start.replace(year=start.year + 1, month=1, day=1, hour=0)
            if start.month == 12
            else start.replace(month=start.month + 1, day=1, hour=0)
        )
        stop = min(end, next_month)
        result.append((start, stop))
        start = stop
    return result


class HistoryWriter:
    """Short-lived implementation helper; the supplied manager owns all state."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def cpu(
        self, function: Any, *args: Any, context: dict[str, Any], **kwargs: Any
    ) -> Any:
        """Run detached pure transforms on HA's bounded executor, then re-guard."""
        work = partial(function, *args, **kwargs)
        executor = getattr(self.manager._hass, "async_add_executor_job", None)
        result = (
            await executor(work)
            if executor is not None
            else await asyncio.to_thread(work)
        )
        await self.guard(context)
        return result

    async def guard(self, context: dict[str, Any]) -> None:
        self.manager._admit()
        callback = context.get("check_current")
        if callback is not None:
            result = callback()
            if inspect.isawaitable(result):
                await result
        self.manager._admit()

    async def root(self, context: dict[str, Any]) -> dict[str, Any] | None:
        await self.guard(context)
        if self.manager.has_pending_store_save():
            raise HourlyImportError("history_store_write_pending")
        state = await self.manager._store.async_load()
        await self.guard(context)
        if state is not None:
            self.manager._validate(state)
            await self.manager._check_history_root_fence(state)
            await self.guard(context)
            _identity_matches(state["identity"], context["identity"])
            marker = self.manager._entry.data.get(MARKER)
            expected = {"version": state["version"], "token": state["token"]}
            prepared_marker = (
                state.get("previous_marker") if state["version"] == 3 else None
            )
            if marker != expected and not (
                state["phase"] == "prepared" and marker == prepared_marker
            ):
                raise HourlyImportError("history_marker_mismatch")
        elif self.manager._entry.data.get(MARKER) is not None:
            raise HourlyImportError("history_adopted_root_missing")
        return cast(dict[str, Any] | None, deepcopy(state))

    async def read_object(
        self, kind: str, reference: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        require_digest(reference)
        raw = await self.manager._store.async_read_object(kind, reference)
        await self.guard(context)
        if sha256(raw).hexdigest() != reference:
            raise HourlyImportError("history_object_digest_mismatch")
        result = await self.cpu(json.loads, raw, context=context)
        if not isinstance(result, dict):
            raise HourlyImportError("history_object_invalid")
        return result

    async def write_object(
        self, kind: str, value: dict[str, Any], context: dict[str, Any]
    ) -> str:
        raw = await self.cpu(encoded, value, context=context)
        reference = await self.manager._store.async_write_object(kind, raw)
        await self.guard(context)
        if (
            reference != sha256(raw).hexdigest()
            or encoded(await self.read_object(kind, reference, context)) != raw
        ):
            raise HourlyImportError("history_object_write_unverified")
        return cast(str, reference)

    def binding(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "identity": {key: deepcopy(context["identity"][key]) for key in _FIELDS},
            "config_revision": context["config_revision"],
            "timezone": context["timezone"],
            "timezone_revision": context["timezone_revision"],
            "method_version": METHOD_VERSION,
        }

    def request(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            request.get("config_entry_id", self.manager._entry.entry_id)
            != self.manager._entry.entry_id
        ):
            raise HourlyImportError("history_entry_changed")
        first, stop = bounds(request["start"], request["end"])
        quantities = request["quantity_ids"]
        sources = request["source_ids"]
        if (
            not isinstance(quantities, list)
            or not 1 <= len(quantities) <= MAX_QUANTITIES
            or len(set(quantities)) != len(quantities)
        ):
            raise HourlyImportError("history_quantities_invalid")
        if (
            not isinstance(sources, list)
            or not 1 <= len(sources) <= MAX_SOURCE_CHUNKS
            or len(set(sources)) != len(sources)
        ):
            raise HourlyImportError("history_sources_invalid")
        for source in sources:
            require_digest(source)
        if (
            request["archive_policy"] not in {"reject", "before_first_provider"}
            or request["daily_policy"] != DAILY_POLICY
        ):
            raise HourlyImportError("history_policy_invalid")
        for field in ("operation_id", "recovery_reference"):
            if (
                not isinstance(request[field], str)
                or not 1 <= len(request[field]) <= 256
            ):
                raise HourlyImportError("history_operation_binding_invalid")
        if (
            type(request["expected_revision"]) is not int
            or request["expected_revision"] < 0
        ):
            raise HourlyImportError("history_revision_invalid")
        _validate_consumers(request["consumer_contract"])
        evaluated = utc(request.get("evaluated_at", context["evaluated_at"]))
        if evaluated > utc(context["evaluated_at"]):
            raise HourlyImportError("history_evaluation_in_future")
        return {
            "config_entry_id": self.manager._entry.entry_id,
            "source_ids": sorted(sources),
            "quantity_ids": sorted(quantities),
            "start": first.isoformat(),
            "end": stop.isoformat(),
            "evaluated_at": evaluated.isoformat(),
            **{
                key: deepcopy(request[key])
                for key in (
                    "archive_policy",
                    "daily_policy",
                    "operation_id",
                    "expected_revision",
                    "consumer_contract",
                    "recovery_reference",
                )
            },
        }

    async def snapshot(
        self, statistic_id: str, start: datetime, end: datetime, context: dict[str, Any]
    ) -> Any:
        snapshot = await self.manager._recorder.async_snapshot_range(
            statistic_id, start, end
        )
        await self.guard(context)
        if not snapshot.complete or len(snapshot.rows) > MAX_BATCH_HOURS:
            raise HourlyImportError("history_native_snapshot_incomplete")
        stamps = [row.start for row in snapshot.rows]
        if len(set(stamps)) != len(stamps) or any(
            not start <= stamp < end for stamp in stamps
        ):
            raise HourlyImportError("history_native_snapshot_bounds_invalid")
        if snapshot.rows and snapshot.metadata is None:
            raise HourlyImportError("history_native_metadata_missing")
        return replace(
            snapshot, rows=tuple(sorted(snapshot.rows, key=lambda row: row.start))
        )

    async def proof(
        self, selection: dict[str, Any] | None, month: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        reference = (selection or {}).get("proofs", {}).get(month)
        if reference is None:
            return {"hours": []}
        assert selection is not None
        proof = await self.read_object("proof", reference, context)
        if (
            proof.get("quantity_id") != selection["descriptor"]["quantity_id"]
            or proof.get("month") != month
            or len(proof.get("hours", ())) > MAX_BATCH_HOURS
        ):
            raise HourlyImportError("history_proof_identity_invalid")
        stamps = [utc(row["start"], whole_hour=True) for row in proof["hours"]]
        if len(set(stamps)) != len(stamps) or any(
            stamp.strftime("%Y-%m") != month for stamp in stamps
        ):
            raise HourlyImportError("history_proof_bounds_invalid")
        return proof

    async def built(
        self,
        request: dict[str, Any],
        state: dict[str, Any] | None,
        context: dict[str, Any],
        provider_order: dict[str, Any] | None = None,
        provider_supersedes: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        history = state["history"] if state and state["version"] == 3 else {}
        sources, merged = await self.source_view(
            request, history, context, provider_order, provider_supersedes
        )
        thermostats: dict[int, list[dict[str, Any]]] = {}
        sensors: dict[int, list[dict[str, Any]]] = {}
        horizons: dict[int, datetime] = {}
        for resource in merged["resources"].values():
            target = (
                thermostats if resource["resource"] == "runtime_thermostat" else sensors
            )
            target[resource["resource_id"]] = resource["rows"]
        for source in sources:
            manifest = source["manifest"]
            owner, end = manifest["thermostat_id"], utc(manifest["source_end"])
            horizons[owner] = max(horizons.get(owner, end), end)
        first, end = bounds(request["start"], request["end"])
        identity = context["identity"]
        raw_series = await self.cpu(
            build_hourly_statistics,
            thermostats,
            sensors,
            context["config"],
            start=first,
            end=end,
            evaluated_at=utc(request["evaluated_at"]),
            source_end_by_thermostat=horizons,
            existing_statistic_ids=tuple(
                base.removesuffix("_hourly_v2") for base in identity["resources"]
            ),
            context=context,
        )
        represented = await self.cpu(
            build_history_series,
            tuple(
                item
                for item in raw_series
                if item.statistic_id in identity["resources"]
            ),
            identity,
            saved={
                key: selected["descriptor"]
                for key, selected in history.get("selections", {}).items()
            },
            context=context,
        )
        return {item.descriptor["quantity_id"]: item for item in represented}, merged

    async def source_view(
        self,
        request: dict[str, Any],
        history: dict[str, Any],
        context: dict[str, Any],
        provider_order: dict[str, Any] | None = None,
        provider_supersedes: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        first, end = bounds(request["start"], request["end"])
        catalogs: dict[str, dict[str, Any]] = {}
        source_ids = set(request.get("source_ids", ()))
        for start, _stop in month_windows(first, end):
            month = start.strftime("%Y-%m")
            catalogs[month] = await self.catalog(history, month, context)
            source_ids.update(catalogs[month]["source_ids"])
            for key in history.get("source_holds", {}):
                hold = await self.hold(history, key, month, context)
                for interval in hold["intervals"]:
                    source_ids.update(interval["source_ids"])
        # Compatibility for an interrupted candidate predating the month index.
        if not history.get("source_catalog"):
            source_ids.update(history.get("source_ids", ()))
        declarations, sources = await self.load_sources(
            sorted(source_ids), first, end, context
        )
        anchors = dict(history.get("first_provider", {}))
        for key, value in first_provider_points(declarations).items():
            anchors[key] = min(anchors.get(key, value), value)
        order = (
            provider_order
            if provider_order is not None
            else history.get("provider_order", {})
        )
        supersedes = (
            provider_supersedes
            if provider_supersedes is not None
            else history.get("provider_supersedes", {})
        )
        merged = await self.cpu(
            merge_source_bundle,
            sources,
            archive_policy=request["archive_policy"] == "before_first_provider",
            provider_order=order,
            first_provider=anchors,
            provider_supersedes=supersedes,
            context=context,
        )
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        month_ids = {
            month: set(value["source_ids"]) for month, value in catalogs.items()
        }
        for declaration in declarations:
            manifest = declaration["manifest"]
            group = (
                manifest["resource"],
                manifest["resource_id"],
                manifest["source_kind"],
                manifest["acquisition_id"],
            )
            groups.setdefault(group, []).append(declaration)
        for members in groups.values():
            begin = min(
                utc(item.get("observed_start") or item["manifest"]["start"])
                for item in members
            )
            stop = max(
                utc(item.get("observed_end") or item["manifest"]["end"])
                for item in members
            )
            begin = begin.replace(minute=0, second=0, microsecond=0)
            stop = stop.replace(minute=0, second=0, microsecond=0) + _HOUR
            for start, _stop in month_windows(begin, stop):
                month = start.strftime("%Y-%m")
                if month not in catalogs:
                    catalogs[month] = await self.catalog(history, month, context)
                    month_ids[month] = set(catalogs[month]["source_ids"])
                month_ids[month].update(item["source_id"] for item in members)
        for month, ids in month_ids.items():
            catalogs[month]["source_ids"] = sorted(ids)
        catalogs = {
            month: value for month, value in catalogs.items() if value["source_ids"]
        }
        references = {
            **history.get("source_catalog", {}),
            **{
                month: sha256(encoded(value)).hexdigest()
                for month, value in catalogs.items()
            },
        }
        merged["merge_revision"] = merged["source_revision"]
        merged["source_revision"] = digest(
            {
                "catalog": references,
                "first_provider": anchors,
                "provider_order": order,
                "provider_supersedes": supersedes,
            }
        )
        merged["source_ids"] = sorted(source["source_id"] for source in sources)
        merged["source_catalog"] = references
        merged["source_catalog_objects"] = catalogs
        merged["first_provider"] = anchors
        return sources, merged

    async def load_sources(
        self,
        source_ids: list[str],
        first: datetime,
        end: datetime,
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not source_ids:
            return [], []
        declarations, sources = await async_load_catalog_source_bundle(
            self.manager._store, source_ids, context["identity"], window=(first, end)
        )
        await self.guard(context)
        return declarations, sources

    async def catalog(
        self, history: dict[str, Any], month: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        reference = history.get("source_catalog", {}).get(month)
        if reference is None:
            return {"kind": "history_source_catalog", "month": month, "source_ids": []}
        value = await self.read_object("operation", reference, context)
        if (
            value.get("kind") != "history_source_catalog"
            or value.get("month") != month
            or not isinstance(value.get("source_ids"), list)
        ):
            raise HourlyImportError("history_source_catalog_invalid")
        for source_id in value["source_ids"]:
            require_digest(source_id)
        return value

    async def source_points(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        state = await self.root(context)
        if not state or state["version"] != 3:
            raise HourlyImportError("history_source_baseline_unadopted")
        history = state["history"]
        if state["operation"]["status"] != "completed" or state.get("pending"):
            raise HourlyImportError("history_source_baseline_uncommitted")
        operation = await self.read_object(
            "operation", state["operation"]["object"], context
        )
        first, end = bounds(request["start"], request["end"])
        sources, merged = await self.source_view(
            {
                "start": first.isoformat(),
                "end": end.isoformat(),
                "source_ids": [],
                "archive_policy": operation["request"]["archive_policy"],
            },
            history,
            context,
        )
        resources = await self.cpu(
            self.baseline_resources, merged["resources"], first, end, context=context
        )
        result = {
            "start": first.isoformat(),
            "end": end.isoformat(),
            "source_revision": history["source_revision"],
            "evaluation_version": "beestat_points_delta_v1",
            "identity": self.binding(context)["identity"],
            "resources": resources,
            "provider_order": history.get("provider_order", {}),
            "provider_supersedes": history.get("provider_supersedes", {}),
            "acquisitions": self.acquisitions(sources),
            "first_provider": history.get("first_provider", {}),
            "routine_policy": history["routine_policy"],
        }
        result["baseline_digest"] = await self.cpu(digest, result, context=context)
        if digest(await self.root(context)) != digest(state):
            raise HourlyImportError("history_source_baseline_changed")
        return result

    def baseline_resources(
        self, merged: dict[str, Any], first: datetime, end: datetime
    ) -> dict[str, Any]:
        resources = deepcopy(merged)
        for resource in resources.values():
            resource["slots"] = {
                stamp: slot
                for stamp, slot in resource.get("slots", {}).items()
                if first <= utc(stamp) < end
            }
            kept = []
            for row in resource["rows"]:
                instant = _row_stamp(row)
                if instant is None:
                    continue
                if not first <= instant < end:
                    continue
                stamp = instant.isoformat()
                kept.append(row)
                resource.setdefault("slots", {}).setdefault(stamp, {})["row"] = row
            resource["rows"] = kept
        return resources

    def acquisitions(self, sources: list[dict[str, Any]]) -> dict[str, list[str]]:
        result: dict[str, set[str]] = {}
        for source in sources:
            manifest = source["manifest"]
            if manifest["source_kind"] == "provider":
                key = f"{manifest['resource']}:{manifest['resource_id']}"
                result.setdefault(key, set()).add(manifest["acquisition_id"])
        return {key: sorted(values) for key, values in result.items()}

    def supersession(
        self,
        existing: dict[str, Any],
        fresh: list[dict[str, Any]],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        result = deepcopy(existing)
        for key, names in self.acquisitions(fresh).items():
            for name in names:
                predecessors = set(baseline["acquisitions"].get(key, ())) - {name}
                result.setdefault(key, {})[name] = sorted(predecessors)
        return result

    async def hold(
        self, history: dict[str, Any], key: str, month: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        reference = history.get("source_holds", {}).get(key, {}).get(month)
        if reference is None:
            return {
                "kind": "history_source_hold",
                "quantity_id": key,
                "month": month,
                "intervals": [],
            }
        value = await self.read_object("operation", reference, context)
        if (value.get("kind"), value.get("quantity_id"), value.get("month")) != (
            "history_source_hold",
            key,
            month,
        ):
            raise HourlyImportError("history_source_hold_invalid")
        for interval in value["intervals"]:
            start, end = bounds(interval["start"], interval["end"])
            if (
                start.strftime("%Y-%m") != month
                or end - start > _HOUR
                or interval["reason"] != "source_conflict"
            ):
                raise HourlyImportError("history_source_hold_invalid")
            for source_id in interval["source_ids"]:
                require_digest(source_id)
        return value

    async def save_holds(
        self,
        root: dict[str, Any],
        batches: list[dict[str, Any]],
        source_ids: list[str],
        context: dict[str, Any],
    ) -> bool:
        """Fence disputed committed values without creating a Recorder intent."""
        after = deepcopy(root)
        index = after["history"].setdefault("source_holds", {})
        changed = False
        for batch in batches:
            key, month = batch["quantity_id"], batch["month"]
            hold = await self.hold(root["history"], key, month, context)
            intervals = {row["start"]: row for row in hold["intervals"]}
            for hour in batch["proof"]["hours"]:
                if hour["status"] != "source_conflict" or not utc(
                    batch["start"]
                ) <= utc(hour["start"]) < utc(batch["end"]):
                    continue
                prior = intervals.get(hour["start"], {})
                intervals[hour["start"]] = {
                    "start": hour["start"],
                    "end": (utc(hour["start"]) + _HOUR).isoformat(),
                    "reason": "source_conflict",
                    "source_ids": sorted(
                        set(source_ids) | set(prior.get("source_ids", ()))
                    ),
                }
            hold["intervals"] = [intervals[stamp] for stamp in sorted(intervals)]
            if not intervals:
                continue
            reference = await self.write_object("operation", hold, context)
            if index.get(key, {}).get(month) != reference:
                index.setdefault(key, {})[month] = reference
                changed = True
        if not changed:
            return False
        if digest(await self.root(context)) != digest(root):
            raise HourlyImportError("history_root_changed_before_hold")
        after["revision"] += 1
        after["token"] = digest(
            {
                "previous_token": root["token"],
                "source_holds": index,
                "revision": after["revision"],
            }
        )
        # The deterministic immutable fence survives even if HA's independently
        # delayed entry marker and the replacement root both fail to persist.
        fence = await self.manager._invalidate_history_root(root["token"])
        after["history"]["invalidation_fences"] = sorted(
            set(after["history"].get("invalidation_fences", ())) | {fence}
        )
        await self.guard(context)
        self.manager._hass.config_entries.async_update_entry(
            self.manager._entry,
            data={
                **self.manager._entry.data,
                MARKER: {"version": 3, "token": after["token"]},
            },
        )
        self.manager._state, self.manager._loaded = root, True
        await self.manager._save(after)
        await self.guard(context)
        return True

    async def clear_holds(
        self, root: dict[str, Any], batch: dict[str, Any], context: dict[str, Any]
    ) -> None:
        key, month = batch["quantity_id"], batch["month"]
        index = root["history"].get("source_holds", {})
        if month not in index.get(key, {}):
            return
        hold = await self.hold(root["history"], key, month, context)
        first, end = utc(batch["start"]), utc(batch["end"])
        hold["intervals"] = [
            row for row in hold["intervals"] if not first <= utc(row["start"]) < end
        ]
        if hold["intervals"]:
            index[key][month] = await self.write_object("operation", hold, context)
        else:
            del index[key][month]
            if not index[key]:
                del index[key]

    async def batch(
        self,
        item: Any,
        selection: dict[str, Any] | None,
        first: datetime,
        end: datetime,
        resource: dict[str, Any],
        merged: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        month = first.strftime("%Y-%m")
        old = await self.proof(selection, month, context)
        proof_hours = {row["start"]: row for row in old["hours"]}
        snapshot = await self.snapshot(item.statistic_id, first, end, context)
        if selection is None and (snapshot.metadata is not None or snapshot.rows):
            raise HourlyImportError("history_unowned_destination_collision")
        if snapshot.metadata is not None and _metadata(snapshot.metadata) != _metadata(
            item.metadata
        ):
            raise HourlyImportError("history_native_metadata_conflict")
        before = {row.start.isoformat(): _row(row) for row in snapshot.rows}
        _validate_owned_rows(before, proof_hours)
        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        conflict = {
            utc(value, whole_hour=True) for value in resource.get("conflict_hours", ())
        }
        blocked = [
            (utc(value["start"]), utc(value["end"]))
            for value in resource.get("blocked_windows", ())
        ]
        for hour in item.hours:
            if not first <= hour.start < end:
                continue
            reason = hour.reason
            if hour.start in conflict or any(
                start < hour.start + _HOUR and stop >= hour.start
                for start, stop in blocked
            ):
                reason = "source_conflict"
            if reason != "source_conflict" and (
                item.blocked_reason or item.rejected_timestamps
            ):
                reason = item.blocked_reason or "source_timestamp_unplaced"
            values = hour.values if reason == "ready" else None
            stamp = hour.start.isoformat()
            native = _row(_native({"start": stamp, **values})) if values else None
            if native is None and stamp in before:
                native = _row(_native({"start": stamp}))
            if native is not None:
                rows.append(native)
            lineage, confidence = set(), set()
            for index in range(12):
                slot = resource.get("slots", {}).get(
                    (hour.start + timedelta(minutes=5 * index)).isoformat(), {}
                )
                lineage.update(slot.get("source_ids", ()))
                confidence.update(slot.get("confidence", ()))
            proof_hours[stamp] = {
                "start": stamp,
                "status": reason,
                "valid_slots": hour.valid_slots,
                "expected_slots": 12,
                "missing_slots": hour.missing_slots,
                "invalid_slots": hour.invalid_slots,
                "duplicate_slots": hour.duplicate_slots,
                "committed": True,
                "mean": values.get("mean") if values else None,
                "min": values.get("min") if values else None,
                "max": values.get("max") if values else None,
                "source_ids": sorted(lineage),
                "confidence": sorted(confidence),
                "row": native,
            }
            counts[reason] += 1
        proof = {
            "contract_version": 3,
            "quantity_id": item.descriptor["quantity_id"],
            "month": month,
            "metadata": item.metadata,
            "source_revision": merged["source_revision"],
            "hours": [proof_hours[key] for key in sorted(proof_hours)],
        }
        return {
            "quantity_id": item.descriptor["quantity_id"],
            "statistic_id": item.statistic_id,
            "month": month,
            "start": first.isoformat(),
            "end": end.isoformat(),
            "metadata": item.metadata,
            "before_metadata": _metadata(snapshot.metadata),
            "before": [before[key] for key in sorted(before)],
            "rows": rows,
            "proof": proof,
            "dispositions": dict(counts),
        }

    async def plan(
        self,
        request: dict[str, Any],
        context: dict[str, Any],
        provider_order: dict[str, Any] | None = None,
        provider_supersedes: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        normalized = self.request(request, context)
        state = await self.root(context)
        revision = (state or {}).get("revision", 0)
        if revision != normalized["expected_revision"]:
            raise HourlyImportError("history_revision_changed")
        if state and (
            state.get("pending")
            or state.get("pending_selection")
            or (legacy_document(state) or {}).get("pending_selection")
            or state.get("phase") == "prepared"
            or (
                state.get("version") == 3
                and state["operation"]["status"] != "completed"
            )
        ):
            raise HourlyImportError("history_operation_pending")
        built, merged = await self.built(
            normalized, state, context, provider_order, provider_supersedes
        )
        old = legacy_document(state)
        old_owned = (
            {
                quantity_id(record["resource"])
                for record in _validated_records(old).values()
            }
            if old
            else set()
        )
        history = state["history"] if state and state["version"] == 3 else {}
        batches: list[dict[str, Any]] = []
        descriptors: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        metadata_predecessors: dict[str, int] = {}
        first, end = bounds(normalized["start"], normalized["end"])
        for key in normalized["quantity_ids"]:
            item = built.get(key)
            if (
                item is None
                or item.descriptor["admission"] != "eligible"
                or key in old_owned
            ):
                raise HourlyImportError("history_quantity_blocked_or_v2_owned")
            descriptors.append(item.descriptor)
            metadata[key] = item.metadata
            sensor = item.descriptor["sensor_id"]
            resource_key = (
                f"runtime_sensor:{sensor}"
                if sensor is not None
                else f"runtime_thermostat:{item.descriptor['thermostat_id']}"
            )
            resource = merged["resources"].get(resource_key, {})
            for start, stop in month_windows(first, end):
                batch = await self.batch(
                    item,
                    history.get("selections", {}).get(key),
                    start,
                    stop,
                    resource,
                    merged,
                    context,
                )
                if (
                    item.statistic_id in metadata_predecessors
                    and batch["before_metadata"] is None
                ):
                    batch["metadata_predecessor_batch"] = metadata_predecessors[
                        item.statistic_id
                    ]
                    batch["before_metadata"] = _metadata(item.metadata)
                if batch["rows"]:
                    metadata_predecessors.setdefault(item.statistic_id, len(batches))
                batches.append(batch)
        if digest(await self.root(context)) != digest(state):
            raise HourlyImportError("history_root_changed_during_plan")
        manifest = {
            "contract_version": 3,
            "request": normalized,
            "binding": self.binding(context),
            "provider_order": deepcopy(
                provider_order
                if provider_order is not None
                else history.get("provider_order", {})
            ),
            "provider_supersedes": deepcopy(
                provider_supersedes
                if provider_supersedes is not None
                else history.get("provider_supersedes", {})
            ),
            "routine_policy": {
                "lookback_days": 45,
                "cadence_hours": 6,
                "provider_corrections": True,
            },
            "source_ids": merged["source_ids"],
            "source_revision": merged["source_revision"],
            "source_catalog": merged["source_catalog"],
            "source_catalog_objects": merged["source_catalog_objects"],
            "first_provider": merged["first_provider"],
            "source_holds_digest": digest(history.get("source_holds", {})),
            "descriptors": descriptors,
            "metadata": metadata,
            "legacy_reservations": sorted(
                {
                    alias
                    for item in descriptors
                    for alias in item["legacy_statistic_ids"]
                }
            ),
            "batches": [
                {
                    "object": sha256(encoded(batch)).hexdigest(),
                    **{
                        key: batch[key]
                        for key in (
                            "quantity_id",
                            "statistic_id",
                            "month",
                            "start",
                            "end",
                            "dispositions",
                        )
                    },
                    "before_digest": digest(
                        {"metadata": batch["before_metadata"], "rows": batch["before"]}
                    ),
                    "intended_digest": digest(
                        {
                            "metadata": _metadata(batch["metadata"]),
                            "rows": batch["rows"],
                        }
                    ),
                    "before_count": len(batch["before"]),
                    "intended_count": len(batch["rows"]),
                }
                for batch in batches
            ],
        }
        return manifest, batches, state

    async def plan_response(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        offset, limit = (
            request.get("detail_offset", 0),
            request.get("detail_limit", 100),
        )
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise HourlyImportError("history_detail_page_invalid")
        try:
            manifest, _, _ = await self.plan(request, context)
        except HourlyImportError as err:
            normalized = self.request(request, context)
            return {
                "contract_version": 3,
                "status": "blocked",
                "blocking_reasons": [str(err)],
                "request": normalized,
                "operation_id": normalized["operation_id"],
                "expected_revision": normalized["expected_revision"],
                "plan_digest": None,
                "batch_count": 0,
                "batches": [],
            }
        total = len(manifest["batches"])
        return {
            "contract_version": 3,
            "status": "planned",
            "blocking_reasons": [],
            "request": manifest["request"],
            "operation_id": manifest["request"]["operation_id"],
            "expected_revision": manifest["request"]["expected_revision"],
            "plan_digest": digest(manifest),
            "descriptors": manifest["descriptors"],
            "metadata": manifest["metadata"],
            "source_ids": manifest["source_ids"],
            "source_revision": manifest["source_revision"],
            "legacy_reservations": manifest["legacy_reservations"],
            "batch_count": total,
            "numeric_rows": sum(row["intended_count"] for row in manifest["batches"]),
            "batches": manifest["batches"][offset : offset + limit],
            "pagination": {
                "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "total_batches": total,
            },
        }

    async def accept(
        self,
        request: dict[str, Any],
        context: dict[str, Any],
        provider_order: dict[str, Any] | None = None,
        provider_supersedes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan_digest = require_digest(request["plan_digest"])
        state = await self.root(context)
        if (
            state
            and state.get("version") == 3
            and state["operation"]["operation_id"] == request["operation_id"]
        ):
            operation = state["operation"]
            if operation["plan_digest"] != plan_digest:
                raise HourlyImportError("history_operation_id_conflict")
            manifest = await self.read_object("operation", operation["object"], context)
            if (
                digest(manifest) != plan_digest
                or manifest["request"] != self.request(request, context)
                or manifest["binding"] != self.binding(context)
            ):
                raise HourlyImportError("history_operation_replay_changed")
            self.manager._state, self.manager._loaded = state, True
            if state["phase"] == "prepared":
                await self.finish_accept(context)
            elif operation["status"] == "blocked":
                after = deepcopy(state)
                after["operation"]["status"] = "in_progress"
                after["operation"]["error"] = None
                after["revision"] += 1
                await self.manager._save(after)
                await self.guard(context)
            return status(self.manager._state)
        manifest, batches, state = await self.plan(
            request, context, provider_order, provider_supersedes
        )
        if digest(manifest) != plan_digest:
            raise HourlyImportError("history_plan_changed")
        reference = await self.seal_plan(manifest, batches, context)
        if digest(await self.root(context)) != digest(state):
            raise HourlyImportError("history_root_changed_before_accept")
        old = (
            state
            if state and state.get("version") != 3
            else (state or {}).get("legacy_v2")
        )
        root = (
            deepcopy(state)
            if state and state.get("version") == 3
            else {
                "version": 3,
                "entry_id": self.manager._entry.entry_id,
                "revision": (state or {}).get("revision", 0),
                "token": plan_digest,
                "identity": deepcopy(manifest["binding"]["identity"]),
                "legacy_v2": deepcopy(old),
                "history": {
                    "selections": {},
                    "source_ids": [],
                    "source_revision": None,
                    "coverage_revision": 0,
                },
                "previous_marker": deepcopy(self.manager._entry.data.get(MARKER)),
                "pending": None,
            }
        )
        root["phase"] = "prepared"
        for descriptor in manifest["descriptors"]:
            key = descriptor["quantity_id"]
            prior = root["history"]["selections"].get(key, {})
            root["history"]["selections"][key] = {
                "descriptor": {**descriptor, "writer_status": "reserved"},
                "metadata": manifest["metadata"][key],
                "proofs": prior.get("proofs", {}),
                "adopted_at": prior.get("adopted_at", context["evaluated_at"]),
                "timezone": prior.get("timezone", context["timezone"]),
                "timezone_revision": prior.get(
                    "timezone_revision", context["timezone_revision"]
                ),
            }
        root["history"]["source_ids"] = manifest["source_ids"]
        root["history"]["source_revision"] = manifest["source_revision"]
        root["history"]["source_catalog"] = manifest["source_catalog"]
        root["history"]["first_provider"] = manifest["first_provider"]
        root["history"]["source_generation"] = (
            root["history"].get("source_generation", 0) + 1
        )
        root["history"]["provider_order"] = manifest["provider_order"]
        root["history"]["provider_supersedes"] = manifest["provider_supersedes"]
        root["history"]["routine_policy"] = manifest["routine_policy"]
        root["operation"] = {
            "operation_id": request["operation_id"],
            "plan_digest": plan_digest,
            "object": reference,
            "cursor": 0,
            "batch_count": len(batches),
            "status": "accepted",
            "error": None,
        }
        root["revision"] += 1
        self.manager._state, self.manager._loaded = state, True
        await self.manager._save(root)
        await self.guard(context)
        await self.finish_accept(context)
        return status(self.manager._state)

    async def seal_plan(
        self,
        manifest: dict[str, Any],
        batches: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        for batch, summary in zip(batches, manifest["batches"], strict=True):
            if (
                await self.write_object("operation", batch, context)
                != summary["object"]
            ):
                raise HourlyImportError("history_batch_changed")
        for month, catalog in manifest["source_catalog_objects"].items():
            if (
                await self.write_object("operation", catalog, context)
                != manifest["source_catalog"][month]
            ):
                raise HourlyImportError("history_catalog_changed")
        return await self.write_object("operation", manifest, context)

    async def refresh(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept bounded authenticated-provider updates under the saved policy."""
        root = await self.root(context)
        if (
            root is None
            or root["version"] != 3
            or root["operation"]["status"] != "completed"
            or root.get("pending")
        ):
            raise HourlyImportError("history_routine_operation_pending")
        prior = await self.read_object(
            "operation", root["operation"]["object"], context
        )
        binding = self.binding(context)
        if any(
            prior["binding"][key] != binding[key]
            for key in ("identity", "timezone", "timezone_revision", "method_version")
        ):
            raise HourlyImportError("history_routine_context_changed")
        policy = root["history"]["routine_policy"]
        first, end = bounds(request["start"], request["end"])
        current = utc(context["evaluated_at"]).replace(
            minute=0, second=0, microsecond=0
        )
        if (
            first < current - timedelta(days=policy["lookback_days"])
            or end > current + _HOUR
        ):
            raise HourlyImportError("history_routine_outside_accepted_policy")
        if not request["source_ids"]:
            return await self.refresh_retained(request, root, prior, context)
        fresh = await async_load_source_bundle(
            self.manager._store, request["source_ids"], context["identity"]
        )
        await self.guard(context)
        if any(item["manifest"]["source_kind"] != "provider" for item in fresh):
            raise HourlyImportError("history_routine_requires_provider_source")
        deltas = [item["delta"] for item in fresh if item.get("delta")]
        supersedes = deepcopy(root["history"].get("provider_supersedes", {}))
        if deltas:
            baseline = await self.source_points(request, context)
            self.validate_deltas(deltas, baseline, first, end, context)
            supersedes = self.supersession(supersedes, fresh, baseline)
        order = request.get("provider_order", root["history"].get("provider_order", {}))
        normalized = self.routine_request(request, root, prior, context)
        if not normalized["quantity_ids"]:
            return status(root)
        manifest, batches, _ = await self.plan(normalized, context, order, supersedes)
        if any(batch["dispositions"].get("source_conflict") for batch in batches):
            await self.save_holds(root, batches, request["source_ids"], context)
            return {
                **status(self.manager._state),
                "status": "blocked",
                "error": "source_conflict",
                "has_pending": False,
            }
        if deltas:
            baseline = await self.source_points(request, context)
            self.validate_deltas(deltas, baseline, first, end, context)
        return await self.accept(
            {**normalized, "plan_digest": digest(manifest)}, context, order, supersedes
        )

    def routine_request(
        self,
        request: dict[str, Any],
        root: dict[str, Any],
        prior: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        enabled = {
            quantity_id(resource)
            for resource in context["identity"]["resources"].values()
        }
        return {
            "config_entry_id": self.manager._entry.entry_id,
            "source_ids": request["source_ids"],
            "quantity_ids": sorted(set(root["history"]["selections"]) & enabled),
            "start": request["start"],
            "end": request["end"],
            "archive_policy": prior["request"]["archive_policy"],
            "daily_policy": DAILY_POLICY,
            "operation_id": "routine-"
            + digest(
                {
                    "request": request,
                    "revision": root["revision"],
                    "evaluated_at": context["evaluated_at"],
                }
            ),
            "expected_revision": root["revision"],
            "consumer_contract": prior["request"]["consumer_contract"],
            "recovery_reference": prior["request"]["recovery_reference"],
            "evaluated_at": context["evaluated_at"],
        }

    async def refresh_retained(
        self,
        request: dict[str, Any],
        root: dict[str, Any],
        prior: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Only a newly closed, adequately sourced complete hour authorizes work."""
        _sources, merged = await self.source_view(
            {**request, "archive_policy": prior["request"]["archive_policy"]},
            root["history"],
            context,
        )
        normalized = self.routine_request(
            {**request, "source_ids": merged["source_ids"]}, root, prior, context
        )
        if not normalized["source_ids"] or not normalized["quantity_ids"]:
            return {"status": "unchanged", "changed_rows": 0}
        manifest, batches, _state = await self.plan(normalized, context)
        if not await self.new_ready_hours(root, batches, context):
            return {"status": "unchanged", "changed_rows": 0}
        return await self.accept(
            {**normalized, "plan_digest": digest(manifest)}, context
        )

    async def new_ready_hours(
        self,
        root: dict[str, Any],
        batches: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> bool:
        for batch in batches:
            selection = root["history"]["selections"][batch["quantity_id"]]
            old = await self.proof(selection, batch["month"], context)
            statuses = {hour["start"]: hour["status"] for hour in old["hours"]}
            if any(
                hour["status"] == "ready" and statuses.get(hour["start"]) != "ready"
                for hour in batch["proof"]["hours"]
            ):
                return True
        return False

    def validate_deltas(
        self,
        deltas: list[dict[str, Any]],
        baseline: dict[str, Any],
        first: datetime,
        end: datetime,
        context: dict[str, Any],
    ) -> None:
        for delta in deltas:
            window = delta["acquisition"]["bounds"]
            if (
                delta["baseline_source_revision"] != baseline["source_revision"]
                or delta["baseline_digest"] != baseline["baseline_digest"]
                or delta["evaluation_version"] != baseline["evaluation_version"]
                or not first <= utc(window["start"]) <= utc(window["end"]) <= end
                or utc(window["end"]) > utc(context["evaluated_at"])
            ):
                raise HourlyImportError("history_delta_baseline_changed")

    async def finish_accept(self, context: dict[str, Any]) -> None:
        root = deepcopy(self.manager._state)
        marker = {"version": 3, "token": root["token"]}
        current = self.manager._entry.data.get(MARKER)
        if current not in (root.get("previous_marker"), marker):
            raise HourlyImportError("history_marker_changed")
        await self.guard(context)
        if current != marker:
            self.manager._hass.config_entries.async_update_entry(
                self.manager._entry, data={**self.manager._entry.data, MARKER: marker}
            )
        root["phase"] = "hourly"
        root["revision"] += 1
        await self.manager._save(root)
        await self.guard(context)

    def compare(self, batch: dict[str, Any], snapshot: Any) -> list[dict[str, Any]]:
        metadata = _metadata(snapshot.metadata)
        if metadata not in (batch["before_metadata"], _metadata(batch["metadata"])):
            raise HourlyReconciliationError("history_pending_metadata_third_state")
        before = {row["start"]: row for row in batch["before"]}
        intended = {row["start"]: row for row in batch["rows"]}
        actual = {row.start.isoformat(): _row(row) for row in snapshot.rows}
        retry = []
        for stamp in before.keys() | intended.keys() | actual.keys():
            if stamp in intended and actual.get(stamp) == intended[stamp]:
                continue
            if actual.get(stamp) != before.get(stamp):
                raise HourlyReconciliationError("history_pending_row_third_state")
            if stamp in intended:
                retry.append(intended[stamp])
        return sorted(retry, key=lambda row: row["start"])

    async def advance(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._advance(context)
        except Exception as err:
            root = self.manager._state
            if (
                root
                and root.get("version") == 3
                and root["operation"]["status"] != "completed"
            ):
                after = deepcopy(root)
                after["operation"]["status"] = "blocked"
                after["operation"]["error"] = type(err).__name__
                after["revision"] += 1
                try:
                    await self.manager._save(after)
                except Exception:  # noqa: BLE001 - an uncertain save never authorizes another native effect
                    self.manager._error = "history_failure_save_unverified"
            raise

    async def _advance(self, context: dict[str, Any]) -> dict[str, Any]:
        await self.manager._load()
        await self.guard(context)
        root = self.manager._state
        if not root or root.get("version") != 3:
            return status(root)
        _identity_matches(root["identity"], context["identity"])
        operation = root["operation"]
        if operation["status"] in {"completed", "blocked"}:
            return status(root)
        manifest = await self.read_object("operation", operation["object"], context)
        if digest(manifest) != operation["plan_digest"] or manifest[
            "binding"
        ] != self.binding(context):
            raise HourlyImportError("history_operation_context_changed")
        if root["phase"] == "prepared":
            await self.finish_accept(context)
            root = self.manager._state
        if self.manager._entry.data.get(MARKER) != {
            "version": 3,
            "token": root["token"],
        }:
            raise HourlyImportError("history_marker_changed")
        index = operation["cursor"]
        summary = manifest["batches"][index]
        batch = await self.read_object("operation", summary["object"], context)
        if root.get("pending") is None:
            snapshot = await self.snapshot(
                batch["statistic_id"], utc(batch["start"]), utc(batch["end"]), context
            )
            current = {
                "metadata": _metadata(snapshot.metadata),
                "rows": [_row(row) for row in snapshot.rows],
            }
            if digest(current) != summary["before_digest"]:
                raise HourlyImportError("history_reviewed_before_state_changed")
            pending = {
                "generation": 3,
                "operation_id": operation["operation_id"],
                "index": index,
                "batch_object": summary["object"],
                "batch": batch,
            }
            pending["digest"] = digest(pending)
            after = deepcopy(root)
            after["pending"] = pending
            after["operation"]["status"] = "in_progress"
            after["revision"] += 1
            await self.manager._save(after)
            await self.guard(context)
        else:
            pending = root["pending"]
            if (
                pending.get("generation") != 3
                or pending["batch_object"] != summary["object"]
            ):
                raise HourlyImportError("history_pending_owner_changed")
            batch = pending["batch"]
        # A fresh fenced comparison after durable intent also catches external
        # effects that appeared while the Store write was awaiting completion.
        snapshot = await self.snapshot(
            batch["statistic_id"], utc(batch["start"]), utc(batch["end"]), context
        )
        retry = self.compare(batch, snapshot)
        if retry:
            await self.guard(context)
            self.manager._recorder.submit(
                batch["metadata"], tuple(_native(row) for row in retry)
            )
            snapshot = await self.snapshot(
                batch["statistic_id"], utc(batch["start"]), utc(batch["end"]), context
            )
            if self.compare(batch, snapshot):
                raise HourlyReconciliationError("history_native_readback_incomplete")
        # An empty assessed batch creates only proof, never phantom metadata.
        reference = await self.write_object("proof", batch["proof"], context)
        after = deepcopy(self.manager._state)
        selected = after["history"]["selections"][batch["quantity_id"]]
        selected["proofs"][batch["month"]] = reference
        selected["descriptor"]["writer_status"] = "adopted"
        await self.clear_holds(after, batch, context)
        after["history"]["coverage_revision"] += 1
        after["operation"]["cursor"] += 1
        after["operation"]["status"] = (
            "completed"
            if after["operation"]["cursor"] == after["operation"]["batch_count"]
            else "in_progress"
        )
        after["operation"]["error"] = None
        after["pending"] = None
        after["revision"] += 1
        await self.manager._save(after)
        await self.guard(context)
        return status(self.manager._state)

    def configuration(self, context: dict[str, Any]) -> dict[str, Any]:
        state = self.manager._state or {}
        history = state.get("history", {}) if state.get("version") == 3 else {}
        descriptors = {
            item["quantity_id"]: deepcopy(item)
            for item in context.get("descriptors", ())
        }
        for key, item in history.get("selections", {}).items():
            descriptors[key] = deepcopy(item["descriptor"])
        if (
            not state
            and (self.manager._entry.data.get(MARKER) or {}).get("version") == 3
        ):
            descriptors = {}
        return {
            **capabilities(),
            "identity": {key: deepcopy(context["identity"][key]) for key in _FIELDS},
            "root_revision": state.get("revision", 0),
            "source_revision": history.get("source_revision"),
            "coverage_revision": history.get("coverage_revision", 0),
            "operation": self.manager.history_status(),
            "quantities": list(descriptors.values()),
        }

    async def material(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        first, end = bounds(request["start"], request["end"])
        requested = request["quantity_ids"]
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= MAX_QUANTITIES
            or len(set(requested)) != len(requested)
        ):
            raise HourlyImportError("history_query_quantities_invalid")
        state = await self.root(context)
        history = state.get("history", {}) if state and state["version"] == 3 else {}
        selected = history.get("selections", {})
        descriptors = {
            item["quantity_id"]: deepcopy(item)
            for item in context.get("descriptors", ())
        }
        descriptors.update(
            {key: deepcopy(item["descriptor"]) for key, item in selected.items()}
        )
        if any(key not in descriptors for key in requested):
            raise HourlyImportError("history_query_quantity_unknown")
        result: dict[str, Any] = {
            "descriptors": [descriptors[key] for key in requested],
            "hours": {},
            "native_rows": {},
            "native_metadata": {},
            "expected_metadata": {},
            "legacy_days": {},
            "pending_affected": {},
            "source_holds": {},
            "root_revision": (state or {}).get("revision", 0),
            "source_revision": history.get("source_revision"),
            "coverage_revision": history.get("coverage_revision", 0),
            "operation": status(state),
            "root_digest": digest(state),
        }
        for key in requested:
            selection = selected.get(key)
            result["hours"][key], result["native_rows"][key] = [], []
            result["legacy_days"][key] = []
            result["source_holds"][key] = []
            if selection is None:
                continue
            result["expected_metadata"][key] = deepcopy(selection["metadata"])
            metadata = None
            for start, stop in month_windows(first, end):
                hold = await self.hold(history, key, start.strftime("%Y-%m"), context)
                result["source_holds"][key].extend(
                    row
                    for row in hold["intervals"]
                    if utc(row["start"]) < stop and utc(row["end"]) > start
                )
                proof = await self.proof(selection, start.strftime("%Y-%m"), context)
                result["hours"][key].extend(
                    row for row in proof["hours"] if start <= utc(row["start"]) < stop
                )
                snapshot = await self.snapshot(
                    selection["descriptor"]["statistic_id"], start, stop, context
                )
                if metadata is not None and _metadata(snapshot.metadata) != _metadata(
                    metadata
                ):
                    raise HourlyImportError("history_query_metadata_changed")
                metadata = snapshot.metadata
                result["native_rows"][key].extend(_row(row) for row in snapshot.rows)
            result["native_metadata"][key] = deepcopy(metadata)
            if request.get("period", "hour") == "day":
                result["legacy_days"][key] = await self.legacy_days(
                    selection, first, end, context
                )
        pending = (state or {}).get("pending")
        if pending and pending.get("generation") == 3:
            batch = pending["batch"]
            result["pending_affected"][batch["quantity_id"]] = [
                [batch["start"], batch["end"]]
            ]
        await self.check_material(result, context)
        return result

    async def check_material(
        self, material: dict[str, Any], context: dict[str, Any]
    ) -> None:
        # Read from the same durable owner even when the writer cache is cold.
        # Saves replace the cached state; reject a save across root/fence awaits.
        cached = self.manager._state
        state = await self.root(context)
        if (
            digest(state) != material["root_digest"]
            or self.manager._state is not cached
            or self.manager.has_pending_store_save()
        ):
            raise HourlyImportError("history_query_root_changed")

    async def legacy_days(
        self,
        selection: dict[str, Any],
        first: datetime,
        end: datetime,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return await async_read_legacy_days(
            self.manager._recorder,
            selection["descriptor"],
            first,
            end,
            context=context,
            selection=selection,
            check_current=lambda: self.guard(context),
        )
