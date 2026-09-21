"""Durable hourly writer owned and serialized by the config-entry importer.

Recorder and Store are independent owners. Every effect has a saved immutable
intent, and only exact readback followed by a saved checkpoint means success.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import fsum, isfinite
from typing import TYPE_CHECKING, Any

from .const import (
    DETAILED_RUNTIME_FIELDS,
    RUNTIME_FIELD_GROUPS,
    SUMMARY_MEAN_STATISTICS,
    SUMMARY_SUM_STATISTICS,
    THERMOSTAT_POINT_STATISTICS,
)
from .hourly_import_plan import (
    CumulativeCheckpoint,
    HourlyStatisticRow,
    RecorderSnapshot,
    SeriesImportPlan,
    hourly_base_id,
    plan_hourly_import,
    segment_id,
)
from .hourly_statistics import HourlySeries

if TYPE_CHECKING:
    from .hourly_history_writer import HistoryWriter

MARKER = "hourly_import_contract"
_VERSION = 1
_HOUR = timedelta(hours=1)
_MAX_HOURS = 366 * 24
_FIELDS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "unit_class",
    "mean_type",
    "has_sum",
)
_SAVES = "beestat_hourly_storage_tasks"
_SUCCESSOR = "_hourly_v2"
_THERMOSTAT_QUANTITIES = frozenset(
    (
        *(f"{key}_runtime_hours" for key, _label, _fields in RUNTIME_FIELD_GROUPS),
        *(f"{key}_runtime_hours" for key, _label, _field in DETAILED_RUNTIME_FIELDS),
        *(spec.statistic_suffix for spec in SUMMARY_MEAN_STATISTICS),
        *(spec.statistic_suffix for spec in SUMMARY_SUM_STATISTICS),
        *(spec.statistic_suffix for spec in THERMOSTAT_POINT_STATISTICS),
    )
)
_SENSOR_QUANTITIES = frozenset(
    {
        "temperature",
        "occupancy",
        "air_quality",
        "co2_concentration",
        "voc_concentration",
    }
)


class HourlyImportError(ValueError):
    """An explicit reconciliation or selection is required before more effects."""


class HourlyReconciliationError(HourlyImportError):
    """A valid saved hourly intent cannot yet prove its native Recorder effects."""


@dataclass(frozen=True, slots=True)
class WriterPartition:
    """Detached ownership projection; membership does not enable a quantity."""

    legacy_statistic_ids: frozenset[str]
    hourly_statistic_ids: frozenset[str]
    frozen_legacy_statistic_ids: frozenset[str]
    has_hourly: bool
    hourly_blocked_reason: str | None = None

    @property
    def hourly_ready(self) -> bool:
        return self.has_hourly and self.hourly_blocked_reason is None


def _time(value: str | datetime) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise HourlyImportError("An aware whole UTC hour is required")
    instant = instant.astimezone(UTC)
    if instant.minute or instant.second or instant.microsecond:
        raise HourlyImportError("An aware whole UTC hour is required")
    return instant


def _iso(value: datetime) -> str:
    return _time(value).isoformat()


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _row(row: HourlyStatisticRow | None) -> dict[str, Any] | None:
    return None if row is None else {**asdict(row), "start": _iso(row.start)}


def _native(row: dict[str, Any]) -> HourlyStatisticRow:
    return HourlyStatisticRow(**{**row, "start": _time(row["start"])})


def _metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if metadata is None else {key: metadata.get(key) for key in _FIELDS}


def _resource(value: dict[str, Any]) -> tuple[Any, ...]:
    return value.get("thermostat_id"), value.get("sensor_id"), value.get("quantity")


def _validate_resource(
    value: Any, *, allow_unmapped_sensor: bool = False
) -> tuple[int | None, int | None, str]:
    if not isinstance(value, dict):
        raise TypeError("Invalid source resource")
    thermostat_id, sensor_id, quantity = _resource(value)
    unmapped_sensor = (
        allow_unmapped_sensor and thermostat_id is None and sensor_id is not None
    )
    if (
        (not unmapped_sensor and (type(thermostat_id) is not int or thermostat_id <= 0))
        or (sensor_id is not None and (type(sensor_id) is not int or sensor_id <= 0))
        or not isinstance(quantity, str)
        or quantity
        not in (_THERMOSTAT_QUANTITIES if sensor_id is None else _SENSOR_QUANTITIES)
    ):
        raise ValueError("Invalid source resource")
    return thermostat_id, sensor_id, quantity


def _validated_records(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep every committed and intended owner; a segment may only keep its owner."""
    records = dict(state["series"])
    selection = state.get("pending_selection")
    if selection is not None:
        for base, record in selection["records"].items():
            if base in records and _resource(record["resource"]) != _resource(
                records[base]["resource"]
            ):
                raise ValueError("Pending selection changes an adopted resource")
            records[base] = record
    owned: set[tuple[int | None, int | None, str]] = set()
    parents: dict[int, int | None] = {}
    for record in records.values():
        resource = _validate_resource(record["resource"])
        if resource in owned:
            raise ValueError("Multiple hourly writers own the same quantity")
        owned.add(resource)
        thermostat_id, sensor_id, _quantity = resource
        if sensor_id is not None:
            if sensor_id in parents and parents[sensor_id] != thermostat_id:
                raise ValueError("An adopted sensor has conflicting parents")
            parents[sensor_id] = thermostat_id
    return records


def _partition_resources(
    identity: dict[str, Any],
) -> tuple[dict[tuple[int | None, int | None, str], str], dict[int, int | None]]:
    current: dict[tuple[int | None, int | None, str], str] = {}
    parents: dict[int, int | None] = {}
    for base, value in identity["resources"].items():
        resource = _validate_resource(value, allow_unmapped_sensor=True)
        thermostat_id, sensor_id, _quantity = resource
        if hourly_base_id(base) != base or resource in current:
            raise ValueError("Ambiguous current quantity identity")
        current[resource] = base
        if sensor_id is not None:
            if sensor_id in parents and parents[sensor_id] != thermostat_id:
                raise ValueError("Configured sensor has conflicting parents")
            parents[sensor_id] = thermostat_id
    supplied_parents = identity.get("sensor_parents", parents)
    if not isinstance(supplied_parents, dict):
        raise TypeError("Invalid observed sensor parents")
    for sensor_id, thermostat_id in supplied_parents.items():
        if (
            type(sensor_id) is not int
            or sensor_id <= 0
            or (
                thermostat_id is not None
                and (type(thermostat_id) is not int or thermostat_id <= 0)
            )
            or (sensor_id in parents and parents[sensor_id] != thermostat_id)
        ):
            raise ValueError("Invalid observed sensor parents")
    return current, {**parents, **supplied_parents}


def _eligible_keys(
    resources: Mapping[str, dict[str, Any]] | None,
) -> set[tuple[int | None, int | None, str]] | None:
    return (
        None
        if resources is None
        else {
            _validate_resource(resource, allow_unmapped_sensor=True)
            for resource in resources.values()
        }
    )


def _source_start(record: dict[str, Any], ordinary_start: datetime | None) -> datetime:
    start = _time(record["epoch_start"])
    if ordinary_start is not None and not (
        record["metadata"].get("has_sum") and record["checkpoint"] is None
    ):
        start = max(start, _time(ordinary_start))
    return start


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

    @property
    def _document(self) -> dict[str, Any]:
        state = self._legacy_state
        if state is None:
            raise HourlyImportError("Hourly state has not been adopted")
        return state

    @property
    def _legacy_state(self) -> dict[str, Any] | None:
        if self._state is not None and self._state.get("version") == 3:
            from .hourly_history_writer import legacy_document  # noqa: PLC0415

            return legacy_document(self._state)
        return self._state

    def _history_writer(self) -> HistoryWriter:
        from .hourly_history_writer import HistoryWriter  # noqa: PLC0415

        return HistoryWriter(self)

    @property
    def has_pending_history(self) -> bool:
        """Tell the entry worker whether an accepted v3 operation can advance."""
        return bool(self.history_status().get("has_pending"))

    def history_status(self) -> dict[str, Any]:
        from .hourly_history_writer import status  # noqa: PLC0415

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
            if state["token"] == getattr(self, "_invalidated_history_token", None):
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
        fence = {"kind": "history_invalidated_root", "token": state["token"]}
        try:
            raw = await self._store.async_read_object("operation", _digest(fence))
        except FileNotFoundError:
            self._admit()
            # Absence was observed before this await completed. A save or fence
            # may have finished meanwhile without changing this manager's cache.
            if (
                self._hass.data.get(_SAVES, {}).get(self._entry.entry_id)
                is not previous
            ):
                raise HourlyImportError("history_root_changed") from None
            if state["token"] == getattr(self, "_invalidated_history_token", None):
                raise HourlyImportError("history_root_invalidated") from None
            return
        except Exception as err:
            self._invalidated_history_token = state["token"]
            raise HourlyImportError("history_invalidation_fence_unreadable") from err
        self._admit()
        self._invalidated_history_token = state["token"]
        if sha256(raw).hexdigest() != _digest(fence):
            raise HourlyImportError("history_invalidation_fence_corrupt")
        raise HourlyImportError("history_root_invalidated")

    async def _invalidate_history_root(self, token: str) -> str:
        """Persist a deterministic fence in the existing immutable operation owner."""
        self._admit()
        if self.has_pending_store_save():
            raise HourlyImportError("history_store_write_pending")
        fence = {"kind": "history_invalidated_root", "token": token}
        raw = json.dumps(
            fence, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        expected = _digest(fence)
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
        if isinstance(state, dict) and state.get("version") == 3:
            from .hourly_history_writer import validate_root  # noqa: PLC0415

            validate_root(self, state)
            return
        self._validate_legacy(state)

    def _validate_legacy(self, state: Any) -> None:
        try:
            body = {key: value for key, value in state.items() if key != "integrity"}
            valid = (
                state["integrity"] == _digest(body)
                and type(state["version"]) is int
                and state["version"] == _VERSION
                and state["entry_id"] == self._entry.entry_id
                and type(state["revision"]) is int
                and state["revision"] >= 0
                and state["phase"] in {"prepared", "hourly"}
                and isinstance(state["series"], dict)
                and isinstance(state["token"], str)
                and len(state["token"]) == 64
            )
            if not valid:
                raise ValueError("Invalid journal header")
            for base, record in state["series"].items():
                self._validate_record(base, record)
            pending = state.get("pending")
            if pending is not None:
                self._validate_pending(pending, state)
            selection = state.get("pending_selection")
            if selection is not None:
                for base, record in selection["records"].items():
                    self._validate_record(base, record)
                if (
                    not selection["records"]
                    or sorted(selection["records"]) != selection["statistic_ids"]
                    or type(selection["expected_revision"]) is not int
                    or not 0 <= selection["expected_revision"] <= state["revision"]
                    or any(
                        record["epoch_start"] != selection["epoch_start"]
                        for record in selection["records"].values()
                    )
                ):
                    raise ValueError("Invalid pending selection")
            if state["phase"] == "prepared" and (
                state["series"] or selection is None or pending is not None
            ):
                raise ValueError("Invalid prepared selection")
            if state["phase"] == "hourly" and not state["series"]:
                raise ValueError("Adopted state has no writers")
            identity = state["identity"]
            if (
                identity["entry_id"] != self._entry.entry_id
                or not isinstance(identity["api_base"], str)
                or not identity["api_base"]
                or not isinstance(identity["account_anchors"], list)
                or not identity["account_anchors"]
                or any(
                    not isinstance(anchor, str) or not anchor
                    for anchor in identity["account_anchors"]
                )
            ):
                raise ValueError("Invalid adopted source identity")
            _validated_records(state)
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise HourlyImportError(
                "Invalid hourly state; reconstruction requires explicit reconciliation"
            ) from err

    def _validate_record(self, base: str, record: dict[str, Any]) -> None:
        epoch = _time(record["epoch_start"])
        _validate_resource(record["resource"])
        if (
            hourly_base_id(base) != base
            or hourly_base_id(record["statistic_id"]) != base
            or record["metadata"]["statistic_id"] != record["statistic_id"]
            or any(key not in record["metadata"] for key in _FIELDS)
            or len(record["coverage"]) > _MAX_HOURS + 1
        ):
            raise ValueError("Invalid adopted series")
        if record["statistic_id"] != base and record["statistic_id"] != segment_id(
            base, epoch
        ):
            raise ValueError("Segment epoch mismatch")
        for start, evidence in record["coverage"].items():
            instant = _time(start)
            row = evidence["row"]
            if evidence["reason"] == "verified":
                value = evidence["value"]
                if (
                    type(value) not in (int, float)
                    or not isfinite(value)
                    or row is None
                    or _native(row).cleared
                    or _native(row).start != instant
                ):
                    raise ValueError("Coverage has no verified numeric row")
            elif row is not None or evidence["value"] is not None:
                raise ValueError("Unverified coverage has numeric values")
        checkpoint = record["checkpoint"]
        if checkpoint is not None:
            row = _native(checkpoint)
            if row.cleared or row.start < epoch:
                raise ValueError("Invalid checkpoint")
            evidence = record["coverage"].get(checkpoint["start"])
            if evidence is not None and evidence["row"] != checkpoint:
                raise ValueError("Checkpoint disagrees with saved coverage")
        if record["blocked_from"] is not None:
            _time(record["blocked_from"])

    def _validate_pending(self, pending: dict[str, Any], state: dict[str, Any]) -> None:
        valid = (
            pending["digest"]
            == _digest(
                {key: value for key, value in pending.items() if key != "digest"}
            )
            and len(pending["before"]) <= _MAX_HOURS + 1
            and 0 < len(pending["rows"]) <= _MAX_HOURS
            and pending["statistic_id"]
            == state["series"][pending["base_id"]]["statistic_id"]
        )
        if not valid:
            raise ValueError("Invalid pending intent")
        start = _time(pending["start"])
        for collection in (pending["before"], pending["rows"]):
            instants = [_native(row).start for row in collection]
            if instants != sorted(set(instants)) or any(
                instant < start for instant in instants
            ):
                raise ValueError("Invalid pending row order or range")
        self._validate_record(pending["base_id"], pending["after_series"])
        if _resource(pending["after_series"]["resource"]) != _resource(
            state["series"][pending["base_id"]]["resource"]
        ):
            raise ValueError("Pending effect changes the writer resource")

    async def _save(self, value: dict[str, Any]) -> None:
        self._admit()
        if value.get("version") == _VERSION and (self._state or {}).get("version") == 3:
            from .hourly_history_writer import wrap_legacy_save  # noqa: PLC0415

            assert self._state is not None
            value = wrap_legacy_save(self._state, value)
        state = deepcopy(value)
        state.pop("integrity", None)
        state["integrity"] = _digest(state)
        self._validate(state)
        # A new intent must suppress before yielding. Completion, including
        # removal of that suppression, is visible only after verified persistence.
        if (
            state.get("pending")
            or state.get("pending_selection")
            or (state.get("legacy_v2") or {}).get("pending_selection")
            or (state.get("version") == 3 and state.get("phase") == "prepared")
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

    async def async_mode(self) -> str:
        """Resolve the authoritative marker before choosing a writer."""
        await self._load()
        marker = self._entry.data.get(MARKER)
        if self._state is None:
            return "legacy"
        if self._state.get("version") == 3:
            if (
                marker != {"version": 3, "token": self._state["token"]}
                or self._state["phase"] != "hourly"
            ):
                raise HourlyImportError("History marker and journal disagree")
            if self._legacy_state is None:
                return "history"
            if (
                self.has_pending_history
                or (self._state.get("pending") or {}).get("generation") == 3
                or self._document.get("pending_selection")
            ):
                raise HourlyImportError("History operation owns the shared writer")
            return "hourly"
        if self._state.get("pending_selection") or self._document["phase"] != "hourly":
            raise HourlyImportError(
                "An interrupted explicit hourly selection must be retried"
            )
        if marker != {"version": _VERSION, "token": self._document["token"]}:
            self._error = "adoption_unverified"
            raise HourlyImportError(
                "Hourly adoption marker and Store disagree; legacy writes are blocked"
            )
        if self._error == "adoption_unverified":
            self._error = None
        return "hourly"

    async def async_writer_partition(self, identity: dict[str, Any]) -> WriterPartition:
        """Read ownership without replaying a selection, saving or querying Recorder.

        The caller supplies currently eligible resource identities and, when
        available, the complete observed sensor-parent map. Disabled or removed
        quantities keep their saved reservation. A verified interrupted selection
        reserves its quantities immediately; unrelated legacy quantities remain eligible.
        """
        await self._load()
        if self._state is not None and self._state.get("version") == 3:
            from .hourly_history_writer import partition  # noqa: PLC0415

            return partition(self, identity)
        return self._legacy_writer_partition(identity)

    def _legacy_writer_partition(self, identity: dict[str, Any]) -> WriterPartition:
        if self._state is not None:
            self._identity(identity, require_resources=False)
        elif identity.get("entry_id") != self._entry.entry_id:
            self._error = "source_identity_unverified"
            raise HourlyImportError("Writer partition config-entry identity changed")
        try:
            current, parents = _partition_resources(identity)
            resources = identity["resources"]
            if self._state is None:
                return WriterPartition(
                    frozenset(base.removesuffix(_SUCCESSOR) for base in resources),
                    frozenset(),
                    frozenset(),
                    False,
                )
            marker = self._entry.data.get(MARKER)
            expected = {"version": _VERSION, "token": self._document["token"]}
            pending_selection = self._document.get("pending_selection")
            prepared_without_marker = (
                self._document["phase"] == "prepared"
                and pending_selection is not None
                and marker is None
            )
            if marker != expected and not prepared_without_marker:
                self._error = "adoption_unverified"
                raise HourlyImportError(
                    "Hourly adoption marker and Store disagree; legacy writes are blocked"
                )
            records = _validated_records(self._document)
            hourly_ids: set[str] = set()
            frozen: set[str] = set()
            for base, record in records.items():
                resource = _validate_resource(record["resource"])
                thermostat_id, sensor_id, _quantity = resource
                if (
                    sensor_id is not None
                    and sensor_id in parents
                    and parents[sensor_id] != thermostat_id
                ):
                    self._error = "source_resource_rebound"
                    raise HourlyImportError("An adopted sensor parent changed")
                current_base = current.get(resource)
                if base in resources and _resource(resources[base]) != resource:
                    self._error = "source_resource_rebound"
                    raise HourlyImportError("An adopted legacy identity was rebound")
                frozen.add(base.removesuffix(_SUCCESSOR))
                if current_base is not None:
                    hourly_ids.add(current_base)
                    frozen.add(current_base.removesuffix(_SUCCESSOR))
            return WriterPartition(
                frozenset(
                    base.removesuffix(_SUCCESSOR)
                    for base in resources
                    if base not in hourly_ids
                ),
                frozenset(hourly_ids),
                frozenset(frozen),
                True,
                "selection_pending" if pending_selection is not None else None,
            )
        except HourlyImportError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError) as err:
            self._error = "source_identity_unverified"
            raise HourlyImportError("Writer partition identity is invalid") from err

    def base_statistic_ids(self) -> tuple[str, ...]:
        return tuple((self._legacy_state or {}).get("series", {}))

    def bootstrap_start(
        self,
        *,
        thermostat_id: int | None = None,
        eligible_resources: Mapping[str, dict[str, Any]] | None = None,
    ) -> datetime | None:
        """Include an explicit cumulative epoch until its first verified checkpoint."""
        eligible = _eligible_keys(eligible_resources)
        epochs = [
            _time(record["epoch_start"])
            for record in (self._legacy_state or {}).get("series", {}).values()
            if record["metadata"].get("has_sum")
            and record["checkpoint"] is None
            and (eligible is None or _resource(record["resource"]) in eligible)
            and (
                thermostat_id is None
                or record["resource"]["thermostat_id"] == thermostat_id
            )
        ]
        return min(epochs, default=None)

    def source_starts(
        self,
        resources: Mapping[str, dict[str, Any]],
        *,
        start: datetime,
        end: datetime,
        ordinary_start: datetime | None = None,
    ) -> dict[str, datetime]:
        """Bind current IDs to cached source bounds before quality is computed."""
        start, end = _time(start), _time(end)
        ordinary = start if ordinary_start is None else _time(ordinary_start)
        records = {
            _resource(record["resource"]): record
            for record in (self._legacy_state or {}).get("series", {}).values()
        }
        return {
            statistic_id: min(
                end,
                max(
                    start,
                    _source_start(record, ordinary_start)
                    if (record := records.get(_resource(resource))) is not None
                    else ordinary,
                ),
            )
            for statistic_id, resource in resources.items()
        }

    def status(self) -> dict[str, Any]:
        """Return a detached cached status; never fetch provider or Recorder data."""
        state = self._legacy_state or {}
        return {
            "history_v3": self.history_status(),
            "mode": state.get("phase", "unloaded" if not self._loaded else "legacy"),
            "revision": state.get("revision", 0),
            "pending": bool(state.get("pending") or state.get("pending_selection")),
            "error": self._error,
            "series": {
                base: {
                    **{
                        key: deepcopy(record[key])
                        for key in (
                            "statistic_id",
                            "epoch_start",
                            "blocked_from",
                            "closed",
                        )
                    },
                    "coverage_incomplete": not record["coverage"]
                    or any(
                        value["reason"] != "verified"
                        for value in record["coverage"].values()
                    ),
                }
                for base, record in state.get("series", {}).items()
            },
        }

    def _identity(
        self,
        identity: dict[str, Any],
        *,
        compare: bool = True,
        require_resources: bool = True,
    ) -> None:
        if (
            identity.get("entry_id") != self._entry.entry_id
            or not identity.get("api_base")
            or not identity.get("account_anchors")
            or (require_resources and not identity.get("resources"))
        ):
            self._error = "source_identity_unverified"
            raise HourlyImportError(
                "Hourly imports require a verified account and resource identity"
            )
        if compare and self._state is not None:
            old = self._state["identity"]
            if (
                old["entry_id"] != identity["entry_id"]
                or old["api_base"] != identity["api_base"]
                or not set(old["account_anchors"]) & set(identity["account_anchors"])
            ):
                self._error = "source_identity_changed"
                raise HourlyImportError("Adopted account identity changed")

    def _bound_series(
        self,
        series: tuple[HourlySeries, ...],
        identity: dict[str, Any],
        ordinary_start: datetime | None,
        eligible_resources: Mapping[str, dict[str, Any]] | None = None,
    ) -> dict[str, HourlySeries]:
        self._identity(identity)
        by_resource: dict[tuple[Any, ...], HourlySeries] = {}
        for source_item in series:
            resource = identity["resources"].get(source_item.statistic_id)
            if resource is None or _resource(resource) in by_resource:
                raise HourlyImportError("Missing or ambiguous source resource identity")
            by_resource[_resource(resource)] = source_item
        eligible = _eligible_keys(eligible_resources)
        result = {}
        for base, record in self._document["series"].items():
            if eligible is not None and _resource(record["resource"]) not in eligible:
                # Settings can disable a quantity without releasing its identity.
                continue
            item = by_resource.get(_resource(record["resource"]))
            if item is None:
                selected = identity.get("selected_thermostat_id")
                if (
                    selected is not None
                    and record["resource"].get("thermostat_id") != selected
                ):
                    continue
                self._error = "source_resource_missing"
                raise HourlyImportError("An adopted source resource is missing")
            metadata = {**item.metadata, "statistic_id": record["statistic_id"]}
            if _metadata(metadata) != _metadata(record["metadata"]):
                self._error = "source_quantity_changed"
                raise HourlyImportError("Adopted quantity or unit contract changed")
            start = _source_start(record, ordinary_start)
            result[base] = replace(
                item,
                metadata=metadata,
                hours=tuple(hour for hour in item.hours if hour.start >= start),
            )
        return result

    async def async_reconcile(self) -> None:
        """Drain and reconcile a saved batch before acquiring fresh source data."""
        await self._load()
        if self._state is not None and self._state.get("version") == 3:
            pending = self._state.get("pending")
            if pending and pending.get("generation") == 3:
                raise HourlyImportError(
                    "Use the guarded history worker to reconcile v3"
                )
            if self._legacy_state is None or not self._legacy_state.get("pending"):
                return
        if self._state is None or self._state.get("pending") is None:
            return
        if await self.async_mode() != "hourly":
            raise HourlyImportError("Pending Recorder intent has no adopted writer")
        pending = self._document["pending"]
        snapshot = await self._recorder.async_snapshot(
            pending["statistic_id"], _time(pending["start"])
        )
        retry = self._compare(pending, snapshot)
        if retry:
            self._admit()
            self._recorder.submit(
                pending["metadata"], tuple(_native(row) for row in retry)
            )
            snapshot = await self._recorder.async_snapshot(
                pending["statistic_id"], _time(pending["start"])
            )
            if self._compare(pending, snapshot):
                raise HourlyReconciliationError("Recorder intent is still incomplete")
        after = deepcopy(self._document)
        after["series"][pending["base_id"]] = pending["after_series"]
        after["pending"] = None
        after["revision"] += 1
        await self._save(after)
        self._error = None

    def _compare(
        self, pending: dict[str, Any], snapshot: RecorderSnapshot
    ) -> list[dict[str, Any]]:
        if not snapshot.complete:
            raise HourlyReconciliationError("Incomplete Recorder readback")
        metadata = _metadata(snapshot.metadata)
        if metadata not in (pending["before_metadata"], _metadata(pending["metadata"])):
            self._error = "recorder_metadata_conflict"
            raise HourlyReconciliationError(
                "Recorder metadata changed during pending intent"
            )
        before = {row["start"]: row for row in pending["before"]}
        current = {_iso(row.start): _row(row) for row in snapshot.rows}
        intended = {row["start"]: row for row in pending["rows"]}
        retry = []
        for start in before.keys() | current.keys() | intended.keys():
            actual, prior = current.get(start), before.get(start)
            if start in intended and actual == intended[start]:
                continue
            if actual != prior:
                self._error = "recorder_row_conflict"
                raise HourlyReconciliationError(
                    "Recorder contains a third state; pending intent was retained"
                )
            if start in intended:
                retry.append(intended[start])
        if metadata is None and current:
            raise HourlyReconciliationError("Recorder rows have no metadata")
        return sorted(retry, key=lambda row: row["start"])

    async def _write(
        self,
        base: str,
        record: dict[str, Any],
        snapshot: RecorderSnapshot,
        start: datetime,
        rows: tuple[HourlyStatisticRow, ...],
        after_record: dict[str, Any],
        suppress_from: datetime | None,
    ) -> None:
        existing = {row.start: row for row in snapshot.rows}
        rows = tuple(row for row in rows if existing.get(row.start) != row)
        if not rows:
            after = deepcopy(self._document)
            after["series"][base] = after_record
            after["revision"] += 1
            await self._save(after)
            return
        if len(rows) > _MAX_HOURS or len(snapshot.rows) > _MAX_HOURS + 1:
            raise HourlyImportError("Repair exceeds the bounded hourly batch")
        rows = tuple(sorted(rows, key=lambda row: row.start))
        pending = {
            "base_id": base,
            "statistic_id": record["statistic_id"],
            "start": _iso(start),
            "metadata": deepcopy(record["metadata"]),
            "before_metadata": _metadata(snapshot.metadata),
            "before": [_row(row) for row in snapshot.rows],
            "rows": [_row(row) for row in rows],
            "after_series": after_record,
            "suppress_from": None if suppress_from is None else _iso(suppress_from),
        }
        pending["digest"] = _digest(pending)
        state = deepcopy(self._document)
        state["pending"] = pending
        await self._save(state)
        # Reacquire before the first enqueue as well as on restart: Store awaits
        # may have allowed an independent Recorder writer to change the target.
        await self.async_reconcile()

    async def async_import(
        self,
        series: tuple[HourlySeries, ...],
        identity: dict[str, Any],
        *,
        ordinary_start: datetime | None = None,
        eligible_resources: Mapping[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if await self.async_mode() != "hourly":
            raise HourlyImportError(
                "Hourly statistics have not been explicitly adopted"
            )
        await self.async_reconcile()
        bound = self._bound_series(series, identity, ordinary_start, eligible_resources)
        self._error = None
        imported_rows = imported_series = 0
        latest = {}
        for base, item in bound.items():
            if not item.hours:
                if item.blocked_reason or item.rejected_timestamps:
                    self._error = "source_series_unverified"
                    raise HourlyImportError(
                        "Adopted hourly source identity or coverage is invalid"
                    )
                continue
            published = await self._import_series(base, item)
            count = len(published)
            if count:
                imported_series += 1
                imported_rows += count
                latest[item.statistic_id] = _iso(max(published))
        return {
            "imported_series": imported_series,
            "imported_rows": imported_rows,
            "latest_start_by_statistic_id": latest,
        }

    async def _import_series(
        self, base: str, item: HourlySeries
    ) -> dict[datetime, HourlyStatisticRow]:
        """Reconcile and publish exactly one adopted quantity."""
        record = self._document["series"][base]
        start = item.hours[0].start - _HOUR
        checkpoint = record["checkpoint"]
        if item.metadata.get("has_sum"):
            if checkpoint is not None:
                start = min(start, _time(checkpoint["start"]))
            else:
                start = min(start, _time(record["epoch_start"]) - _HOUR)
            if record["blocked_from"] is not None:
                start = min(start, _time(record["blocked_from"]) - _HOUR)
        snapshot = await self._recorder.async_snapshot(record["statistic_id"], start)
        if not snapshot.complete or (
            snapshot.metadata is not None
            and _metadata(snapshot.metadata) != _metadata(record["metadata"])
        ):
            self._error = "recorder_snapshot_conflict"
            raise HourlyImportError("Incomplete or incompatible Recorder snapshot")
        existing = {row.start: row for row in snapshot.rows}
        if (
            checkpoint is not None
            and _row(existing.get(_time(checkpoint["start"]))) != checkpoint
        ):
            self._error = "checkpoint_conflict"
            raise HourlyImportError(
                "The saved verified checkpoint is missing or changed"
            )
        for instant, row in existing.items():
            saved = record["coverage"].get(_iso(instant), {}).get("row")
            if checkpoint is not None and checkpoint["start"] == _iso(instant):
                saved = checkpoint
            if (not row.cleared or saved is not None) and _row(row) != saved:
                self._error = "unowned_recorder_effect"
                raise HourlyImportError(
                    "Recorder effects are not explained by the saved journal"
                )
        predecessor = (
            record["coverage"].get(_iso(item.hours[0].start - _HOUR), {}).get("row")
        )
        epoch = _time(record["epoch_start"])
        trusted = None if predecessor is None else _native(predecessor)
        # Presence of an older native row alone is not saved continuity proof.
        verified = None if checkpoint is None else _time(checkpoint["start"])
        if (
            item.metadata.get("has_sum")
            and item.hours[0].start != epoch
            and trusted is None
        ):
            verified = None
        plan = plan_hourly_import(
            (item,),
            snapshots={item.statistic_id: snapshot},
            checkpoints={
                item.statistic_id: CumulativeCheckpoint(epoch, verified, trusted)
            },
        )[0]
        rows, after_record, suppress, published = self._prepare_batch(
            record, item, snapshot, plan
        )
        await self._write(
            base, record, snapshot, start, tuple(rows), after_record, suppress
        )
        return published

    def _prepare_batch(
        self,
        record: dict[str, Any],
        item: HourlySeries,
        snapshot: RecorderSnapshot,
        plan: SeriesImportPlan,
    ) -> tuple[
        tuple[HourlyStatisticRow, ...],
        dict[str, Any],
        datetime | None,
        dict[datetime, HourlyStatisticRow],
    ]:
        """Choose corrections or full suffix invalidation from a proven snapshot."""
        existing = {row.start: row for row in snapshot.rows}
        after_record = deepcopy(record)
        coverage = after_record["coverage"]
        calculated = {row.start: row for row in plan.calculated_rows}
        changed = [
            instant
            for instant, row in calculated.items()
            if row != existing.get(instant)
        ]
        cumulative = bool(item.metadata.get("has_sum"))
        blocked = bool(set(plan.blocking_reasons) - {"surviving_stale_rows"})
        suppress = None
        rows = plan.calculated_rows
        if cumulative and (blocked or plan.stale_starts):
            suppress = self._invalidation_boundary(record, item, plan, changed)
            rows = tuple(
                HourlyStatisticRow(row.start)
                for row in snapshot.rows
                if row.start >= suppress and not row.cleared
            )
            after_record["blocked_from"] = _iso(suppress)
            after_record["checkpoint"] = self._boundary_checkpoint(
                record, existing, suppress
            )
        elif not cumulative:
            if blocked:
                suppress = item.hours[0].start
                rows = ()
                stale = {
                    row.start
                    for row in snapshot.rows
                    if item.hours[0].start <= row.start <= item.hours[-1].start
                    and not row.cleared
                }
            else:
                stale = set(plan.stale_starts)
                if stale:
                    suppress = min(stale)
            rows = (
                *rows,
                *(HourlyStatisticRow(instant) for instant in sorted(stale)),
            )
        else:
            self._advance_checkpoint(after_record, rows, calculated)
        published = {row.start: row for row in rows if not row.cleared}
        for hour in item.hours:
            verified_row = published.get(hour.start)
            if cumulative and suppress is not None and hour.start < suppress:
                old = coverage.get(_iso(hour.start), {})
                verified_row = (
                    existing.get(hour.start)
                    if old.get("row") == _row(existing.get(hour.start))
                    else None
                )
            coverage[_iso(hour.start)] = {
                "reason": "verified"
                if verified_row is not None
                else (
                    hour.reason if hour.reason != "ready" else "continuity_unverified"
                ),
                "valid_slots": hour.valid_slots,
                "missing_slots": hour.missing_slots,
                "invalid_slots": hour.invalid_slots,
                "duplicate_slots": hour.duplicate_slots,
                "value": (hour.values or {}).get("increment" if cumulative else "mean")
                if verified_row is not None
                else None,
                "row": _row(verified_row),
            }
        if suppress is not None and cumulative:
            for instant, evidence in coverage.items():
                if _time(instant) >= suppress:
                    evidence.update(
                        reason="continuity_unverified", value=None, row=None
                    )
        after_record["coverage"] = dict(sorted(coverage.items())[-(_MAX_HOURS + 1) :])
        return rows, after_record, suppress, published

    def _boundary_checkpoint(
        self,
        record: dict[str, Any],
        existing: dict[datetime, HourlyStatisticRow],
        boundary: datetime,
    ) -> dict[str, Any] | None:
        """Retain only the saved, native-verified row immediately before a gap."""
        prior = boundary - _HOUR
        saved: dict[str, Any] | None = (
            record["coverage"].get(_iso(prior), {}).get("row")
        )
        checkpoint = record["checkpoint"]
        if (
            saved is None
            and checkpoint is not None
            and _time(checkpoint["start"]) == prior
        ):
            saved = checkpoint
        return saved if saved == _row(existing.get(prior)) else None

    def _advance_checkpoint(
        self,
        record: dict[str, Any],
        rows: tuple[HourlyStatisticRow, ...],
        calculated: dict[datetime, HourlyStatisticRow],
    ) -> None:
        """Clear a prior gap only when this verified calculation includes it."""
        boundary = record["blocked_from"]
        if boundary is None or _time(boundary) in calculated:
            record["blocked_from"] = None
        if rows and (
            record["checkpoint"] is None
            or rows[-1].start >= _time(record["checkpoint"]["start"])
        ):
            record["checkpoint"] = _row(rows[-1])

    def _invalidation_boundary(
        self,
        record: dict[str, Any],
        item: HourlySeries,
        plan: SeriesImportPlan,
        changed: list[datetime],
    ) -> datetime:
        """Rolling source windows cannot move an unresolved boundary forward."""
        boundaries = [plan.continuity_break or item.hours[0].start, *changed]
        if record["blocked_from"] is not None:
            boundaries.append(_time(record["blocked_from"]))
        checkpoint = record["checkpoint"]
        next_unverified = (
            _time(record["epoch_start"])
            if checkpoint is None
            else _time(checkpoint["start"]) + _HOUR
        )
        if next_unverified < item.hours[0].start:
            boundaries.append(next_unverified)
        return min(boundaries)

    def _admit_legacy_selection(
        self, ids: tuple[str, ...], identity: dict[str, Any]
    ) -> None:
        if self._state is not None and self._state.get("version") == 3:
            from .hourly_history_contract import quantity_id  # noqa: PLC0415

            if (
                self._state["operation"]["status"] != "completed"
                or (self._state.get("pending") or {}).get("generation") == 3
            ):
                raise HourlyImportError("History operation owns the shared writer")
            if self._entry.data.get(MARKER) != {
                "version": 3,
                "token": self._state["token"],
            }:
                raise HourlyImportError("History marker and journal disagree")
            selected = self._state["history"]["selections"]
            if any(
                quantity_id(identity["resources"][base]) in selected
                for base in ids
                if base in identity["resources"]
            ):
                raise HourlyImportError("Physical quantity already has a v3 owner")

    async def async_select(
        self,
        series: tuple[HourlySeries, ...],
        identity: dict[str, Any],
        *,
        epoch_start: datetime,
        statistic_ids: tuple[str, ...],
        expected_revision: int,
        preview_digest: str | None = None,
    ) -> dict[str, Any]:
        """Preview or apply an explicit exact selection, never infer an epoch."""
        await self._load()
        self._admit_legacy_selection(statistic_ids, identity)
        self._identity(identity)
        epoch = _time(epoch_start)
        if epoch >= datetime.now(UTC).replace(minute=0, second=0, microsecond=0):
            raise HourlyImportError("Selected epoch must be an observed closed hour")
        if not statistic_ids or len(statistic_ids) != len(set(statistic_ids)):
            raise HourlyImportError("Select unique explicit base statistic IDs")
        self._validate_selection_identity(series, identity)
        replay = await self._selection_replay(
            epoch, statistic_ids, expected_revision, preview_digest
        )
        if replay is not None:
            return replay
        revision = (self._state or {}).get("revision", 0)
        if expected_revision != revision:
            raise HourlyImportError("Hourly selection revision changed; preview again")
        available, identity = self._selection_sources(series, identity)
        records = {}
        snapshots = {}
        preview = []
        for base in sorted(statistic_ids):
            record, snapshot, projection = await self._preview_record(
                base, available, identity, epoch
            )
            records[base] = record
            if snapshot is not None:
                snapshots[base] = snapshot
            preview.append(projection)
        adopted = {
            **(
                {} if self._legacy_state is None else _validated_records(self._document)
            ),
            **records,
        }
        frozen_resources = {
            _resource(record["resource"]) for record in adopted.values()
        }
        frozen_legacy_ids = {base.removesuffix(_SUCCESSOR) for base in adopted}
        self._include_history_reservations(frozen_resources, frozen_legacy_ids)
        continuing_legacy_ids = set()
        for base, resource in identity["resources"].items():
            if _resource(resource) in frozen_resources:
                frozen_legacy_ids.add(base.removesuffix(_SUCCESSOR))
            else:
                continuing_legacy_ids.add(base.removesuffix(_SUCCESSOR))
        proposal = {
            "expected_revision": revision,
            "epoch_start": _iso(epoch),
            "statistic_ids": sorted(statistic_ids),
            "identity": {
                key: identity[key]
                for key in ("entry_id", "api_base", "account_anchors")
            },
            "selection": preview,
            "legacy_writes": {
                "policy": "per_quantity",
                "frozen_statistic_ids": sorted(frozen_legacy_ids),
                "continuing_statistic_ids": sorted(continuing_legacy_ids),
            },
            "unselected_series": sorted(
                base
                for base in available
                if _resource(identity["resources"][base]) not in frozen_resources
            ),
        }
        digest = _digest(proposal)
        if preview_digest is None:
            return {"status": "preview", "preview_digest": digest, **proposal}
        if preview_digest != digest:
            raise HourlyImportError(
                "Hourly preview changed; review a new preview before applying"
            )
        await self._clear_closed_suffixes(snapshots)
        state = (
            deepcopy(self._document)
            if self._legacy_state is not None
            else {
                "version": _VERSION,
                "entry_id": self._entry.entry_id,
                "phase": "prepared",
                "revision": 0,
                "token": digest,
                "identity": deepcopy(identity),
                "series": {},
                "pending": None,
            }
        )
        state["pending_selection"] = {
            "digest": digest,
            "expected_revision": expected_revision,
            "epoch_start": _iso(epoch),
            "statistic_ids": sorted(statistic_ids),
            "records": records,
        }
        await self._save(state)
        return await self._finish_selection()

    def _include_history_reservations(
        self, resources: set[tuple[Any, ...]], aliases: set[str]
    ) -> None:
        for selection in (
            (self._state or {}).get("history", {}).get("selections", {}).values()
        ):
            descriptor = selection["descriptor"]
            resources.add(_resource(descriptor))
            aliases.update(descriptor["legacy_statistic_ids"])

    async def _clear_closed_suffixes(
        self, snapshots: dict[str, RecorderSnapshot]
    ) -> None:
        """Close a segment only after every stale row through retained end clears."""
        for base, snapshot in snapshots.items():
            old = self._document["series"][base]
            boundary = _time(old["blocked_from"])
            if (
                not snapshot.complete
                or (snapshot.metadata is None and snapshot.rows)
                or (
                    snapshot.metadata is not None
                    and _metadata(snapshot.metadata) != _metadata(old["metadata"])
                )
            ):
                raise HourlyImportError(
                    "Old segment snapshot is incomplete or incompatible"
                )
            rows = tuple(
                HourlyStatisticRow(row.start)
                for row in snapshot.rows
                if not row.cleared
            )
            if rows:
                await self._write(
                    base, old, snapshot, boundary, rows, deepcopy(old), boundary
                )

    def _selection_sources(
        self, series: tuple[HourlySeries, ...], identity: dict[str, Any]
    ) -> tuple[dict[str, HourlySeries], dict[str, Any]]:
        """Display slug changes cannot allocate a second owner for a bound quantity."""
        identities = deepcopy(identity)
        resources = identities["resources"]
        owned = {
            _resource(record["resource"]): base
            for base, record in (self._legacy_state or {}).get("series", {}).items()
        }
        available = {}
        for item in series:
            resource = resources.get(item.statistic_id)
            if resource is None:
                raise HourlyImportError("Source identity is missing")
            base = owned.get(_resource(resource), item.statistic_id)
            if base in available:
                raise HourlyImportError("Source identity is ambiguous")
            available[base] = replace(
                item, metadata={**item.metadata, "statistic_id": base}
            )
            resources[base] = resource
        return available, identities

    def _validate_selection_identity(
        self, series: tuple[HourlySeries, ...], identity: dict[str, Any]
    ) -> None:
        """An explicit retry cannot rebind its saved resources or quantity units."""
        pending = (self._legacy_state or {}).get("pending_selection")
        if pending is None:
            return
        current = {
            _resource(identity["resources"][item.statistic_id]): item
            for item in series
            if item.statistic_id in identity["resources"]
        }
        for record in pending["records"].values():
            item = current.get(_resource(record["resource"]))
            if item is None or _metadata(
                {**item.metadata, "statistic_id": record["statistic_id"]}
            ) != _metadata(record["metadata"]):
                self._error = "selection_identity_changed"
                raise HourlyImportError(
                    "Interrupted selection resource or quantity changed"
                )

    async def _selection_replay(
        self, epoch: datetime, ids: tuple[str, ...], revision: int, digest: str | None
    ) -> dict[str, Any] | None:
        """Resume only the exact explicit selection after either owner's interruption."""
        if self._legacy_state is None:
            return None
        pending = self._document.get("pending_selection")
        previous = pending or self._document.get("last_selection")
        matches = bool(
            previous
            and digest == previous["digest"]
            and revision == previous["expected_revision"]
            and _iso(epoch) == previous["epoch_start"]
            and sorted(ids) == previous["statistic_ids"]
        )
        if pending:
            if not matches:
                raise HourlyImportError(
                    "Retry the exact interrupted selection before another action"
                )
            return await self._finish_selection()
        if matches and self._entry.data.get(MARKER) is None:
            self._hass.config_entries.async_update_entry(
                self._entry,
                data={
                    **self._entry.data,
                    MARKER: {"version": _VERSION, "token": self._document["token"]},
                },
            )
        await self.async_mode()
        await self.async_reconcile()
        return {"status": "already_selected", **self.status()} if matches else None

    async def _preview_record(
        self,
        base: str,
        available: dict[str, HourlySeries],
        identity: dict[str, Any],
        epoch: datetime,
    ) -> tuple[dict[str, Any], RecorderSnapshot | None, dict[str, Any]]:
        """Bind one selected resource and reject any unowned native successor."""
        snapshot: RecorderSnapshot | None = None
        if (
            hourly_base_id(base) != base
            or base not in available
            or base not in identity["resources"]
        ):
            raise HourlyImportError(
                "Select a currently configured base hourly statistic"
            )
        item = available[base]
        first = next((hour for hour in item.hours if hour.start == epoch), None)
        if (
            item.blocked_reason
            or item.rejected_timestamps
            or first is None
            or first.reason != "ready"
            or first.valid_slots != 12
        ):
            raise HourlyImportError(
                "Selected epoch needs a complete observed hour with trusted units"
            )
        old = (self._legacy_state or {}).get("series", {}).get(base)
        target = base
        if old is not None:
            if (
                old["blocked_from"] is None
                or epoch <= _time(old["blocked_from"])
                or epoch <= _time(old["epoch_start"])
            ):
                raise HourlyImportError(
                    "A new segment requires an unresolved boundary and an explicit later complete epoch"
                )
            if _resource(old["resource"]) != _resource(identity["resources"][base]):
                raise HourlyImportError("Selected resource identity changed")
            target = segment_id(base, epoch)
        native = await self._recorder.async_snapshot(target, epoch - _HOUR)
        if not native.complete or native.metadata is not None or native.rows:
            raise HourlyImportError(
                "Selected successor ID collides with existing Recorder identity"
            )
        closed = [] if old is None else deepcopy(old["closed"])
        if old is not None:
            boundary = _time(old["blocked_from"])
            snapshot = await self._recorder.async_snapshot(
                old["statistic_id"], boundary
            )
            closed.append(
                {
                    "statistic_id": old["statistic_id"],
                    "epoch_start": old["epoch_start"],
                    "end": old["blocked_from"],
                }
            )
        record = {
            "statistic_id": target,
            "metadata": {**item.metadata, "statistic_id": target},
            "resource": deepcopy(identity["resources"][base]),
            "epoch_start": _iso(epoch),
            "checkpoint": None,
            "blocked_from": None,
            "coverage": {},
            "closed": closed,
        }
        preview = {
            "base_id": base,
            "statistic_id": target,
            "epoch_start": _iso(epoch),
            "metadata": _metadata(record["metadata"]),
            "resource": record["resource"],
            "first_hour": first.values,
            "closed": closed,
            "stale_rows": [
                _row(row)
                for row in (snapshot or RecorderSnapshot()).rows
                if not row.cleared
            ],
        }
        return record, snapshot, preview

    async def _finish_selection(self) -> dict[str, Any]:
        selection = self._document["pending_selection"]
        for record in selection["records"].values():
            native = await self._recorder.async_snapshot(
                record["statistic_id"], _time(record["epoch_start"]) - _HOUR
            )
            if not native.complete or native.metadata is not None or native.rows:
                raise HourlyImportError(
                    "Interrupted selection now collides with a Recorder identity"
                )
        self._admit()
        marker = {"version": _VERSION, "token": self._document["token"]}
        if self._state is not None and self._state.get("version") == 3:
            marker = {"version": 3, "token": self._state["token"]}
        current = self._entry.data.get(MARKER)
        if current is not None and current != marker:
            raise HourlyImportError("Hourly adoption marker changed")
        if current is None:
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, MARKER: marker}
            )
        after = deepcopy(self._document)
        after["series"].update(selection["records"])
        after["phase"] = "hourly"
        after["revision"] += 1
        after["last_selection"] = {
            key: value for key, value in selection.items() if key != "records"
        }
        after["pending_selection"] = None
        await self._save(after)
        return {"status": "selected", **self.status()}

    def coverage(
        self,
        *,
        start: datetime,
        end: datetime,
        statistic_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Expose verified observations and a complete-observed-hour denominator."""
        start, end = _time(start), _time(end)
        if not start < end or end - start > timedelta(days=366):
            raise HourlyImportError(
                "Coverage needs a positive window of at most 366 days"
            )
        state = self._legacy_state or {}
        pending = state.get("pending")
        pending_starts = (
            set() if pending is None else {row["start"] for row in pending["rows"]}
        )
        open_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        output = {}
        for base, record in state.get("series", {}).items():
            if (
                statistic_ids is not None
                and base not in statistic_ids
                and record["statistic_id"] not in statistic_ids
            ):
                continue
            rows, observed = [], []
            instant = start
            while instant < end:
                evidence = record["coverage"].get(_iso(instant), {})
                reason, value = evidence.get("reason", "missing"), evidence.get("value")
                if instant >= open_hour:
                    reason, value = "provisional", None
                if instant < _time(record["epoch_start"]):
                    reason, value = "outside_active_segment", None
                if self._error or state.get("pending_selection"):
                    reason, value = "state_unverified", None
                elif (
                    pending
                    and pending["base_id"] == base
                    and (
                        (
                            pending["suppress_from"] is not None
                            and instant >= _time(pending["suppress_from"])
                        )
                        or _iso(instant) in pending_starts
                    )
                ):
                    reason, value = "pending_reconciliation", None
                if reason == "verified" and value is not None:
                    observed.append(value)
                else:
                    value = None
                rows.append(
                    {
                        "start": _iso(instant),
                        "value": value,
                        "coverage": reason,
                        **{
                            key: evidence[key]
                            for key in (
                                "valid_slots",
                                "missing_slots",
                                "invalid_slots",
                                "duplicate_slots",
                            )
                            if key in evidence
                        },
                    }
                )
                instant += _HOUR
            scale = max((abs(value) for value in observed), default=0.0)
            average = (
                scale * (fsum(value / scale for value in observed) / len(observed))
                if scale
                else 0.0
                if observed
                else None
            )
            output[base] = {
                "statistic_id": record["statistic_id"],
                "unit_of_measurement": record["metadata"]["unit_of_measurement"],
                "epoch_start": record["epoch_start"],
                "closed_segments": deepcopy(record["closed"]),
                "hours": rows,
                "complete_observed_hours": len(observed),
                "requested_hours": len(rows),
                "complete": len(observed) == len(rows),
                "observed_hour_average": average,
            }
        return {
            "start": _iso(start),
            "end": _iso(end),
            "revision": state.get("revision", 0),
            "series": output,
        }
