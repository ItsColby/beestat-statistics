"""The source view behind independent-hour history.

Resolves the sources a request, the root's monthly catalogs and its holds name
into one merged view, retires redundant acquisitions only when resolved history
stays identical, and builds the quantities a plan assesses. Routine deltas read
the committed view as their baseline.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .const import HOURLY_V2_SUFFIX
from .hourly_correction_store import Corrections
from .hourly_corrections import project
from .hourly_history_contract import bounds, digest, require_digest, utc
from .hourly_history_holds import WriterHolds
from .hourly_history_root import _ROOT_VERSIONS
from .hourly_history_values import build_history_series
from .hourly_import_core import HourlyImportError
from .hourly_sources import (
    _row_stamp,
    async_load_catalog_source_bundle,
    first_provider_points,
    merge_source_bundle,
    source_redundancy,
    source_resolution,
)
from .hourly_statistics import build_hourly_statistics

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)


_LOGGER = logging.getLogger(__name__)


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


class WriterSources(WriterHolds):
    """Resolve, merge and build the sources a request or the root names."""

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
