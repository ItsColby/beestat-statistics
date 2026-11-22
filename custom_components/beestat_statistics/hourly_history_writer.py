"""Independent-hour history through the existing manager's one durable journal.

This helper has no scheduler, provider client, lock or mutable owner of its own.
The entry importer serializes calls; all root commits use HourlyImportManager._save.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .hourly_correction_store import Corrections
from .hourly_history_collection import WriterCollection
from .hourly_history_contract import (
    DAILY_POLICY,
    MAX_QUANTITIES,
    bounds,
    capabilities,
    digest,
    quantity_id,
    require_digest,
    unmapped_sensor,
    utc,
)
from .hourly_history_io import _FIELDS, _binding_matches
from .hourly_history_legacy import async_read_legacy_days
from .hourly_history_planning import WriterPlanning
from .hourly_history_root import _ROOT_VERSIONS, _identity_matches, status
from .hourly_history_source_view import month_windows
from .hourly_import_core import (
    MARKER,
    HourlyImportError,
    HourlyReconciliationError,
    _metadata,
    _native,
    _row,
)
from .hourly_sources import (
    async_load_source_bundle,
)

_HOUR = timedelta(hours=1)


class HistoryWriter(WriterPlanning, WriterCollection):
    """Short-lived implementation helper; the supplied manager owns all state."""

    async def stage_correction(
        self, content: bytes, expected: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Retain reviewed evidence without changing a root or Recorder."""
        await self.guard(context)
        return await Corrections(self).stage(content, expected, context)

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
