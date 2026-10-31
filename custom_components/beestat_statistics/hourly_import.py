"""Durable hourly writer owned and serialized by the config-entry importer.

Recorder and Store are independent owners. Every effect has a saved immutable
intent, and only exact readback followed by a saved checkpoint means success.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from typing import Any

from .const import HOURLY_V2_SUFFIX
from .hourly_history_contract import canonical_json, digest
from .hourly_history_writer import HistoryWriter, partition, status, validate_root
from .hourly_import_core import (
    MARKER,
    HourlyImportError,
    WriterPartition,
    _history_fence,
    _partition_resources,
)

_HOUR = timedelta(hours=1)
_SAVES = "beestat_hourly_storage_tasks"


class HourlyImportManager:
    """One entry's journal; caller must hold the existing importer lock."""

    def __init__(
        self, hass: Any, entry: Any, *, store: Any = None, recorder: Any = None
    ) -> None:
        if store is None:
            from .hourly_storage import (  # noqa: PLC0415 - native dependency boundary
                HourlyStore,
            )

            store = HourlyStore(hass, entry.entry_id)
        if recorder is None:
            from .hourly_recorder import (  # noqa: PLC0415 - native dependency boundary
                HourlyRecorder,
            )

            recorder = HourlyRecorder(hass)
        self._hass, self._entry = hass, entry
        self._store, self._recorder = store, recorder
        self._state: dict[str, Any] | None = None
        self._loaded = False
        self._closed = False
        self._error: str | None = None
        self._invalidated_history_token: str | None = None

    def _history_writer(self) -> HistoryWriter:
        return HistoryWriter(self)

    @property
    def has_pending_history(self) -> bool:
        """Tell the entry worker whether an accepted v3 operation can advance."""
        return bool(self.history_status().get("has_pending"))

    def history_status(self) -> dict[str, Any]:
        result = status(self._state)
        state = self._state
        if state is None and (self._entry.data.get(MARKER) or {}).get("version") == 3:
            result.update(
                status="blocked",
                error=self._error or "history_state_unavailable",
                has_pending=False,
            )
        if state and state.get("version") == 3:
            if self.has_pending_store_save():
                result.update(
                    status="blocked",
                    error="history_store_write_pending",
                    has_pending=False,
                )
            if state["token"] == self._invalidated_history_token:
                result.update(
                    status="blocked",
                    error="history_root_invalidated",
                    has_pending=False,
                )
            marker = self._entry.data.get(MARKER)
            expected = {"version": 3, "token": state["token"]}
            prepared = state["phase"] == "prepared" and marker == state.get(
                "previous_marker"
            )
            if marker != expected and not prepared:
                result.update(
                    status="blocked", error="history_marker_mismatch", has_pending=False
                )
        return result

    async def async_plan_history(
        self, request: dict[str, Any], *, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Read sealed source and bounded native snapshots without effects."""
        return await self._history_writer().plan_response(request, context)

    async def async_accept_history(
        self, request: dict[str, Any], *, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Durably accept the exact reviewed manifest; the entry worker advances it."""
        return await self._history_writer().accept(request, context)

    async def async_advance_history(self, *, context: dict[str, Any]) -> dict[str, Any]:
        """Reconcile and commit at most one quantity/month under this journal."""
        return await self._history_writer().advance(context)

    async def async_refresh_history(
        self, request: dict[str, Any], *, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept ordinary provider corrections under the adopted bounded policy."""
        return await self._history_writer().refresh(request, context)

    async def async_collect_history_objects(self, *, context: dict[str, Any]) -> int:
        """Delete proof/operation generations the committed root no longer needs."""
        return await self._history_writer().collect(context)

    async def async_history_source_points(
        self, request: dict[str, Any], *, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Read the committed indexed point baseline for bounded delta evaluation."""
        return await self._history_writer().source_points(request, context)

    async def async_history_material(
        self, request: dict[str, Any], *, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Read detached proof and native rows for the pure logical query."""
        return await self._history_writer().material(request, context)

    def history_configuration(self, context: dict[str, Any]) -> dict[str, Any]:
        """Project capabilities and cached reservations without I/O."""
        return self._history_writer().configuration(context)

    async def async_check_history_material(
        self, material: dict[str, Any], *, context: dict[str, Any]
    ) -> None:
        """Reject a query snapshot superseded during background projection."""
        await self._history_writer().check_material(material, context)

    def history_quantity_ids(self) -> tuple[str, ...]:
        """Return only cached physically adopted/reserved v3 quantities."""
        state = self._state or {}
        if state.get("version") != 3:
            return ()
        return tuple(sorted(state["history"]["selections"]))

    def has_pending_store_save(self) -> bool:
        """Keep the sole save-task owner inside the journal manager."""
        task = self._hass.data.get(_SAVES, {}).get(self._entry.entry_id)
        return task is not None and not task.done()

    async def _check_history_root_fence(self, state: dict[str, Any] | None) -> None:
        """A durable immutable fence rejects a predecessor even with an old marker."""
        if state is None or state.get("version") != 3:
            return
        if self.has_pending_store_save():
            raise HourlyImportError("history_store_write_pending")
        previous = self._hass.data.get(_SAVES, {}).get(self._entry.entry_id)
        if (
            previous is not None
            and getattr(previous, "_beestat_history_fence", None) == state["token"]
        ):
            self._invalidated_history_token = state["token"]
            if previous.cancelled() or previous.exception() is not None:
                raise HourlyImportError("history_invalidation_fence_unverified")
        fence = _history_fence(state["token"])
        try:
            raw = await self._store.async_read_object("operation", digest(fence))
        except FileNotFoundError:
            self._admit()
            # Absence was observed before this await completed. A save or fence
            # may have finished meanwhile without changing this manager's cache.
            if (
                self._hass.data.get(_SAVES, {}).get(self._entry.entry_id)
                is not previous
            ):
                raise HourlyImportError("history_root_changed") from None
            if state["token"] == self._invalidated_history_token:
                raise HourlyImportError("history_root_invalidated") from None
            return
        except Exception as err:
            self._invalidated_history_token = state["token"]
            raise HourlyImportError("history_invalidation_fence_unreadable") from err
        self._admit()
        self._invalidated_history_token = state["token"]
        if sha256(raw).hexdigest() != digest(fence):
            raise HourlyImportError("history_invalidation_fence_corrupt")
        raise HourlyImportError("history_root_invalidated")

    async def _invalidate_history_root(self, token: str) -> str:
        """Persist a deterministic fence in the existing immutable operation owner."""
        self._admit()
        if self.has_pending_store_save():
            raise HourlyImportError("history_store_write_pending")
        fence = _history_fence(token)
        raw = canonical_json(fence)
        expected = digest(fence)
        self._invalidated_history_token = token
        task = self._hass.async_create_task(self._persist_history_fence(raw, expected))
        task._beestat_history_fence = token
        self._hass.data.setdefault(_SAVES, {})[self._entry.entry_id] = task
        try:
            await asyncio.shield(task)
            self._admit()
        except BaseException:
            self._loaded = False
            self._error = "history_invalidation_fence_unverified"
            raise
        return expected

    async def _persist_history_fence(self, raw: bytes, expected: str) -> None:
        """One tracked task owns write plus readback even if its caller cancels."""
        reference = await self._store.async_write_object("operation", raw)
        self._admit()
        stored = await self._store.async_read_object("operation", expected)
        self._admit()
        if reference != expected or stored != raw:
            raise HourlyImportError("history_invalidation_fence_unverified")

    def close(self) -> None:
        """Stop admission immediately; surviving storage/Recorder work is drained."""
        self._closed = True

    def _admit(self) -> None:
        if self._closed:
            raise HourlyImportError("Hourly importer is unloaded")

    async def _load(self) -> None:
        self._admit()
        if self._loaded:
            await self._check_history_root_fence(self._state)
            return
        saves = self._hass.data.setdefault(_SAVES, {})
        previous = saves.get(self._entry.entry_id)
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except Exception:  # noqa: BLE001 - prior uncertain save is resolved from disk
                self._error = "previous_hourly_save_unverified"
            if (
                saves.get(self._entry.entry_id) is previous
                and getattr(previous, "_beestat_history_fence", None) is None
            ):
                saves.pop(self._entry.entry_id)
        self._admit()
        try:
            state = await self._store.async_load()
            if state is not None:
                self._validate(state)
                await self._check_history_root_fence(state)
            if state is None and self._entry.data.get(MARKER) is not None:
                raise HourlyImportError(
                    "Adopted hourly state is missing; legacy writes are blocked"
                )
            self._state = deepcopy(state)
            self._loaded = True
            self._error = None
        except Exception as err:
            self._error = "hourly_state_unavailable"
            raise HourlyImportError(
                "Hourly state is missing, corrupt or unsupported"
            ) from err

    def _validate(self, state: Any) -> None:
        validate_root(self, state)

    async def _save(self, value: dict[str, Any]) -> None:
        self._admit()
        state = deepcopy(value)
        state.pop("integrity", None)
        state["integrity"] = digest(state)
        self._validate(state)
        # A new intent must suppress before yielding. Completion, including
        # removal of that suppression, is visible only after verified persistence.
        if state.get("pending") or (
            state.get("version") == 3 and state.get("phase") == "prepared"
        ):
            self._state = state
        task = self._hass.async_create_task(self._store.async_save(deepcopy(state)))
        self._hass.data.setdefault(_SAVES, {})[self._entry.entry_id] = task
        try:
            await asyncio.shield(task)
            self._admit()
            self._state = state
        except BaseException:
            self._loaded = False
            self._error = "hourly_save_unverified"
            raise

    async def async_writer_partition(self, identity: dict[str, Any]) -> WriterPartition:
        """Read ownership without replaying a selection, saving or querying Recorder.

        The caller supplies currently eligible resource identities and, when
        available, the complete observed sensor-parent map. Disabled or removed
        quantities keep their saved reservation. A verified interrupted selection
        reserves its quantities immediately; unrelated legacy quantities remain eligible.
        """
        await self._load()
        if self._state is None:
            if identity.get("entry_id") != self._entry.entry_id:
                raise HourlyImportError(
                    "Writer partition config-entry identity changed"
                )
            _partition_resources(identity)
            return WriterPartition(
                frozenset(
                    base.removesuffix(HOURLY_V2_SUFFIX)
                    for base in identity["resources"]
                ),
                False,
            )
        return partition(self, identity)

    def status(self) -> dict[str, Any]:
        """Return a detached cached status; never fetch provider or Recorder data."""
        state = self._state or {}
        return {
            "history_v3": self.history_status(),
            "mode": "history" if state else "legacy" if self._loaded else "unloaded",
            "revision": state.get("revision", 0),
            "pending": self.has_pending_history or bool(state.get("pending")),
            "error": self._error,
        }
