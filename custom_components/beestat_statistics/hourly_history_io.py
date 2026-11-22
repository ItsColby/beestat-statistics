"""Guarded I/O and binding that every part of the history writer shares.

Each read re-guards the entry before its result is used: Store objects are
hash-verified, native snapshots are bounded and the durable root is checked
against the entry marker. The supplied manager owns all state.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial
from hashlib import sha256
from typing import Any, cast

from .config_model import build_sensor_statistics
from .hourly_history_contract import (
    MAX_BATCH_HOURS,
    METHOD_VERSION,
    canonical_json,
    digest,
    require_digest,
    utc,
)
from .hourly_history_root import _ROOT_VERSIONS, _identity_matches
from .hourly_import_core import MARKER, HourlyImportError
from .hourly_statistics import _thermostat_quantities

_FIELDS = ("entry_id", "api_base", "account_anchors")


def _config_digest(config: Any) -> str:
    """Digest only what decides which statistics a routine operation imports.

    The digest is derived from the owners, not a field list: the thermostat
    quantities ``build_hourly_statistics`` creates (``_thermostat_quantities``
    per thermostat id/slug/name) and the specs ``build_sensor_statistics``
    returns, with each sensor's owning thermostat. A new spec condition
    therefore enters the binding automatically. Entity mappings, filter state,
    saved overrides, timing and source details that no spec reads stay out.
    Rows are sorted so ordering never moves the digest.
    """
    owners = {item.sensor_id: item.thermostat_id for item in config.sensors}
    return digest(
        {
            "thermostats": sorted(
                (
                    {
                        "id": item.thermostat_id,
                        "quantities": [
                            asdict(quantity)
                            for quantity in _thermostat_quantities(item.slug, item.name)
                        ],
                    }
                    for item in config.thermostats
                ),
                key=lambda row: row["id"],
            ),
            "sensors": sorted(
                (
                    {**asdict(spec), "thermostat_id": owners[spec.sensor_id]}
                    for spec in build_sensor_statistics(config)
                ),
                key=lambda row: (row["sensor_id"], row["statistic_suffix"]),
            ),
        }
    )


def _binding_matches(
    sealed_binding: dict[str, Any], current: dict[str, Any], context: dict[str, Any]
) -> bool:
    """Compare a sealed binding under the form it was sealed with.

    Records sealed before the binding dropped the process-local timezone
    revision still carry it; it is never compared. A sealed record that stores
    ``config_revision`` predates ``config_digest`` and keeps comparing the
    whole configuration response; newer records compare the digest only.
    """
    stored = {k: v for k, v in sealed_binding.items() if k != "timezone_revision"}
    if "config_revision" in stored:
        current = {
            **{k: v for k, v in current.items() if k != "config_digest"},
            "config_revision": context["config_revision"],
        }
    return stored == current


class WriterIO:
    """Guarded Store, root and native reads; the supplied manager owns all state."""

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
                state.get("previous_marker")
                if state["version"] in _ROOT_VERSIONS
                else None
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
        raw = await self.cpu(canonical_json, value, context=context)
        reference = await self.manager._store.async_write_object(kind, raw)
        await self.guard(context)
        if (
            reference != sha256(raw).hexdigest()
            or canonical_json(await self.read_object(kind, reference, context)) != raw
        ):
            raise HourlyImportError("history_object_write_unverified")
        return cast(str, reference)

    def binding(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "identity": {key: deepcopy(context["identity"][key]) for key in _FIELDS},
            "config_digest": _config_digest(context["config"]),
            "timezone": context["timezone"],
            "method_version": METHOD_VERSION,
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
