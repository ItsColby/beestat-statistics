"""Native Recorder boundary for the importer-owned hourly write protocol.

The caller owns serialization, durable intent and checkpoint authority. A queued
submission is not completion: only a later complete snapshot establishes effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from math import isfinite
from typing import Any, Literal, cast

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from .hourly_import_plan import HourlyStatisticRow, RecorderSnapshot

MAX_SNAPSHOT_ROWS = 366 * 24 + 1  # A maximum source window plus its exact predecessor.
_FIELDS = ("mean", "min", "max", "state", "sum")
_METADATA_FIELDS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "unit_class",
    "mean_type",
    "has_sum",
)
type _NativeStatisticType = Literal[
    "change", "last_reset", "mean", "min", "max", "state", "sum"
]


class HourlyRecorderError(ValueError):
    """A native result cannot establish a complete and usable hourly snapshot."""


class HourlyRecorder:
    """Adapt supported Recorder APIs without selecting epochs or owning retries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_barrier(self) -> None:
        """Fence even the last running task; cancellation does not cancel its effect."""

        synchronized: asyncio.Future[None] = self._hass.loop.create_future()
        get_instance(self._hass).queue_task(SynchronizeTask(synchronized))
        await asyncio.shield(synchronized)

    async def async_snapshot(
        self, statistic_id: str, start: datetime
    ) -> RecorderSnapshot:
        """Read every retained row at or after the explicit inclusive UTC bound.

        Callers include the exact predecessor in this bound when needed. There is
        no end bound, row truncation, display conversion or derived change query.
        """

        _validate_id(statistic_id)
        start = _utc_hour(start)
        await self.async_barrier()
        return await get_instance(self._hass).async_add_executor_job(
            partial(self._snapshot, statistic_id, start)
        )

    async def async_known_ids(self) -> set[str]:
        """Return the complete Beestat metadata inventory after preceding imports."""

        await self.async_barrier()
        native = await get_instance(self._hass).async_add_executor_job(
            partial(get_metadata, self._hass, statistic_source="beestat")
        )
        if not isinstance(native, Mapping):
            raise HourlyRecorderError("invalid_metadata_projection")
        for statistic_id, value in native.items():
            _metadata_value(statistic_id, value)
        return set(native)

    def submit(
        self, metadata: dict[str, Any], rows: tuple[HourlyStatisticRow, ...]
    ) -> None:
        """Queue one fully validated batch; an all-null row is a start-only upsert."""

        metadata = _validated_metadata(metadata.get("statistic_id"), metadata)
        if len(rows) > MAX_SNAPSHOT_ROWS:
            raise HourlyRecorderError("hourly_batch_too_large")
        fields = _supported_fields(metadata)
        payload: list[StatisticData] = []
        previous: datetime | None = None
        for row in rows:
            start = _utc_hour(row.start)
            if previous is not None and start <= previous:
                raise HourlyRecorderError("unordered_hourly_batch")
            previous = start
            values: dict[str, Any] = {"start": start}
            for field in _FIELDS:
                value = getattr(row, field)
                if value is None:
                    continue
                if field not in fields:
                    raise HourlyRecorderError("unsupported_statistic_field")
                values[field] = _number(value)
            payload.append(cast(StatisticData, values))
        if payload:
            async_add_external_statistics(
                self._hass, cast(StatisticMetaData, metadata), payload
            )

    def _snapshot(self, statistic_id: str, start: datetime) -> RecorderSnapshot:
        """Keep metadata, projection and metadata recheck in one executor job."""

        native_metadata = get_metadata(self._hass, statistic_ids={statistic_id})
        metadata = _single_metadata(statistic_id, native_metadata)
        fields = _supported_fields(metadata) if metadata is not None else set(_FIELDS)
        units = (
            {metadata["unit_class"]: metadata["unit_of_measurement"]}
            if metadata is not None
            and metadata["unit_class"] is not None
            and metadata["unit_of_measurement"] is not None
            else None
        )
        native_rows = statistics_during_period(
            self._hass,
            start,
            None,
            {statistic_id},
            "hour",
            units,
            cast(set[_NativeStatisticType], fields),
        )
        if not isinstance(native_rows, Mapping) or set(native_rows) - {statistic_id}:
            raise HourlyRecorderError("invalid_statistics_projection")
        rows = native_rows.get(statistic_id, [])
        if not isinstance(rows, list):
            raise HourlyRecorderError("invalid_statistics_projection")
        if len(rows) > MAX_SNAPSHOT_ROWS:
            raise HourlyRecorderError("recorder_snapshot_too_large")
        if metadata is None and rows:
            raise HourlyRecorderError("rows_without_metadata")
        detached = tuple(_detached_row(row, fields) for row in rows)
        previous: datetime | None = None
        for row in detached:
            if row.start < start or (previous is not None and row.start <= previous):
                raise HourlyRecorderError("unordered_recorder_snapshot")
            previous = row.start
        # A metadata change while reading cannot authorize a mixed projection.
        after = get_metadata(self._hass, statistic_ids={statistic_id})
        if _single_metadata(statistic_id, after) != metadata:
            raise HourlyRecorderError("metadata_changed_during_snapshot")
        return RecorderSnapshot(detached, metadata, complete=True)


def _single_metadata(statistic_id: str, native: Any) -> dict[str, Any] | None:
    if not isinstance(native, Mapping) or set(native) - {statistic_id}:
        raise HourlyRecorderError("invalid_metadata_projection")
    if statistic_id not in native:
        return None
    return _metadata_value(statistic_id, native[statistic_id])


def _metadata_value(statistic_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise HourlyRecorderError("invalid_metadata_projection")
    metadata_id, metadata = value
    if isinstance(metadata_id, bool) or not isinstance(metadata_id, int):
        raise HourlyRecorderError("invalid_metadata_projection")
    return _validated_metadata(statistic_id, metadata)


def _validated_metadata(statistic_id: Any, metadata: Any) -> dict[str, Any]:
    _validate_id(statistic_id)
    if not isinstance(metadata, Mapping) or any(
        field not in metadata for field in _METADATA_FIELDS
    ):
        raise HourlyRecorderError("incomplete_metadata_projection")
    if metadata["statistic_id"] != statistic_id or metadata["source"] != "beestat":
        raise HourlyRecorderError("incompatible_metadata_identity")
    if any(
        metadata[field] is not None and not isinstance(metadata[field], str)
        for field in ("unit_of_measurement", "unit_class")
    ):
        raise HourlyRecorderError("invalid_metadata_unit")
    mean_type = metadata["mean_type"]
    if (
        isinstance(mean_type, bool)
        or not isinstance(mean_type, int)
        or mean_type not in (0, 1, 2)
        or not isinstance(metadata["has_sum"], bool)
    ):
        raise HourlyRecorderError("invalid_metadata_quantity")
    return dict(metadata)


def _supported_fields(metadata: Mapping[str, Any]) -> set[str]:
    fields = set(_FIELDS[:3]) if metadata["mean_type"] else set()
    if metadata["has_sum"]:
        fields.update(_FIELDS[3:])
    if not fields:
        raise HourlyRecorderError("metadata_without_statistic_fields")
    return fields


def _detached_row(native: Any, fields: set[str]) -> HourlyStatisticRow:
    if not isinstance(native, Mapping) or "start" not in native:
        raise HourlyRecorderError("invalid_statistics_projection")
    if any(field not in native for field in fields):
        raise HourlyRecorderError("incomplete_statistics_projection")
    try:
        start = _utc_hour(datetime.fromtimestamp(_number(native["start"]), UTC))
    except (OverflowError, OSError, ValueError) as err:
        raise HourlyRecorderError("invalid_statistic_timestamp") from err
    values: dict[str, Any] = {"start": start}
    for field in _FIELDS:
        value = native.get(field)
        if value is not None and field not in fields:
            raise HourlyRecorderError("unsupported_statistic_projection")
        values[field] = None if value is None else _number(value)
    return HourlyStatisticRow(**values)


def _validate_id(statistic_id: Any) -> None:
    if (
        not isinstance(statistic_id, str)
        or not statistic_id.startswith("beestat:")
        or not valid_statistic_id(statistic_id)
    ):
        raise HourlyRecorderError("invalid_beestat_statistic_id")


def _utc_hour(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise HourlyRecorderError("invalid_statistic_timestamp")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise HourlyRecorderError("invalid_statistic_timestamp")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HourlyRecorderError("invalid_statistic_number")
    try:
        number = float(value)
    except OverflowError as err:
        raise HourlyRecorderError("invalid_statistic_number") from err
    if not isfinite(number):
        raise HourlyRecorderError("invalid_statistic_number")
    return number
