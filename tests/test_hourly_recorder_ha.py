"""Native Recorder contracts for qualified hourly history and retained daily rows."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.beestat_statistics import hourly_recorder
from custom_components.beestat_statistics.hourly_recorder import (
    MAX_BOUNDED_SNAPSHOT_HOURS,
    HourlyRecorder,
    HourlyRecorderError,
)
from custom_components.beestat_statistics.hourly_rows import (
    HourlyStatisticRow,
)
from tests.hourly_support import hourly_metadata, known_beestat_ids

HOUR = timedelta(hours=1)
START = datetime(2026, 9, 10, 16, tzinfo=UTC)  # New York noon.
DAY_START = START - 12 * HOUR
NOW = datetime(2026, 9, 13, tzinfo=UTC)
RUNTIME_ID = "beestat:contract_fan_runtime_hours_hourly_v2"
RATE_ID = "beestat:contract_heat_runtime_rate_hourly_v3"


@pytest.fixture(autouse=True)
async def _started_recorder(recorder_mock: Any, freezer: Any) -> AsyncIterator[None]:
    """Use the harness's disposable SQLite and restore both timezone owners."""

    # recorder_mock must create the database before its own hass dependency.
    hass: HomeAssistant = recorder_mock.hass
    previous_zone = hass.config.time_zone
    previous_default = dt_util.get_default_time_zone()
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("America/New_York")
    try:
        await hass.async_start()
        yield
    finally:
        await hass.config.async_set_time_zone(previous_zone)
        dt_util.set_default_time_zone(previous_default)


def _counter_rows(start, totals):
    return [
        {"start": start + index * HOUR, "state": total, "sum": total}
        for index, total in enumerate(totals)
    ]


def _rate_metadata(statistic_id=RATE_ID, *, temperature_delta=False):
    return {
        "statistic_id": statistic_id,
        "source": "beestat",
        "name": "Independent hourly rate fixture",
        "unit_of_measurement": "°F" if temperature_delta else "%",
        "unit_class": "temperature_delta" if temperature_delta else "unitless",
        "mean_type": 1,
        "has_sum": False,
    }


async def _read(
    hass, statistic_ids, *, start=START, period="hour", types=None, drain=True
):
    if drain:
        await async_wait_recording_done(hass)
    return await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start,
            None,
            set(statistic_ids),
            period,
            None,
            {"state", "sum", "change"} if types is None else set(types),
        )
    )


async def _metadata_readback(hass, statistic_id):
    return await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )


async def test_start_only_measurement_upsert_clears_values_and_daily_omits_them(hass):
    statistic_id = "beestat:contract_temperature_hourly_v2"
    metadata = hourly_metadata(statistic_id, measurement=True)
    async_add_external_statistics(
        hass,
        metadata,
        [
            {
                "start": START + index * HOUR,
                "mean": value,
                "min": value - 1,
                "max": value + 1,
            }
            for index, value in enumerate((70.0, 100.0, 74.0))
        ],
    )
    original = (await _read(hass, {statistic_id}, types={"mean", "min", "max"}))[
        statistic_id
    ]
    metadata_before = await _metadata_readback(hass, statistic_id)
    for _replay in range(2):
        async_add_external_statistics(hass, metadata, [{"start": START + HOUR}])
        hourly = (await _read(hass, {statistic_id}, types={"mean", "min", "max"}))[
            statistic_id
        ]
        assert len(hourly) == 3
        assert hourly[0] == original[0]
        assert hourly[2] == original[2]
        assert [hourly[1][field] for field in ("mean", "min", "max")] == [None] * 3
        assert await _metadata_readback(hass, statistic_id) == metadata_before
    daily = (
        await _read(hass, {statistic_id}, period="day", types={"mean", "min", "max"})
    )[statistic_id]
    assert len(daily) == 1
    assert (daily[0]["mean"], daily[0]["min"], daily[0]["max"]) == (72, 69, 75)
    # Native reduction has no coverage field distinguishing this partial day.
    assert set(daily[0]) == {"start", "end", "mean", "min", "max"}


async def test_same_id_reset_cannot_repair_both_ranges_but_distinct_segment_can(hass):
    resumed = START + 3 * HOUR
    metadata = hourly_metadata(RUNTIME_ID)
    async_add_external_statistics(
        hass,
        metadata,
        [
            *_counter_rows(START, (0.75,)),
            {"start": START + HOUR},
            {"start": START + 2 * HOUR},
            {"start": resumed, "state": 0.25, "sum": 0.25, "last_reset": resumed},
        ],
    )
    types = {"sum", "change", "last_reset"}
    wide = (await _read(hass, {RUNTIME_ID}, types=types))[RUNTIME_ID]
    narrow = (await _read(hass, {RUNTIME_ID}, start=resumed, types=types))[RUNTIME_ID]
    assert wide[-1]["last_reset"] == resumed.timestamp()
    assert wide[-1]["change"] == -0.5
    assert narrow[0]["change"] == 0.25

    segment_id = f"{RUNTIME_ID}_e{resumed:%Y%m%dt%H%M%Sz}"
    async_add_external_statistics(
        hass, hourly_metadata(segment_id), _counter_rows(resumed, (0.25,))
    )
    for start in (START, resumed):
        segment = (await _read(hass, {segment_id}, start=start))[segment_id]
        assert len(segment) == 1
        assert segment[0]["change"] == 0.25
    segment_metadata = await _metadata_readback(hass, segment_id)
    async_add_external_statistics(
        hass, hourly_metadata(segment_id), _counter_rows(resumed, (0.25,))
    )
    assert (await _read(hass, {segment_id}))[segment_id] == segment
    assert await _metadata_readback(hass, segment_id) == segment_metadata
    # The segment's first increment is independent; this makes no assertion that
    # separate IDs provide a combined, complete day across the missing interval.


async def test_native_readback_distinguishes_partial_intent_and_third_state(hass):
    """Prove native readback distinguishability, not future Store recovery logic."""

    metadata = hourly_metadata(RUNTIME_ID)
    async_add_external_statistics(hass, metadata, _counter_rows(START, (0.25, 0.75)))
    prior = (await _read(hass, {RUNTIME_ID}, types={"state", "sum"}))[RUNTIME_ID]
    intended = [
        {**row, "state": total, "sum": total}
        for row, total in zip(prior, (0.5, 1.0), strict=True)
    ]
    # Separate native submissions need not have the same completion state.
    async_add_external_statistics(hass, metadata, _counter_rows(START, (0.5,)))
    partial_result = (await _read(hass, {RUNTIME_ID}, types={"state", "sum"}))[
        RUNTIME_ID
    ]
    assert partial_result[0] == intended[0]
    assert partial_result[1] == prior[1]
    assert partial_result != prior
    assert partial_result != intended

    # A later independent write is distinguishable from both captured states.
    async_add_external_statistics(hass, metadata, _counter_rows(START + HOUR, (0.875,)))
    conflicted = (await _read(hass, {RUNTIME_ID}, types={"state", "sum"}))[RUNTIME_ID]
    assert len(conflicted) == 2
    assert conflicted[0] == intended[0]
    assert conflicted[1] not in (prior[1], intended[1])
    assert conflicted[1]["sum"] == 0.875


async def test_cancelled_wait_requires_native_reconciliation_before_stable_replay(hass):
    recorder = get_instance(hass)
    paused = asyncio.Event()
    submitted = asyncio.Event()
    release = threading.Event()
    writer = None
    metadata = hourly_metadata(RUNTIME_ID)

    class GateRecorderTask(RecorderTask):
        def run(self, instance):
            instance.hass.loop.call_soon_threadsafe(paused.set)
            if not release.wait(timeout=10):
                raise TimeoutError("Recorder contract gate was not released")

    async def submit_and_wait():
        async_add_external_statistics(
            hass, metadata, _counter_rows(START, (0.25, 0.75))
        )
        submitted.set()
        await recorder.async_block_till_done()

    recorder.queue_task(GateRecorderTask())
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        writer = asyncio.create_task(submit_and_wait())
        await asyncio.wait_for(submitted.wait(), timeout=5)
        assert not writer.done()
        assert await _read(hass, {RUNTIME_ID}, drain=False) == {}
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert await _read(hass, {RUNTIME_ID}, drain=False) == {}
    finally:
        release.set()
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    # Cancellation of the waiter did not cancel the queued database import.
    reconciled = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    assert [row["sum"] for row in reconciled] == [0.25, 0.75]
    metadata_before = await _metadata_readback(hass, RUNTIME_ID)
    for _replay in range(2):
        async_add_external_statistics(
            hass, metadata, _counter_rows(START, (0.25, 0.75))
        )
        assert (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID] == reconciled
        assert await _metadata_readback(hass, RUNTIME_ID) == metadata_before


async def test_bounded_month_snapshot_includes_all_slots_without_predecessor_or_tail(
    hass,
):
    adapter = HourlyRecorder(hass)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    missing = await adapter.async_snapshot_range(RATE_ID, start, end)
    assert missing.complete and missing.metadata is None and not missing.rows
    rows = tuple(
        HourlyStatisticRow(start + index * HOUR, mean=125 + index / 10)
        for index in range(-1, MAX_BOUNDED_SNAPSHOT_HOURS + 2)
    )
    adapter.submit(_rate_metadata(), rows)
    original = hourly_recorder.statistics_during_period
    with patch.object(
        hourly_recorder, "statistics_during_period", wraps=original
    ) as read:
        snapshot = await adapter.async_snapshot_range(RATE_ID, start, end)
    assert snapshot.complete
    assert snapshot.rows == rows[1:-2]
    assert len(snapshot.rows) == MAX_BOUNDED_SNAPSHOT_HOURS
    assert read.call_args.args[1:5] == (start, end, {RATE_ID}, "hour")
    assert read.call_args.args[5:7] == (
        {"unitless": "%"},
        {"mean", "min", "max", "last_reset"},
    )
    assert all(row.min is None and row.max is None for row in snapshot.rows)
    assert all(row.sum is None and row.state is None for row in snapshot.rows)
    empty = await adapter.async_snapshot_range(RATE_ID, end + 2 * HOUR, end + 3 * HOUR)
    assert empty.complete and not empty.rows and empty.metadata == snapshot.metadata


async def test_mean_only_rate_correction_and_clear_leave_adjacent_hours_unchanged(hass):
    adapter = HourlyRecorder(hass)
    rows = tuple(
        HourlyStatisticRow(START + index * HOUR, mean=value)
        for index, value in enumerate((125.0, 175.0, 50.0))
    )
    metadata = _rate_metadata()
    adapter.submit(metadata, rows)
    initial = await adapter.async_snapshot_range(RATE_ID, START, START + 3 * HOUR)
    assert initial.rows == rows
    for corrected in (
        HourlyStatisticRow(START + HOUR, mean=150),
        HourlyStatisticRow(START + HOUR),
    ):
        adapter.submit(metadata, (corrected,))
        actual = await adapter.async_snapshot_range(RATE_ID, START, START + 3 * HOUR)
        assert actual.rows == (rows[0], corrected, rows[2])
        assert actual.metadata == initial.metadata
        one_hour = await adapter.async_snapshot_range(
            RATE_ID, START + HOUR, START + 2 * HOUR
        )
        assert one_hour.rows == (corrected,)


async def test_temperature_departure_uses_delta_conversion_and_native_mean_only_rows(
    hass,
):
    adapter = HourlyRecorder(hass)
    statistic_id = "beestat:contract_heating_temperature_departure_hourly_v3"
    metadata = _rate_metadata(statistic_id, temperature_delta=True)
    rows = (
        HourlyStatisticRow(START, mean=9),
        HourlyStatisticRow(START + HOUR, mean=18),
    )
    adapter.submit(metadata, rows)
    native = await adapter.async_snapshot_range(statistic_id, START, START + 2 * HOUR)
    assert native.rows == rows
    assert native.metadata["unit_class"] == "temperature_delta"
    assert native.metadata["unit_of_measurement"] == "°F"
    converted = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            START,
            START + 2 * HOUR,
            {statistic_id},
            "hour",
            {"temperature_delta": "°C"},
            {"mean", "min", "max"},
        )
    )
    assert [row["mean"] for row in converted[statistic_id]] == pytest.approx([5, 10])
    assert all(
        row["min"] is None and row["max"] is None for row in converted[statistic_id]
    )


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        (START, START, "invalid_snapshot_range"),
        (START, START - HOUR, "invalid_snapshot_range"),
        (START, START + 745 * HOUR, "invalid_snapshot_range"),
        (START + timedelta(minutes=1), START + HOUR, "invalid_statistic_timestamp"),
        (START, START + HOUR + timedelta(minutes=1), "invalid_statistic_timestamp"),
        (START.replace(tzinfo=None), START + HOUR, "invalid_statistic_timestamp"),
    ],
)
async def test_bounded_snapshot_rejects_invalid_bounds_before_native_work(
    hass, start, end, error
):
    adapter = HourlyRecorder(hass)
    with (
        patch.object(adapter, "async_barrier") as barrier,
        pytest.raises(HourlyRecorderError, match=error),
    ):
        await adapter.async_snapshot_range(RATE_ID, start, end)
    barrier.assert_not_called()


async def test_bounded_snapshot_does_not_certify_out_of_range_native_result(hass):
    adapter = HourlyRecorder(hass)
    adapter.submit(_rate_metadata(), (HourlyStatisticRow(START, mean=125),))
    with (
        patch.object(
            hourly_recorder,
            "statistics_during_period",
            return_value={
                RATE_ID: [
                    {
                        "start": (START + HOUR).timestamp(),
                        "mean": 125,
                        "min": None,
                        "max": None,
                        "last_reset": None,
                    }
                ]
            },
        ),
        pytest.raises(HourlyRecorderError, match="out_of_range_recorder_snapshot"),
    ):
        await adapter.async_snapshot_range(RATE_ID, START, START + HOUR)


async def test_bounded_snapshot_rejects_explicit_reset(
    hass,
):
    adapter = HourlyRecorder(hass)
    async_add_external_statistics(
        hass,
        hourly_metadata(RUNTIME_ID),
        [{"start": START, "sum": 2, "state": 2, "last_reset": START}],
    )
    with pytest.raises(HourlyRecorderError, match="unexpected_statistic_reset"):
        await adapter.async_snapshot_range(RUNTIME_ID, START, START + HOUR)


async def test_cancelled_snapshot_does_not_cancel_queued_effect_or_replacement_fence(
    hass,
):
    adapter = HourlyRecorder(hass)
    paused = asyncio.Event()
    release = threading.Event()
    reader = None

    class GateRecorderTask(RecorderTask):
        def run(self, instance):
            instance.hass.loop.call_soon_threadsafe(paused.set)
            if not release.wait(timeout=10):
                raise TimeoutError("Hourly writer gate was not released")

    get_instance(hass).queue_task(GateRecorderTask())
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        adapter.submit(
            _rate_metadata(),
            (HourlyStatisticRow(START, mean=25),),
        )
        reader = asyncio.create_task(
            adapter.async_snapshot_range(RATE_ID, START, START + HOUR)
        )
        await asyncio.sleep(0)
        assert not reader.done()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
    finally:
        release.set()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    replacement = HourlyRecorder(hass)
    snapshot = await replacement.async_snapshot_range(RATE_ID, START, START + HOUR)
    assert snapshot.complete
    assert snapshot.rows == (HourlyStatisticRow(START, mean=25),)


@pytest.mark.parametrize(
    ("corruption", "error"),
    [
        ("missing_mean", "incomplete_statistics_projection"),
        ("nonfinite", "invalid_statistic_number"),
        ("boolean", "invalid_statistic_number"),
        ("fractional_hour", "invalid_statistic_timestamp"),
        ("duplicate", "unordered_recorder_snapshot"),
        ("before_bound", "unordered_recorder_snapshot"),
        ("unsupported", "unsupported_statistic_projection"),
        ("unexpected_id", "invalid_statistics_projection"),
    ],
)
async def test_incomplete_or_malformed_native_projection_never_becomes_complete(
    hass, corruption, error
):
    adapter = HourlyRecorder(hass)
    adapter.submit(_rate_metadata(), (HourlyStatisticRow(START, mean=25),))
    await adapter.async_barrier()
    original = hourly_recorder.statistics_during_period

    def corrupted(*args, **kwargs):
        native = original(*args, **kwargs)
        row = native[RATE_ID][0]
        if corruption == "missing_mean":
            del row["mean"]
        elif corruption == "nonfinite":
            row["mean"] = float("inf")
        elif corruption == "boolean":
            row["mean"] = True
        elif corruption == "fractional_hour":
            row["start"] += 1
        elif corruption == "duplicate":
            native[RATE_ID].append(dict(row))
        elif corruption == "before_bound":
            row["start"] -= 3600
        elif corruption == "unsupported":
            row["sum"] = 1.0
        elif corruption == "unexpected_id":
            native["beestat:other"] = []
        return native

    with (
        patch.object(
            hourly_recorder, "statistics_during_period", side_effect=corrupted
        ),
        pytest.raises(HourlyRecorderError, match=error),
    ):
        await adapter.async_snapshot_range(RATE_ID, START, START + 2 * HOUR)


@pytest.mark.parametrize("extra_rows", [0, 1])
async def test_snapshot_bound_rejects_overflow_without_truncation(hass, extra_rows):
    adapter = HourlyRecorder(hass)
    adapter.submit(_rate_metadata(), (HourlyStatisticRow(START, mean=25),))
    await adapter.async_barrier()
    native = {
        RATE_ID: [
            {
                "start": (START + index * HOUR).timestamp(),
                "mean": 25,
                "min": None,
                "max": None,
                "last_reset": None,
            }
            for index in range(MAX_BOUNDED_SNAPSHOT_HOURS + extra_rows)
        ]
    }
    with patch.object(hourly_recorder, "statistics_during_period", return_value=native):
        if extra_rows:
            with pytest.raises(
                HourlyRecorderError, match="recorder_snapshot_too_large"
            ):
                await adapter.async_snapshot_range(
                    RATE_ID, START, START + MAX_BOUNDED_SNAPSHOT_HOURS * HOUR
                )
        else:
            result = await adapter.async_snapshot_range(
                RATE_ID, START, START + MAX_BOUNDED_SNAPSHOT_HOURS * HOUR
            )
            assert result.complete and len(result.rows) == MAX_BOUNDED_SNAPSHOT_HOURS


async def test_metadata_change_during_projection_blocks_complete_snapshot(hass):
    adapter = HourlyRecorder(hass)
    adapter.submit(_rate_metadata(), (HourlyStatisticRow(START, mean=25),))
    await adapter.async_barrier()
    original = hourly_recorder.get_metadata
    reads = 0

    def changed(*args, **kwargs):
        nonlocal reads
        native = original(*args, **kwargs)
        reads += 1
        if reads == 2:
            metadata_id, metadata = native[RATE_ID]
            native[RATE_ID] = metadata_id, {**metadata, "unit_of_measurement": "s"}
        return native

    with (
        patch.object(hourly_recorder, "get_metadata", side_effect=changed),
        pytest.raises(HourlyRecorderError, match="metadata_changed_during_snapshot"),
    ):
        await adapter.async_snapshot_range(RATE_ID, START, START + HOUR)


@pytest.mark.parametrize(
    "row",
    [
        HourlyStatisticRow(START + timedelta(minutes=1), mean=25),
        HourlyStatisticRow(START, mean=float("nan")),
        HourlyStatisticRow(START, state=1),
    ],
)
async def test_invalid_submission_has_no_native_effect(hass, row):
    adapter = HourlyRecorder(hass)
    with pytest.raises(HourlyRecorderError):
        adapter.submit(_rate_metadata(), (row,))
    assert await known_beestat_ids(hass, adapter) == set()


async def test_native_read_failure_propagates_without_empty_success(hass):
    adapter = HourlyRecorder(hass)
    with (
        patch.object(
            hourly_recorder,
            "statistics_during_period",
            side_effect=RuntimeError("Synthetic read failure"),
        ),
        pytest.raises(RuntimeError, match="Synthetic read failure"),
    ):
        await adapter.async_snapshot_range(RATE_ID, START, START + HOUR)
