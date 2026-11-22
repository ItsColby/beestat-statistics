"""Read-only evidence the history query projects from an adopted root.

Collects each quantity's proof hours, native rows and metadata, source holds,
legacy days and the windows an unfinished operation will replace, then checks
that the durable root did not move while they were read.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from .hourly_history_contract import MAX_QUANTITIES, bounds, capabilities, digest, utc
from .hourly_history_holds import WriterHolds
from .hourly_history_io import _FIELDS
from .hourly_history_legacy import async_read_legacy_days
from .hourly_history_root import _ROOT_VERSIONS, status
from .hourly_history_source_view import month_windows
from .hourly_import_core import MARKER, HourlyImportError, _metadata, _row


class WriterMaterial(WriterHolds):
    """Read the material a history query and the configuration response need."""

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
