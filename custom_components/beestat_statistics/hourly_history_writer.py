"""Independent-hour history through the existing manager's one durable journal.

This helper has no scheduler, provider client, lock or mutable owner of its own.
The entry importer serializes calls; all root commits use HourlyImportManager._save.
"""

from __future__ import annotations

import logging
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .const import HOURLY_V2_SUFFIX
from .hourly_correction_store import Corrections
from .hourly_corrections import project
from .hourly_history_collection import WriterCollection
from .hourly_history_contract import (
    DAILY_POLICY,
    MAX_QUANTITIES,
    MAX_SOURCE_CHUNKS,
    ROUTINE_LOOKBACK_DAYS,
    ROUTINE_QUANTITIES_EXCEEDED,
    bounds,
    canonical_json,
    capabilities,
    digest,
    quantity_id,
    require_digest,
    unmapped_sensor,
    utc,
)
from .hourly_history_io import _FIELDS, _binding_matches
from .hourly_history_legacy import async_read_legacy_days
from .hourly_history_root import _ROOT_VERSIONS, _identity_matches, status
from .hourly_history_values import build_history_series
from .hourly_import_core import (
    MARKER,
    HourlyImportError,
    HourlyReconciliationError,
    _metadata,
    _native,
    _row,
)
from .hourly_sources import (
    _row_stamp,
    async_load_catalog_source_bundle,
    async_load_source_bundle,
    first_provider_points,
    merge_source_bundle,
    source_redundancy,
    source_resolution,
)
from .hourly_statistics import build_hourly_statistics

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)

_LOGGER = logging.getLogger(__name__)


def _changed_window(
    old: dict[str, Any],
    proof: dict[str, Any],
    assessed: list[str],
    before: dict[str, dict[str, Any] | None],
    rows: list[dict[str, Any]],
    held: frozenset[str],
    window: tuple[datetime, datetime],
) -> tuple[datetime, datetime] | None:
    """Bound a routine batch to its touched hours, or None when nothing changes.

    A touched hour has a changed proof entry or row, a hold or a conflict. A
    proof that differs only outside its hours, such as an earlier release's
    format, is re-proved over the whole assessed window once.
    """
    previous = {row["start"]: canonical_json(row) for row in old["hours"]}
    current = {row["start"]: row for row in proof["hours"]}
    intended = {row["start"]: row for row in rows}
    touched = [
        stamp
        for stamp in assessed
        if previous.get(stamp) != canonical_json(current[stamp])
        or before.get(stamp) != intended.get(stamp)
        or current[stamp]["status"] == "source_conflict"
        or stamp in held
    ]
    if touched:
        return utc(touched[0]), utc(touched[-1]) + _HOUR
    if canonical_json(proof) == canonical_json(old):
        return None
    return window


def _sealed(batch: dict[str, Any]) -> dict[str, Any]:
    """Return a batch's stored form, naming its proof object by digest."""
    sealed = {key: value for key, value in batch.items() if key != "proof"}
    sealed["proof_object"] = digest(batch["proof"])
    return sealed


def _nearest(edges: dict[str, list[str]], predecessors: set[str]) -> list[str]:
    """Drop predecessors already reachable through another listed one."""
    reached: set[str] = set()
    pending = [older for name in predecessors for older in edges.get(name, ())]
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(edges.get(name, ()))
    return sorted(predecessors - reached)


def _contract(
    relation: dict[str, Any], retired: dict[str, list[str]]
) -> dict[str, Any]:
    """Remove retired acquisitions, keeping reachability among the rest."""
    result = deepcopy(relation)
    for key, names in retired.items():
        edges = result.get(key)
        if not edges:
            continue
        for name in names:
            older = set(edges.pop(name, ()))
            for newer, predecessors in edges.items():
                if name in predecessors:
                    edges[newer] = sorted((set(predecessors) - {name}) | older)
        for newer, predecessors in list(edges.items()):
            edges[newer] = _nearest(edges, set(predecessors))
            if not edges[newer]:
                del edges[newer]
        if not edges:
            del result[key]
    return result


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


class HistoryWriter(WriterCollection):
    """Short-lived implementation helper; the supplied manager owns all state."""

    async def stage_correction(
        self, content: bytes, expected: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Retain reviewed evidence without changing a root or Recorder."""
        await self.guard(context)
        return await Corrections(self).stage(content, expected, context)

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
        corrections = request.get("correction_ids", [])
        if (
            not isinstance(corrections, list)
            or len(corrections) > MAX_SOURCE_CHUNKS
            or len(set(corrections)) != len(corrections)
        ):
            raise HourlyImportError("history_corrections_invalid")
        for reference in corrections:
            require_digest(reference)
        if (
            request["archive_policy"]
            not in {"reject", "before_first_provider", "provider_gaps"}
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
            **({"correction_ids": sorted(corrections)} if corrections else {}),
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

    async def built(
        self,
        request: dict[str, Any],
        state: dict[str, Any] | None,
        context: dict[str, Any],
        provider_order: dict[str, Any] | None,
        provider_supersedes: dict[str, Any] | None,
    ) -> tuple[Any, dict[str, Any]]:
        history = (
            state["history"] if state and state["version"] in _ROOT_VERSIONS else {}
        )
        correction_index, documents = await Corrections(self).resolve(
            request, history, context
        )
        await Corrections(self).guard_controls(
            request,
            state,
            correction_index,
            provider_order,
            provider_supersedes,
            context,
        )
        source_request = deepcopy(request)
        first, end = bounds(request["start"], request["end"])
        dependency_starts, dependency_ends = [first], [end]
        for wrapped in documents:
            for index in wrapped["entry_indices"]:
                entry = wrapped["document"]["entries"][index]
                dependency_starts.append(utc(entry["dependency_start"]))
                dependency_ends.append(utc(entry["dependency_end"]))
        source_request["start"] = min(dependency_starts).isoformat()
        source_request["end"] = max(dependency_ends).isoformat()
        sources, merged = await self.source_view(
            source_request,
            {**history, "corrections": correction_index},
            context,
            provider_order,
            provider_supersedes,
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
                base.removesuffix(HOURLY_V2_SUFFIX) for base in identity["resources"]
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
        built = {item.descriptor["quantity_id"]: item for item in represented}
        if documents:
            built, quantity_resources = await self.cpu(
                project, built, merged, sources, documents, context, context=context
            )
            merged["quantity_resources"] = quantity_resources
        merged["corrections"] = correction_index
        if (
            correction_index
            or "corrections" in history
            or request.get("correction_ids")
        ):
            merged["source_revision"] = digest(
                {"raw": merged["source_revision"], "corrections": correction_index}
            )
        return built, merged

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
        requested = set(request.get("source_ids", ()))
        cataloged: set[str] = set()
        held: set[str] = set()
        for start, _stop in month_windows(first, end):
            month = start.strftime("%Y-%m")
            catalogs[month] = await self.catalog(history, month, context)
            cataloged.update(catalogs[month]["source_ids"])
            for key in history.get("source_holds", {}):
                hold = await self.hold(history, key, month, context)
                for interval in hold["intervals"]:
                    held.update(interval["source_ids"])
        source_ids = requested | cataloged | held
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
        archive_policy = (
            "provider_gaps"
            if request["archive_policy"] == "provider_gaps"
            else request["archive_policy"] == "before_first_provider"
        )
        merged = await self.cpu(
            merge_source_bundle,
            sources,
            archive_policy=archive_policy,
            provider_order=order,
            first_provider=anchors,
            provider_supersedes=supersedes,
            context=context,
        )
        # Sources being admitted and held conflicts are never retired here.
        correction_sources = await Corrections(self).protected_sources(
            history, sources, context
        )
        redundancy = await self.cpu(
            source_redundancy,
            sources,
            provider_order=order,
            provider_supersedes=supersedes,
            window=(first, end),
            protected=held | (requested - cataloged) | correction_sources,
            context=context,
        )
        retired = set(redundancy["source_ids"])
        if retired:
            kept = [item for item in sources if item["source_id"] not in retired]
            contracted = _contract(supersedes, redundancy["acquisitions"])
            remerged = await self.cpu(
                merge_source_bundle,
                kept,
                archive_policy=archive_policy,
                provider_order=order,
                first_provider=anchors,
                provider_supersedes=contracted,
                context=context,
            )
            before = await self.cpu(source_resolution, merged, sources, context=context)
            after = await self.cpu(source_resolution, remerged, kept, context=context)
            # Retirement may change lineage only; any other difference keeps all.
            if before == after:
                sources, merged, supersedes = kept, remerged, contracted
            else:
                _LOGGER.warning(
                    "Kept %d redundant hourly history sources because retiring "
                    "them would change resolved history",
                    len(retired),
                )
                retired = set()
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
            catalogs[month]["source_ids"] = sorted(ids - retired)
        # A loaded month's reference follows its catalog, which retirement can
        # empty; unloaded months keep the reference the root already holds.
        references = {
            month: reference
            for month, reference in history.get("source_catalog", {}).items()
            if month not in catalogs
        }
        catalogs = {
            month: value for month, value in catalogs.items() if value["source_ids"]
        }
        references.update({month: digest(value) for month, value in catalogs.items()})
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
        merged["provider_supersedes"] = supersedes
        merged["maximal_days"] = redundancy["days"]
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
        if not state or state["version"] not in _ROOT_VERSIONS:
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
        current = utc(context["evaluated_at"]).replace(
            minute=0, second=0, microsecond=0
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
            "compaction": self.compaction(sources, merged, first, current),
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

    def compaction(
        self,
        sources: list[dict[str, Any]],
        merged: dict[str, Any],
        first: datetime,
        current: datetime,
    ) -> dict[str, list[str]]:
        """Name closed UTC days whose points still need several acquisitions.

        The next delta carries such a day's points again, so one acquisition
        holds the day and the others retire. A day is closed once it ends by
        both the current hour and its thermostat's provider horizon.
        """
        horizons: dict[int, datetime] = {}
        for source in sources:
            manifest = source["manifest"]
            owner, end = manifest["thermostat_id"], utc(manifest["source_end"])
            horizons[owner] = max(horizons.get(owner, end), end)
        result: dict[str, list[str]] = {}
        for key, days in merged["maximal_days"].items():
            owner = merged["resources"][key]["thermostat_id"]
            limit = min(current, horizons.get(owner, current))
            closed = [
                day
                for day, count in days.items()
                if count > 1 and first <= utc(day) and utc(day) + _DAY <= limit
            ]
            if closed:
                result[key] = closed
        return result

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
        """Record each new acquisition's nearest predecessors only.

        Precedence uses the transitive closure, so dropping predecessors that
        another listed predecessor already reaches keeps it exact while the
        relation grows linearly. Lists left unreduced by earlier releases are
        reduced once on the next delta.
        """
        result = deepcopy(existing)
        for key, names in self.acquisitions(fresh).items():
            edges = result.setdefault(key, {})
            for name in names:
                predecessors = set(baseline["acquisitions"].get(key, ())) - {name}
                edges[name] = _nearest(edges, predecessors)
            for newer, older in list(edges.items()):
                if len(older) > 1:
                    edges[newer] = _nearest(edges, set(older))
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
                or interval["reason"]
                not in {"source_conflict", "source_timestamp_unplaced"}
            ):
                raise HourlyImportError("history_source_hold_invalid")
            for source_id in interval["source_ids"]:
                require_digest(source_id)
        return value

    async def block_disputed_source(
        self,
        root: dict[str, Any],
        batches: list[dict[str, Any]],
        source_ids: list[str],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Fence routine source disputes without creating a Recorder intent."""
        reasons: set[str] = set()
        after = None
        for batch in batches:
            if not {"source_conflict", "source_timestamp_unplaced"}.intersection(
                batch["dispositions"]
            ):
                continue
            committed_values = {
                row["start"] for row in batch["before"] if row["mean"] is not None
            }
            first, end = bounds(batch["start"], batch["end"])
            disputed = {
                hour["start"]: hour["status"]
                for hour in batch["proof"]["hours"]
                if first <= utc(hour["start"]) < end
                and (
                    hour["status"] == "source_conflict"
                    or (
                        hour["status"] == "source_timestamp_unplaced"
                        and hour["start"] in committed_values
                    )
                )
            }
            if not disputed:
                continue
            reasons.update(disputed.values())
            key, month = batch["quantity_id"], batch["month"]
            hold = await self.hold(root["history"], key, month, context)
            intervals = {row["start"]: row for row in hold["intervals"]}
            for stamp, reason in disputed.items():
                prior = intervals.get(stamp, {})
                intervals[stamp] = {
                    "start": stamp,
                    "end": (utc(stamp) + _HOUR).isoformat(),
                    "reason": reason,
                    "source_ids": sorted(
                        set(source_ids) | set(prior.get("source_ids", ()))
                    ),
                }
            updated = [intervals[stamp] for stamp in sorted(intervals)]
            if updated != hold["intervals"]:
                reference = await self.write_object(
                    "operation", {**hold, "intervals": updated}, context
                )
                if after is None:
                    after = deepcopy(root)
                index = after["history"].setdefault("source_holds", {})
                index.setdefault(key, {})[month] = reference
        if not reasons:
            return None
        if after is not None:
            await self.commit_source_holds(root, after, context)
        return {
            **status(root if after is None else self.manager._state),
            "status": "blocked",
            "error": "source_conflict"
            if "source_conflict" in reasons
            else "source_timestamp_unplaced",
            "has_pending": False,
        }

    async def commit_source_holds(
        self, root: dict[str, Any], after: dict[str, Any], context: dict[str, Any]
    ) -> None:
        if digest(await self.root(context)) != digest(root):
            raise HourlyImportError("history_root_changed_before_hold")
        after["revision"] += 1
        after["token"] = digest(
            {
                "previous_token": root["token"],
                "source_holds": after["history"]["source_holds"],
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
                MARKER: {"version": after["version"], "token": after["token"]},
            },
        )
        self.manager._state, self.manager._loaded = root, True
        await self.manager._save(after)
        await self.guard(context)

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
        context: dict[str, Any],
        *,
        changed_only: bool = False,
        held: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        """Assess one quantity/month; a routine pass keeps only touched hours.

        The proof always covers the whole month. With ``changed_only`` the
        native range narrows to the hours whose proof, row or hold changes,
        and an untouched month yields no batch at all.
        """
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
        assessed: list[str] = []
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
            correction_id = resource.get("correction_ids", {}).get(
                hour.start.isoformat()
            )
            if (
                not correction_id
                and reason != "source_conflict"
                and (item.blocked_reason or item.rejected_timestamps)
            ):
                reason = item.blocked_reason or "source_timestamp_unplaced"
            values = hour.values if reason == "ready" else None
            stamp = hour.start.isoformat()
            assessed.append(stamp)
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
            confidence.update(
                ("reviewed_quantity_correction_v1",) if correction_id else ()
            )
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
                **({"correction_id": correction_id} if correction_id else {}),
            }
        # The proof binds each hour's own lineage, not the whole-catalog
        # revision, so an untouched month's proof stays byte-identical.
        proof = {
            "contract_version": 3,
            "quantity_id": item.descriptor["quantity_id"],
            "month": month,
            "metadata": item.metadata,
            "hours": [proof_hours[key] for key in sorted(proof_hours)],
        }
        if changed_only:
            window = _changed_window(
                old, proof, assessed, before, rows, held, (first, end)
            )
            if window is None:
                return None
            first, end = window
            assessed = [stamp for stamp in assessed if first <= utc(stamp) < end]
            rows = [row for row in rows if first <= utc(row["start"]) < end]
            before = {
                stamp: row for stamp, row in before.items() if first <= utc(stamp) < end
            }
        counts = Counter(proof_hours[stamp]["status"] for stamp in assessed)
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
        *,
        changed_only: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        normalized = self.request(request, context)
        state = await self.root(context)
        revision = (state or {}).get("revision", 0)
        if revision != normalized["expected_revision"]:
            raise HourlyImportError("history_revision_changed")
        if state and (
            state.get("pending")
            or state.get("phase") == "prepared"
            or (
                state.get("version") in _ROOT_VERSIONS
                and state["operation"]["status"] != "completed"
            )
        ):
            raise HourlyImportError("history_operation_pending")
        built, merged = await self.built(
            normalized, state, context, provider_order, provider_supersedes
        )
        history = (
            state["history"] if state and state["version"] in _ROOT_VERSIONS else {}
        )
        batches: list[dict[str, Any]] = []
        descriptors: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        metadata_predecessors: dict[str, int] = {}
        first, end = bounds(normalized["start"], normalized["end"])
        for key in normalized["quantity_ids"]:
            item = built.get(key)
            if item is None or item.descriptor["admission"] != "eligible":
                raise HourlyImportError("history_quantity_blocked")
            descriptors.append(item.descriptor)
            metadata[key] = item.metadata
            sensor = item.descriptor["sensor_id"]
            resource_key = (
                f"runtime_sensor:{sensor}"
                if sensor is not None
                else f"runtime_thermostat:{item.descriptor['thermostat_id']}"
            )
            resource = merged.get("quantity_resources", {}).get(
                key, merged["resources"].get(resource_key, {})
            )
            for start, stop in month_windows(first, end):
                hold = await self.hold(history, key, start.strftime("%Y-%m"), context)
                batch = await self.batch(
                    item,
                    history.get("selections", {}).get(key),
                    start,
                    stop,
                    resource,
                    context,
                    changed_only=changed_only,
                    held=frozenset(row["start"] for row in hold["intervals"]),
                )
                if batch is None:
                    continue
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
        # Routine refresh requests every adopted, still enabled quantity in one
        # request, so that total must stay within one request's limit.
        enabled = {
            quantity_id(resource)
            for resource in context["identity"]["resources"].values()
            if not unmapped_sensor(resource)
        }
        routine = (set(history.get("selections", {})) & enabled) | {
            item["quantity_id"] for item in descriptors
        }
        if len(routine) > MAX_QUANTITIES:
            raise HourlyImportError(ROUTINE_QUANTITIES_EXCEEDED)
        manifest = {
            "contract_version": 3,
            "request": normalized,
            "binding": self.binding(context),
            "provider_order": deepcopy(
                provider_order
                if provider_order is not None
                else history.get("provider_order", {})
            ),
            "provider_supersedes": deepcopy(merged["provider_supersedes"]),
            "routine_policy": {
                "lookback_days": ROUTINE_LOOKBACK_DAYS,
                "cadence_hours": 6,
                "provider_corrections": True,
            },
            "source_ids": merged["source_ids"],
            "source_revision": merged["source_revision"],
            "source_catalog": merged["source_catalog"],
            "source_catalog_objects": merged["source_catalog_objects"],
            "first_provider": merged["first_provider"],
            "source_holds_digest": digest(history.get("source_holds", {})),
            **(
                {"corrections": merged["corrections"]}
                if merged["corrections"]
                or "corrections" in history
                or normalized.get("correction_ids")
                else {}
            ),
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
                    "object": digest(_sealed(batch)),
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
        *,
        changed_only: bool = False,
    ) -> dict[str, Any]:
        plan_digest = require_digest(request["plan_digest"])
        state = await self.root(context)
        if (
            state
            and state.get("version") in _ROOT_VERSIONS
            and state["operation"]["operation_id"] == request["operation_id"]
        ):
            operation = state["operation"]
            if operation["plan_digest"] != plan_digest:
                raise HourlyImportError("history_operation_id_conflict")
            manifest = await self.read_object("operation", operation["object"], context)
            if (
                digest(manifest) != plan_digest
                or manifest["request"] != self.request(request, context)
                or not _binding_matches(
                    manifest["binding"], self.binding(context), context
                )
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
            request,
            context,
            provider_order,
            provider_supersedes,
            changed_only=changed_only,
        )
        if digest(manifest) != plan_digest:
            raise HourlyImportError("history_plan_changed")
        reference = await self.seal_plan(manifest, batches, context)
        if digest(await self.root(context)) != digest(state):
            raise HourlyImportError("history_root_changed_before_accept")
        root = (
            deepcopy(state)
            if state and state.get("version") in _ROOT_VERSIONS
            else {
                "version": 3,
                "entry_id": self.manager._entry.entry_id,
                "revision": (state or {}).get("revision", 0),
                "token": plan_digest,
                "identity": deepcopy(manifest["binding"]["identity"]),
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
        root["previous_marker"] = deepcopy(self.manager._entry.data.get(MARKER))
        if "corrections" in manifest:
            root["version"] = 4
            root["history"]["corrections"] = manifest["corrections"]
        for descriptor in manifest["descriptors"]:
            key = descriptor["quantity_id"]
            prior = root["history"]["selections"].get(key, {})
            # Adoption is ownership, not verification, so a pass that leaves a
            # quantity untouched keeps it adopted.
            adopted = prior.get("descriptor", {}).get("writer_status") == "adopted"
            root["history"]["selections"][key] = {
                "descriptor": {
                    **descriptor,
                    "writer_status": "adopted" if adopted else "reserved",
                },
                "metadata": manifest["metadata"][key],
                "proofs": prior.get("proofs", {}),
                "adopted_at": prior.get("adopted_at", context["evaluated_at"]),
                "timezone": prior.get("timezone", context["timezone"]),
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
            # A routine source that changes no proof or row commits only its
            # catalog membership; there is no native effect left to verify.
            "status": "accepted" if batches else "completed",
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
            sealed = _sealed(batch)
            # The proof precedes the batch that names it, so a sealed batch
            # never references a missing proof.
            if (
                await self.write_object("proof", batch["proof"], context)
                != sealed["proof_object"]
                or await self.write_object("operation", sealed, context)
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
            or root["version"] not in _ROOT_VERSIONS
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
            for key in ("identity", "timezone", "method_version")
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
        manifest, batches, _ = await self.plan(
            normalized, context, order, supersedes, changed_only=True
        )
        await self.validate_native_rows(root, batches, context)
        if blocked := await self.block_disputed_source(
            root, batches, request["source_ids"], context
        ):
            return blocked
        if deltas:
            baseline = await self.source_points(request, context)
            self.validate_deltas(deltas, baseline, first, end, context)
        return await self.accept(
            {**normalized, "plan_digest": digest(manifest)},
            context,
            order,
            supersedes,
            changed_only=True,
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
            if not unmapped_sensor(resource)
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
        manifest, batches, _state = await self.plan(
            normalized, context, changed_only=True
        )
        newly_ready = await self.validate_native_rows(root, batches, context)
        if blocked := await self.block_disputed_source(root, batches, [], context):
            return blocked
        if not newly_ready:
            return {"status": "unchanged", "changed_rows": 0}
        return await self.accept(
            {**normalized, "plan_digest": digest(manifest)},
            context,
            changed_only=True,
        )

    async def validate_native_rows(
        self,
        root: dict[str, Any],
        batches: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> bool:
        """Validate routine native ownership and report newly closed hours."""
        newly_ready = False
        for batch in batches:
            selection = root["history"]["selections"][batch["quantity_id"]]
            old = await self.proof(selection, batch["month"], context)
            first, end = bounds(batch["start"], batch["end"])
            native = {row["start"]: row for row in batch["before"]}
            if any(
                hour.get("row") != native.get(hour["start"])
                for hour in old["hours"]
                if first <= utc(hour["start"]) < end
            ):
                raise HourlyImportError("history_routine_native_row_changed")
            statuses = {hour["start"]: hour["status"] for hour in old["hours"]}
            if any(
                hour["status"] == "ready" and statuses.get(hour["start"]) != "ready"
                for hour in batch["proof"]["hours"]
            ):
                newly_ready = True
        return newly_ready

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
        marker = {"version": root["version"], "token": root["token"]}
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
                and root.get("version") in _ROOT_VERSIONS
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
        if not root or root.get("version") not in _ROOT_VERSIONS:
            return status(root)
        _identity_matches(root["identity"], context["identity"])
        operation = root["operation"]
        if operation["status"] == "blocked" or (
            operation["status"] == "completed" and root["phase"] != "prepared"
        ):
            return status(root)
        manifest = await self.read_object("operation", operation["object"], context)
        if digest(manifest) != operation["plan_digest"] or not _binding_matches(
            manifest["binding"], self.binding(context), context
        ):
            raise HourlyImportError("history_operation_context_changed")
        if root["phase"] == "prepared":
            # A batchless accept is saved completed; only its marker step remains.
            await self.finish_accept(context)
            root = self.manager._state
            if root["operation"]["status"] == "completed":
                return status(root)
        if self.manager._entry.data.get(MARKER) != {
            "version": root["version"],
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
                **{key: batch[key] for key in ("quantity_id", "start", "end")},
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
                or any(
                    pending.get("batch", pending)[key] != batch[key]
                    for key in ("quantity_id", "start", "end")
                )
            ):
                raise HourlyImportError("history_pending_owner_changed")
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
        reference = await self.batch_proof(batch, context)
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

    async def batch_proof(self, batch: dict[str, Any], context: dict[str, Any]) -> str:
        """Return the retained proof a verified batch publishes."""
        if "proof" in batch:
            # Batches sealed by earlier releases embed their proof.
            return await self.write_object("proof", batch["proof"], context)
        reference = require_digest(batch["proof_object"])
        # Hash-verified readback keeps the root from naming a missing proof.
        await self.read_object("proof", reference, context)
        return reference

    def configuration(self, context: dict[str, Any]) -> dict[str, Any]:
        state = self.manager._state or {}
        history = (
            state.get("history", {}) if state.get("version") in _ROOT_VERSIONS else {}
        )
        descriptors = {
            item["quantity_id"]: deepcopy(item)
            for item in context.get("descriptors", ())
        }
        for key, item in history.get("selections", {}).items():
            descriptors[key] = deepcopy(item["descriptor"])
        if (
            not state
            and (self.manager._entry.data.get(MARKER) or {}).get("version")
            in _ROOT_VERSIONS
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
        history = (
            state.get("history", {})
            if state and state["version"] in _ROOT_VERSIONS
            else {}
        )
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
        result["pending_affected"] = await self.unfinished_windows(state, context)
        await self.check_material(result, context)
        return result

    async def unfinished_windows(
        self, state: dict[str, Any] | None, context: dict[str, Any]
    ) -> dict[str, list[list[str]]]:
        """Suppress replaced proof until each accepted batch commits its successor."""
        if not state or state.get("version") not in _ROOT_VERSIONS:
            return {}
        operation = state["operation"]
        if operation["cursor"] == operation["batch_count"]:
            return {}
        manifest = await self.read_object("operation", operation["object"], context)
        if (
            digest(manifest) != operation["plan_digest"]
            or len(manifest["batches"]) != operation["batch_count"]
        ):
            raise HourlyImportError("history_operation_context_changed")
        windows: dict[str, list[list[str]]] = {}
        # Acceptance already changes source_revision. The current pending intent
        # covers only one batch and may not exist yet (or between batches).
        # The sealed manifest owns every remaining half-open replacement window.
        for batch in manifest["batches"][operation["cursor"] :]:
            windows.setdefault(batch["quantity_id"], []).append(
                [batch["start"], batch["end"]]
            )
        return windows

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
