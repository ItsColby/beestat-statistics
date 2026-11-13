"""Immutable reviewed evidence and the history root's bounded correction index.

The entry importer owns the lock. Staging retains bytes only; activation belongs
to the existing sealed plan and durable writer operation.
"""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from hashlib import sha256
from typing import Any, cast

from .hourly_corrections import validate_document, validate_points
from .hourly_history_contract import (
    MAX_SOURCE_BYTES,
    bounds,
    require_digest,
    utc,
)
from .hourly_import_core import HourlyImportError

MAX_CORRECTIONS = 4096


def validate_index(index: Any) -> None:
    """Reject unsupported, ambiguous or unbounded persisted references."""
    if not isinstance(index, dict):
        raise TypeError("history_correction_index_invalid")
    count = 0
    for key, hours in index.items():
        if not isinstance(key, str) or not isinstance(hours, dict) or not hours:
            raise ValueError("history_correction_index_invalid")
        for stamp, reference in hours.items():
            if utc(stamp, whole_hour=True).isoformat() != stamp:
                raise ValueError("history_correction_index_invalid")
            if (
                not isinstance(reference, dict)
                or set(reference) != {"object", "entry"}
                or type(reference["entry"]) is not int
                or not 0 <= reference["entry"] < MAX_CORRECTIONS
            ):
                raise ValueError("history_correction_index_invalid")
            require_digest(reference["object"])
            count += 1
    if count > MAX_CORRECTIONS:
        raise ValueError("history_correction_index_limit")


def _activate(
    index: dict[str, Any], reference: str, number: int, entry: dict[str, Any]
) -> None:
    key, stamp = entry["descriptor"]["quantity_id"], entry["start"]
    old = index.get(key, {}).get(stamp)
    if old == {"object": reference, "entry": number}:
        return
    if entry["predecessor"] != (old or {}).get("object"):
        raise HourlyImportError("history_correction_predecessor_changed")
    if entry["action"] == "withdraw":
        if old is None:
            raise HourlyImportError("history_correction_withdrawal_missing")
        del index[key][stamp]
        if not index[key]:
            del index[key]
    else:
        index.setdefault(key, {})[stamp] = {"object": reference, "entry": number}


class Corrections:
    """Use the existing writer's guarded object I/O, not another journal."""

    def __init__(self, writer: Any) -> None:
        self.writer = writer

    async def document(self, reference: str, context: dict[str, Any]) -> dict[str, Any]:
        document = await self.writer.read_object("correction", reference, context)
        await self.writer.cpu(
            validate_document,
            document,
            self.writer.binding(context),
            {item["quantity_id"]: item for item in context["descriptors"]},
            context=context,
        )
        for original in document["originals"]:
            raw = await self.writer.manager._store.async_read_object(
                "source", original["sha256"]
            )
            await self.writer.guard(context)
            if sha256(raw).hexdigest() != original["sha256"]:
                raise HourlyImportError("history_correction_original_changed")
        return cast(dict[str, Any], document)

    async def stage(
        self, content: bytes, expected: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        require_digest(expected)
        if len(content) > MAX_SOURCE_BYTES or sha256(content).hexdigest() != expected:
            raise ValueError("history_correction_upload_invalid")
        bundle = await self.writer.cpu(json.loads, content, context=context)
        if not isinstance(bundle, dict) or set(bundle) != {"document", "originals"}:
            raise ValueError("history_correction_bundle_invalid")
        document = await self.writer.cpu(
            validate_document,
            bundle["document"],
            self.writer.binding(context),
            {item["quantity_id"]: item for item in context["descriptors"]},
            context=context,
        )
        await self.writer.cpu(validate_points, document, context, context=context)
        supplied = bundle["originals"]
        if not isinstance(supplied, list):
            raise TypeError("history_correction_original_invalid")
        originals: dict[str, bytes] = {}
        for item in supplied:
            if not isinstance(item, dict) or set(item) != {"sha256", "content_base64"}:
                raise ValueError("history_correction_original_invalid")
            reference = require_digest(item["sha256"])
            raw = base64.b64decode(item["content_base64"], validate=True)
            if (
                not raw
                or len(raw) > MAX_SOURCE_BYTES
                or sha256(raw).hexdigest() != reference
                or reference in originals
            ):
                raise ValueError("history_correction_original_invalid")
            originals[reference] = raw
        if set(originals) != {item["sha256"] for item in document["originals"]}:
            raise ValueError("history_correction_original_missing")
        for reference, raw in originals.items():
            actual = await self.writer.manager._store.async_write_object("source", raw)
            await self.writer.guard(context)
            stored = await self.writer.manager._store.async_read_object(
                "source", reference
            )
            await self.writer.guard(context)
            if actual != reference or stored != raw:
                raise HourlyImportError("history_correction_original_unverified")
        reference = await self.writer.write_object("correction", document, context)
        await self.document(reference, context)
        return {
            "status": "staged",
            "correction_id": reference,
            "entry_count": len(document["entries"]),
            "holds": sum(item["action"] == "hold" for item in document["entries"]),
        }

    async def resolve(
        self, request: dict[str, Any], history: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        index = deepcopy(history.get("corrections", {}))
        validate_index(index)
        documents: dict[str, dict[str, Any]] = {}
        first, end = bounds(request["start"], request["end"])
        for reference in request.get("correction_ids", ()):
            document = await self.document(reference, context)
            documents[reference] = document
            for number, entry in enumerate(document["entries"]):
                key = entry["descriptor"]["quantity_id"]
                stamp = entry["start"]
                if key not in request["quantity_ids"] or not first <= utc(stamp) < end:
                    raise HourlyImportError("history_correction_outside_plan")
                _activate(index, reference, number, entry)
        validate_index(index)
        active: dict[str, list[int]] = {}
        for key, hours in index.items():
            for stamp, item in hours.items():
                reference, number = item["object"], item["entry"]
                if reference not in documents:
                    documents[reference] = await self.document(reference, context)
                entries = documents[reference]["entries"]
                if (
                    number >= len(entries)
                    or (
                        entries[number]["descriptor"]["quantity_id"],
                        entries[number]["start"],
                    )
                    != (key, stamp)
                    or entries[number]["action"] == "withdraw"
                ):
                    raise HourlyImportError("history_correction_index_changed")
                if key in request["quantity_ids"] and first <= utc(stamp) < end:
                    active.setdefault(reference, []).append(number)
        await self.guard_admission(request, history, index, documents, context)
        return index, [
            {
                "correction_id": reference,
                "document": documents[reference],
                "entry_indices": numbers,
            }
            for reference, numbers in sorted(active.items())
        ]

    async def guard_admission(
        self,
        request: dict[str, Any],
        history: dict[str, Any],
        index: dict[str, Any],
        documents: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """A partial plan cannot admit evidence while leaving dependent proof live."""
        if not index:
            return
        known = set(history.get("source_ids", ()))
        for month in history.get("source_catalog", {}):
            catalog = await self.writer.catalog(history, month, context)
            known.update(catalog["source_ids"])
        fresh = []
        for reference in set(request["source_ids"]) - known:
            source = await self.writer.read_object("manifest", reference, context)
            fresh.append(source)
        first, end = bounds(request["start"], request["end"])
        for key, hours in index.items():
            for stamp, reference in hours.items():
                if key in request["quantity_ids"] and first <= utc(stamp) < end:
                    continue
                entry = documents[reference["object"]]["entries"][reference["entry"]]
                descriptor = entry["descriptor"]
                sensor = descriptor["sensor_id"]
                kind, resource = (
                    ("runtime_sensor", sensor)
                    if sensor is not None
                    else ("runtime_thermostat", descriptor["thermostat_id"])
                )
                start, stop = bounds(entry["dependency_start"], entry["dependency_end"])
                if any(
                    (source["manifest"]["resource"], source["manifest"]["resource_id"])
                    == (kind, resource)
                    and (
                        (
                            utc(source["manifest"]["start"]) < stop
                            and utc(source["manifest"]["end"]) > start
                        )
                        or (
                            source["manifest"]["source_kind"] == "provider"
                            and source["observed_start"] is not None
                            and (
                                f"{kind}:{resource}"
                                not in history.get("first_provider", {})
                                or utc(source["observed_start"])
                                < utc(history["first_provider"][f"{kind}:{resource}"])
                            )
                        )
                    )
                    for source in fresh
                ):
                    raise HourlyImportError("history_correction_replan_required")

    async def guard_controls(
        self,
        request: dict[str, Any],
        state: dict[str, Any] | None,
        index: dict[str, Any],
        provider_order: Any,
        provider_supersedes: Any,
        context: dict[str, Any],
    ) -> None:
        """Resolution policy changes require a sealed batch for every correction."""
        if not state or not index:
            return
        history = state["history"]
        prior = await self.writer.read_object(
            "operation", state["operation"]["object"], context
        )
        changed = (
            request["archive_policy"] != prior["request"]["archive_policy"]
            or (
                provider_order is not None
                and provider_order != history.get("provider_order", {})
            )
            or (
                provider_supersedes is not None
                and provider_supersedes != history.get("provider_supersedes", {})
            )
        )
        if not changed:
            return
        first, end = bounds(request["start"], request["end"])
        if any(
            key not in request["quantity_ids"] or not first <= utc(stamp) < end
            for key, hours in index.items()
            for stamp in hours
        ):
            raise HourlyImportError("history_correction_replan_required")

    async def originals(
        self, history: dict[str, Any], context: dict[str, Any]
    ) -> set[str]:
        originals: set[str] = set()
        references = {
            item["object"]
            for hours in history.get("corrections", {}).values()
            for item in hours.values()
        }
        for reference in references:
            document = await self.document(reference, context)
            originals.update(item["sha256"] for item in document["originals"])
        return originals

    async def protected_sources(
        self,
        history: dict[str, Any],
        sources: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> set[str]:
        """Keep the competing observations bound by active resolution witnesses."""
        dependencies = []
        documents: dict[str, Any] = {}
        for hours in history.get("corrections", {}).values():
            for reference in hours.values():
                object_id = reference["object"]
                if object_id not in documents:
                    documents[object_id] = await self.document(object_id, context)
                entry = documents[object_id]["entries"][reference["entry"]]
                descriptor = entry["descriptor"]
                sensor = descriptor["sensor_id"]
                dependencies.append(
                    (
                        "runtime_sensor"
                        if sensor is not None
                        else "runtime_thermostat",
                        sensor if sensor is not None else descriptor["thermostat_id"],
                        utc(entry["dependency_start"]),
                        utc(entry["dependency_end"]),
                    )
                )
        return {
            source["source_id"]
            for source in sources
            if any(
                (source["manifest"]["resource"], source["manifest"]["resource_id"])
                == (kind, resource)
                and utc(source["manifest"]["start"]) < end
                and utc(source["manifest"]["end"]) > start
                for kind, resource, start, end in dependencies
            )
        }
