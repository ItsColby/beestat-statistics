"""Native Recorder contracts for the inactive hourly successor producer."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredThermostat,
)
from custom_components.beestat_statistics.hourly_import_plan import (
    CumulativeCheckpoint,
    HourlyStatisticRow,
    RecorderSnapshot,
    plan_hourly_import,
)
from custom_components.beestat_statistics.hourly_statistics import (
    HourlySeries,
    build_hourly_statistics,
)

pytestmark = pytest.mark.asyncio
HOUR = timedelta(hours=1)
START = datetime(2026, 9, 10, 16, tzinfo=UTC)  # New York noon.
DAY_START = START - 12 * HOUR
NOW = datetime(2026, 9, 13, tzinfo=UTC)
RUNTIME_ID = "beestat:contract_fan_runtime_hours_hourly_v2"


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


def _metadata(statistic_id=RUNTIME_ID, *, measurement=False):
    return {
        "statistic_id": statistic_id,
        "source": "beestat",
        "name": "Recorder contract fixture",
        "unit_of_measurement": "°F" if measurement else "h",
        "unit_class": "temperature" if measurement else "duration",
        "mean_type": 1 if measurement else 0,
        "has_sum": not measurement,
    }


def _counter_rows(start, totals):
    return [
        {"start": start + index * HOUR, "state": total, "sum": total}
        for index, total in enumerate(totals)
    ]


def _runtime_series(start, increments, *, slug="contract") -> HourlySeries:
    """Build real hourly buckets from twelve explicit fan observations per hour."""

    end = start + len(increments) * HOUR
    rows = [
        {
            "thermostat_id": 1,
            "timestamp": (
                start + hour * HOUR + slot * timedelta(minutes=5)
            ).isoformat(),
            "fan": increment * 300,
        }
        for hour, increment in enumerate(increments)
        if increment is not None
        for slot in range(12)
    ]
    series = build_hourly_statistics(
        {1: rows},
        {},
        BeestatConfig((ConfiguredThermostat(1, slug, "Contract"),), ()),
        start=start,
        end=end,
        evaluated_at=NOW,
        source_end_by_thermostat={1: end - timedelta(minutes=5)},
    )
    return next(
        item
        for item in series
        if item.statistic_id == f"beestat:{slug}_fan_runtime_hours_hourly_v2"
    )


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


async def _snapshot(hass, series) -> RecorderSnapshot:
    # This fixture is the sole writer. Fence its queue and read without an end
    # bound, from the required predecessor through every retained later row.
    native = await _read(
        hass,
        {series.statistic_id},
        start=series.hours[0].start - HOUR,
        types={"state", "sum"},
    )
    metadata = await _metadata_readback(hass, series.statistic_id)
    return RecorderSnapshot(
        tuple(
            HourlyStatisticRow(
                datetime.fromtimestamp(row["start"], UTC),
                state=row["state"],
                sum=row["sum"],
            )
            for row in native.get(series.statistic_id, ())
        ),
        metadata[series.statistic_id][1] if series.statistic_id in metadata else None,
        complete=True,
    )


def _plan(series, snapshot, *, verified=None):
    return plan_hourly_import(
        (series,),
        snapshots={series.statistic_id: snapshot},
        checkpoints={series.statistic_id: CumulativeCheckpoint(START, verified)},
    )[0]


def _submit_plan(hass, series, plan):
    assert not plan.blocking_reasons
    assert plan.unblocked_rows
    async_add_external_statistics(
        hass,
        dict(series.metadata),
        [
            {"start": row.start, "state": row.state, "sum": row.sum}
            for row in plan.unblocked_rows
        ],
    )


async def test_first_partial_day_keeps_first_increment_without_zero_predecessor(hass):
    # The remaining first-day hours have explicitly observed zero runtime.
    series = _runtime_series(START, (0.25, 0.5, *([0.0] * 10), 0.125))
    plan = _plan(series, await _snapshot(hass, series))
    _submit_plan(hass, series, plan)

    hourly = (await _read(hass, {RUNTIME_ID}, start=DAY_START))[RUNTIME_ID]
    assert len(hourly) == 13
    assert hourly[0]["start"] == START.timestamp()
    assert [row["change"] for row in hourly[:2]] == [0.25, 0.5]
    assert hourly[0]["sum"] == 0.25
    assert hourly[-1]["start"] == (DAY_START + 24 * HOUR).timestamp()
    assert hourly[-1]["sum"] == 0.875
    assert hourly[-1]["change"] == 0.125
    daily = (await _read(hass, {RUNTIME_ID}, start=DAY_START, period="day"))[RUNTIME_ID]
    assert [row["start"] for row in daily] == [
        DAY_START.timestamp(),
        (DAY_START + 24 * HOUR).timestamp(),
    ]
    assert [row["change"] for row in daily] == [0.75, 0.125]


async def test_start_only_measurement_upsert_clears_values_and_daily_omits_them(hass):
    statistic_id = "beestat:contract_temperature_hourly_v2"
    metadata = _metadata(statistic_id, measurement=True)
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


async def test_cleared_cumulative_suffix_blocks_daily_change_and_restart_is_range_dependent(
    hass,
):
    metadata = _metadata()
    async_add_external_statistics(
        hass, metadata, _counter_rows(START, (0.75, 1.25, 1.5))
    )
    await async_wait_recording_done(hass)
    metadata_before = await _metadata_readback(hass, RUNTIME_ID)
    async_add_external_statistics(
        hass, metadata, [{"start": START + HOUR}, {"start": START + 2 * HOUR}]
    )
    cleared = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    assert [row["state"] for row in cleared] == [0.75, None, None]
    assert [row["sum"] for row in cleared] == [0.75, None, None]
    assert [row["change"] for row in cleared] == [0.75, None, None]
    assert await _metadata_readback(hass, RUNTIME_ID) == metadata_before
    daily = (await _read(hass, {RUNTIME_ID}, period="day"))[RUNTIME_ID]
    assert daily[0]["sum"] is None
    assert daily[0]["change"] is None
    gapped = _runtime_series(START, (0.75, None, None))
    plan = _plan(gapped, await _snapshot(hass, gapped))
    assert "invalid_recorder_snapshot" not in plan.blocking_reasons
    assert "cumulative_source_gap" in plan.blocking_reasons
    assert not plan.stale_starts
    assert not plan.unblocked_rows

    # Probe a hypothetical same-ID continuation, not a safe production proposal.
    resumed = START + 3 * HOUR
    async_add_external_statistics(hass, metadata, _counter_rows(resumed, (1.0,)))
    wide = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    narrow = (await _read(hass, {RUNTIME_ID}, start=resumed))[RUNTIME_ID]
    assert wide[-1]["change"] == 0.25
    assert narrow[0]["change"] == 1.0
    from_first_null = (await _read(hass, {RUNTIME_ID}, start=START + HOUR))[RUNTIME_ID]
    from_last_null = (await _read(hass, {RUNTIME_ID}, start=START + 2 * HOUR))[
        RUNTIME_ID
    ]
    assert [row["change"] for row in from_first_null] == [None, None, 0.25]
    assert [row["change"] for row in from_last_null] == [None, 1.0]
    resumed_daily = (await _read(hass, {RUNTIME_ID}, period="day"))[RUNTIME_ID]
    assert len(resumed_daily) == 1
    assert resumed_daily[0]["sum"] == 1.0
    assert resumed_daily[0]["change"] == 1.0
    assert set(resumed_daily[0]) == {"start", "end", "state", "sum", "change"}


async def test_same_id_reset_cannot_repair_both_ranges_but_distinct_segment_can(hass):
    resumed = START + 3 * HOUR
    metadata = _metadata()
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
        hass, _metadata(segment_id), _counter_rows(resumed, (0.25,))
    )
    for start in (START, resumed):
        segment = (await _read(hass, {segment_id}, start=start))[segment_id]
        assert len(segment) == 1
        assert segment[0]["change"] == 0.25
    segment_metadata = await _metadata_readback(hass, segment_id)
    async_add_external_statistics(
        hass, _metadata(segment_id), _counter_rows(resumed, (0.25,))
    )
    assert (await _read(hass, {segment_id}))[segment_id] == segment
    assert await _metadata_readback(hass, segment_id) == segment_metadata
    # The segment's first increment is independent; this makes no assertion that
    # separate IDs provide a combined, complete day across the missing interval.


async def test_native_readback_distinguishes_partial_intent_and_third_state(hass):
    """Prove native readback distinguishability, not future Store recovery logic."""

    metadata = _metadata()
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
    metadata = _metadata()

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


@pytest.mark.parametrize("has_exact_seed", [True, False])
async def test_expired_raw_prefix_requires_exact_verified_native_seed(
    hass, has_exact_seed
):
    prefix = _runtime_series(START, (0.25, 0.5) if has_exact_seed else (0.25,))
    _submit_plan(hass, prefix, _plan(prefix, await _snapshot(hass, prefix)))
    verified_rows = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    verified = datetime.fromtimestamp(verified_rows[-1]["start"], UTC)

    # Only the rolling window's raw observations remain available to the builder.
    rolling = _runtime_series(START + 2 * HOUR, (0.125, 0.25))
    snapshot = await _snapshot(hass, rolling)
    plan = _plan(rolling, snapshot, verified=verified)
    if not has_exact_seed:
        assert not snapshot.rows
        assert "unproven_cumulative_basis" in plan.blocking_reasons
        assert not plan.unblocked_rows
        assert (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID] == verified_rows
        return
    assert snapshot.rows[0].start == START + HOUR
    assert snapshot.rows[0].sum == 0.75
    _submit_plan(hass, rolling, plan)
    appended = (await _read(hass, {RUNTIME_ID}, start=START + 2 * HOUR))[RUNTIME_ID]
    assert [row["sum"] for row in appended] == [0.875, 1.125]
    assert [row["change"] for row in appended] == [0.125, 0.25]


async def test_early_correction_requires_rewriting_native_cumulative_suffix(hass):
    original = _runtime_series(START, (0.25, 0.5, 0.125))
    _submit_plan(hass, original, _plan(original, await _snapshot(hass, original)))
    corrected_first = _runtime_series(START, (0.5,))
    plan = _plan(corrected_first, await _snapshot(hass, corrected_first))
    assert "surviving_stale_rows" in plan.blocking_reasons
    assert plan.stale_starts == (START + HOUR, START + 2 * HOUR)
    assert not plan.unblocked_rows

    # Demonstrate why updating only the early row is insufficient in native HA.
    async_add_external_statistics(
        hass, dict(original.metadata), _counter_rows(START, (0.5,))
    )
    partial_update = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    assert [row["sum"] for row in partial_update] == [0.5, 0.75, 0.875]
    assert [row["change"] for row in partial_update] == [0.5, 0.25, 0.125]
    corrected = _runtime_series(START, (0.5, 0.5, 0.125))
    full_plan = _plan(corrected, await _snapshot(hass, corrected))
    assert not full_plan.stale_starts
    _submit_plan(hass, corrected, full_plan)
    complete_update = (await _read(hass, {RUNTIME_ID}))[RUNTIME_ID]
    assert len(complete_update) == 3
    assert [row["sum"] for row in complete_update] == [0.5, 1.0, 1.125]
    assert [row["change"] for row in complete_update] == [0.5, 0.5, 0.125]
