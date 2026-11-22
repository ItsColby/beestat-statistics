"""Planning and sealing of independent-hour history operations.

A plan normalizes the request, assesses each quantity/month against its proof
and native snapshot, and digests the result; nothing changes until a reviewed
plan's digest is accepted, when ``seal_plan`` stores its objects.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .hourly_history_contract import (
    DAILY_POLICY,
    MAX_QUANTITIES,
    MAX_SOURCE_CHUNKS,
    ROUTINE_LOOKBACK_DAYS,
    ROUTINE_QUANTITIES_EXCEEDED,
    bounds,
    canonical_json,
    digest,
    quantity_id,
    require_digest,
    unmapped_sensor,
    utc,
)
from .hourly_history_root import _ROOT_VERSIONS
from .hourly_history_source_view import WriterSources, month_windows
from .hourly_import_core import HourlyImportError, _metadata, _native, _row

_HOUR = timedelta(hours=1)


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


class WriterPlanning(WriterSources):
    """Normalize requests and assess them into a digested, sealable plan."""

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
