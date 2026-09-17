"""Exercise the production hourly adapter against disposable native Recorder."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beestat_statistics import hourly_recorder
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredThermostat,
)
from custom_components.beestat_statistics.hourly_import import HourlyImportManager
from custom_components.beestat_statistics.hourly_import_plan import HourlyStatisticRow
from custom_components.beestat_statistics.hourly_recorder import (
    MAX_SNAPSHOT_ROWS,
    HourlyRecorder,
    HourlyRecorderError,
)
from custom_components.beestat_statistics.hourly_statistics import (
    build_hourly_statistics,
)
from custom_components.beestat_statistics.hourly_storage import (
    HourlyStorageError,
    HourlyStore,
)

pytestmark = pytest.mark.asyncio
HOUR = timedelta(hours=1)
START = datetime(2026, 9, 10, 16, tzinfo=UTC)
NOW = datetime(2026, 9, 13, tzinfo=UTC)
RUNTIME_ID = "beestat:writer_fan_runtime_hours_hourly_v2"
TEMPERATURE_ID = "beestat:writer_temperature_hourly_v2"
# Capture before hass_storage replaces it; restore only the explicitly owned
# journal instance, preserving the harness's isolation for every other Store.
_NATIVE_STORE_WRITE = Store._async_write_data


@pytest.fixture(autouse=True)
async def _started_recorder(recorder_mock: Any, freezer: Any) -> AsyncIterator[None]:
    """Request Recorder first so the harness creates its isolated SQLite owner."""

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
        "name": "Hourly writer fixture",
        "unit_of_measurement": "°F" if measurement else "h",
        "unit_class": "temperature" if measurement else "duration",
        "mean_type": 1 if measurement else 0,
        "has_sum": not measurement,
    }


async def test_snapshot_distinguishes_absence_empty_range_and_cleared_row(hass):
    adapter = HourlyRecorder(hass)
    missing = await adapter.async_snapshot(RUNTIME_ID, START)
    assert missing.complete and missing.metadata is None and not missing.rows

    adapter.submit(_metadata(), (HourlyStatisticRow(START, state=0.25, sum=0.25),))
    imported = await adapter.async_snapshot(RUNTIME_ID, START)
    assert imported.complete and imported.metadata is not None
    assert imported.rows == (HourlyStatisticRow(START, state=0.25, sum=0.25),)
    empty = await adapter.async_snapshot(RUNTIME_ID, START + HOUR)
    assert empty.complete and empty.metadata == imported.metadata and not empty.rows

    for _replay in range(2):
        adapter.submit(_metadata(), (HourlyStatisticRow(START),))
        cleared = await adapter.async_snapshot(RUNTIME_ID, START)
        assert cleared.complete and cleared.metadata == imported.metadata
        assert cleared.rows == (HourlyStatisticRow(START),)


async def test_snapshot_includes_retained_tail_and_only_supported_fields(hass):
    adapter = HourlyRecorder(hass)
    rows = (
        HourlyStatisticRow(START - HOUR, mean=70, min=69, max=71),
        HourlyStatisticRow(START, mean=72, min=71, max=73),
        HourlyStatisticRow(START + 5 * HOUR, mean=75, min=74, max=76),
    )
    metadata = _metadata(TEMPERATURE_ID, measurement=True)
    adapter.submit(metadata, rows)
    snapshot = await adapter.async_snapshot(TEMPERATURE_ID, START)
    assert snapshot.complete
    assert snapshot.rows == rows[1:]
    assert {key: snapshot.metadata[key] for key in metadata} == metadata
    native_metadata = await get_instance(hass).async_add_executor_job(
        lambda: hourly_recorder.get_metadata(hass, statistic_ids={TEMPERATURE_ID})
    )
    assert snapshot.metadata == native_metadata[TEMPERATURE_ID][1]
    assert all(row.sum is None and row.state is None for row in snapshot.rows)

    other = {**_metadata(), "statistic_id": "other:writer", "source": "other"}
    async_add_external_statistics(hass, other, [{"start": START, "sum": 1}])
    assert await adapter.async_known_ids() == {TEMPERATURE_ID}


async def test_projection_explicitly_requests_native_units_and_no_change(hass):
    adapter = HourlyRecorder(hass)
    adapter.submit(
        _metadata(TEMPERATURE_ID, measurement=True),
        (HourlyStatisticRow(START, mean=72, min=71, max=73),),
    )
    original = hourly_recorder.statistics_during_period
    with patch.object(
        hourly_recorder, "statistics_during_period", wraps=original
    ) as read:
        snapshot = await adapter.async_snapshot(TEMPERATURE_ID, START)
    assert snapshot.rows[0].mean == 72
    assert read.call_args.args[2] is None
    assert read.call_args.args[4:7] == (
        "hour",
        {"temperature": "°F"},
        {"mean", "min", "max"},
    )


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
        adapter.submit(_metadata(), (HourlyStatisticRow(START, state=0.25, sum=0.25),))
        reader = asyncio.create_task(adapter.async_snapshot(RUNTIME_ID, START))
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
    snapshot = await replacement.async_snapshot(RUNTIME_ID, START)
    assert snapshot.complete
    assert snapshot.rows == (HourlyStatisticRow(START, state=0.25, sum=0.25),)


@pytest.mark.parametrize(
    ("corruption", "error"),
    [
        ("missing_sum", "incomplete_statistics_projection"),
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
    adapter.submit(_metadata(), (HourlyStatisticRow(START, state=0.25, sum=0.25),))
    await adapter.async_barrier()
    original = hourly_recorder.statistics_during_period

    def corrupted(*args, **kwargs):
        native = original(*args, **kwargs)
        row = native[RUNTIME_ID][0]
        if corruption == "missing_sum":
            del row["sum"]
        elif corruption == "nonfinite":
            row["sum"] = float("inf")
        elif corruption == "boolean":
            row["state"] = True
        elif corruption == "fractional_hour":
            row["start"] += 1
        elif corruption == "duplicate":
            native[RUNTIME_ID].append(dict(row))
        elif corruption == "before_bound":
            row["start"] -= 3600
        elif corruption == "unsupported":
            row["mean"] = 1.0
        elif corruption == "unexpected_id":
            native["beestat:other"] = []
        return native

    with (
        patch.object(
            hourly_recorder, "statistics_during_period", side_effect=corrupted
        ),
        pytest.raises(HourlyRecorderError, match=error),
    ):
        await adapter.async_snapshot(RUNTIME_ID, START)


@pytest.mark.parametrize("extra_rows", [0, 1])
async def test_snapshot_bound_rejects_overflow_without_truncation(hass, extra_rows):
    adapter = HourlyRecorder(hass)
    adapter.submit(_metadata(), (HourlyStatisticRow(START, state=0.25, sum=0.25),))
    await adapter.async_barrier()
    native = {
        RUNTIME_ID: [
            {"start": (START + index * HOUR).timestamp(), "state": 0.25, "sum": 0.25}
            for index in range(MAX_SNAPSHOT_ROWS + extra_rows)
        ]
    }
    with patch.object(hourly_recorder, "statistics_during_period", return_value=native):
        if extra_rows:
            with pytest.raises(
                HourlyRecorderError, match="recorder_snapshot_too_large"
            ):
                await adapter.async_snapshot(RUNTIME_ID, START)
        else:
            result = await adapter.async_snapshot(RUNTIME_ID, START)
            assert result.complete and len(result.rows) == MAX_SNAPSHOT_ROWS


async def test_metadata_change_during_projection_blocks_complete_snapshot(hass):
    adapter = HourlyRecorder(hass)
    adapter.submit(_metadata(), (HourlyStatisticRow(START, state=0.25, sum=0.25),))
    await adapter.async_barrier()
    original = hourly_recorder.get_metadata
    reads = 0

    def changed(*args, **kwargs):
        nonlocal reads
        native = original(*args, **kwargs)
        reads += 1
        if reads == 2:
            metadata_id, metadata = native[RUNTIME_ID]
            native[RUNTIME_ID] = metadata_id, {**metadata, "unit_of_measurement": "s"}
        return native

    with (
        patch.object(hourly_recorder, "get_metadata", side_effect=changed),
        pytest.raises(HourlyRecorderError, match="metadata_changed_during_snapshot"),
    ):
        await adapter.async_snapshot(RUNTIME_ID, START)


@pytest.mark.parametrize(
    "row",
    [
        HourlyStatisticRow(START + timedelta(minutes=1), state=1, sum=1),
        HourlyStatisticRow(START, state=float("nan"), sum=1),
        HourlyStatisticRow(START, mean=1),
    ],
)
async def test_invalid_submission_has_no_native_effect(hass, row):
    adapter = HourlyRecorder(hass)
    with pytest.raises(HourlyRecorderError):
        adapter.submit(_metadata(), (row,))
    assert await adapter.async_known_ids() == set()


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
        await adapter.async_snapshot(RUNTIME_ID, START)


@pytest.fixture
def disk_store(hass, tmp_path):
    store = HourlyStore(hass, "writer-contract")
    native = store._store
    with (
        patch.object(native, "path", str(tmp_path / "hourly-journal.json")),
        patch.object(native, "_async_write_data", _NATIVE_STORE_WRITE.__get__(native)),
    ):
        yield store


async def test_store_round_trip_proves_real_disk_and_ignores_native_pending_load(
    disk_store,
):
    store = disk_store
    assert await store.async_load() is None
    await store.async_save({"revision": 1, "pending": {"sum": 0.25}})
    assert await store.async_load() == {"revision": 1, "pending": {"sum": 0.25}}

    # Native Store.async_load may return its unsaved _data. The wrapper must read
    # its file directly rather than accepting that successful native load.
    store._store._data = {
        "version": 1,
        "minor_version": 1,
        "key": store._store.key,
        "data": {"revision": 2},
    }
    assert await store._store.async_load() == {"revision": 2}
    assert await store.async_load() == {"revision": 1, "pending": {"sum": 0.25}}
    with patch.object(store._store, "async_load", new_callable=AsyncMock) as cached:
        cached.return_value = {"revision": 3}
        assert await store.async_load() == {"revision": 1, "pending": {"sum": 0.25}}
        cached.assert_not_called()


async def test_native_silent_write_failure_cannot_prove_new_intent(disk_store, caplog):
    store = disk_store
    await store.async_save({"revision": 1})
    with (
        patch.object(
            store._store,
            "_write_prepared_data",
            side_effect=WriteError("Synthetic journal write failure"),
        ),
        pytest.raises(HourlyStorageError, match="hourly_store_write_unverified"),
    ):
        await store.async_save({"revision": 2})
    assert "Synthetic journal write failure" in caplog.text
    assert await store.async_load() == {"revision": 1}


async def test_native_read_only_store_cannot_prove_new_intent(disk_store):
    store = disk_store
    store._store.make_read_only()
    with pytest.raises(HourlyStorageError, match="hourly_store_write_unverified"):
        await store.async_save({"revision": 1})
    assert await store.async_load() is None


@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.stopped])
async def test_store_closes_admission_before_native_stopping_deferral(
    hass, disk_store, state
):
    with (
        patch.object(hass, "state", state),
        patch.object(disk_store._store, "async_save", new_callable=AsyncMock) as save,
        pytest.raises(HourlyStorageError, match="hourly_store_stopping"),
    ):
        await disk_store.async_save({"revision": 1})
    save.assert_not_awaited()
    assert await disk_store.async_load() is None


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "null",
        "{}",
        "[]",
        '{"version":2,"minor_version":1,"key":"KEY","data":{}}',
        '{"version":1,"minor_version":2,"key":"KEY","data":{}}',
        '{"version":1,"minor_version":1,"key":"other","data":{}}',
        '{"version":1,"minor_version":1,"key":"KEY","data":[]}',
    ],
)
async def test_invalid_disk_journal_is_not_absence_or_renamed(
    hass, disk_store, content
):
    store = disk_store
    content = content.replace("KEY", store._store.key)
    path = Path(store.path)
    await hass.async_add_executor_job(path.write_text, content)
    with pytest.raises((HourlyStorageError, HomeAssistantError)):
        await store.async_load()
    assert await hass.async_add_executor_job(path.read_text) == content


async def test_readback_requires_json_type_identity(hass, disk_store):
    store = disk_store
    await store.async_save({"revision": 1})
    path = Path(store.path)
    envelope = {
        "version": 1,
        "minor_version": 1,
        "key": store._store.key,
        "data": {"revision": True},
    }
    await hass.async_add_executor_job(path.write_text, json.dumps(envelope))
    with pytest.raises(HourlyStorageError, match="hourly_store_write_unverified"):
        await hass.async_add_executor_job(store._verify_data, {"revision": 1})


def _source_hours(increments):
    end = START + len(increments) * HOUR
    rows = [
        {
            "thermostat_id": 1,
            "timestamp": (
                START + hour * HOUR + slot * timedelta(minutes=5)
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
        BeestatConfig((ConfiguredThermostat(1, "writer", "Writer fixture"),), ()),
        start=START,
        end=end,
        evaluated_at=NOW,
        source_end_by_thermostat={1: end - timedelta(minutes=5)},
    )
    return tuple(item for item in series if item.statistic_id == RUNTIME_ID)


def _entry_and_identity(hass):
    entry = MockConfigEntry(
        domain="beestat_statistics", entry_id="writer-contract", data={}
    )
    entry.add_to_hass(hass)
    return entry, {
        "entry_id": entry.entry_id,
        "api_base": "https://api.beestat.io/",
        "account_anchors": ["synthetic-account-anchor"],
        "resources": {
            RUNTIME_ID: {
                "thermostat_id": 1,
                "sensor_id": None,
                "quantity": "fan_runtime_hours",
            }
        },
    }


async def _select(writer, source, identity, *, epoch=START):
    revision = writer.status()["revision"]
    preview = await writer.async_select(
        source,
        identity,
        epoch_start=epoch,
        statistic_ids=(RUNTIME_ID,),
        expected_revision=revision,
    )
    selected = await writer.async_select(
        source,
        identity,
        epoch_start=epoch,
        statistic_ids=(RUNTIME_ID,),
        expected_revision=revision,
        preview_digest=preview["preview_digest"],
    )
    assert selected["status"] == "selected"
    return preview


async def test_real_manager_restart_gap_invalidation_and_explicit_segment(
    hass, disk_store
):
    entry, identity = _entry_and_identity(hass)
    recorder = HourlyRecorder(hass)
    writer = HourlyImportManager(hass, entry, store=disk_store, recorder=recorder)
    source = _source_hours((0.25, 0.5, 0.125))
    await _select(writer, source, identity)
    assert (await writer.async_import(source, identity))["imported_rows"] == 3
    original = await recorder.async_snapshot(RUNTIME_ID, START)
    assert [row.sum for row in original.rows] == [0.25, 0.75, 0.875]

    writer.close()
    writer = HourlyImportManager(hass, entry, store=disk_store, recorder=recorder)
    assert await writer.async_mode() == "hourly"
    coverage = writer.coverage(start=START, end=START + 3 * HOUR)["series"][RUNTIME_ID]
    assert coverage["complete"] and coverage["complete_observed_hours"] == 3
    assert coverage["observed_hour_average"] == pytest.approx(0.875 / 3)

    corrected = _source_hours((0.25, None, 0.125))
    await writer.async_import(corrected, identity)
    invalidated = await recorder.async_snapshot(RUNTIME_ID, START)
    assert invalidated.rows[0] == original.rows[0]
    assert all(row.cleared for row in invalidated.rows[1:])
    coverage = writer.coverage(start=START, end=START + 3 * HOUR)["series"][RUNTIME_ID]
    assert not coverage["complete"] and coverage["complete_observed_hours"] == 1
    assert coverage["observed_hour_average"] == 0.25
    assert [hour["value"] for hour in coverage["hours"]] == [0.25, None, None]

    preview = await _select(writer, corrected, identity, epoch=START + 2 * HOUR)
    successor = preview["selection"][0]["statistic_id"]
    assert successor != RUNTIME_ID
    assert (await writer.async_import(corrected, identity))["imported_rows"] == 1
    assert await recorder.async_snapshot(RUNTIME_ID, START) == invalidated
    segment = await recorder.async_snapshot(successor, START)
    assert segment.rows == (
        HourlyStatisticRow(START + 2 * HOUR, state=0.125, sum=0.125),
    )
    coverage = writer.coverage(start=START, end=START + 3 * HOUR)["series"][RUNTIME_ID]
    assert coverage["complete_observed_hours"] == 1
    assert coverage["observed_hour_average"] == 0.125
    assert coverage["closed_segments"] == [
        {
            "statistic_id": RUNTIME_ID,
            "epoch_start": START.isoformat(),
            "end": (START + HOUR).isoformat(),
        }
    ]
    assert [hour["coverage"] for hour in coverage["hours"]] == [
        "outside_active_segment",
        "outside_active_segment",
        "verified",
    ]


async def test_real_checkpoint_write_failure_recovers_without_recorder_replay(
    hass, disk_store
):
    entry, identity = _entry_and_identity(hass)
    recorder = HourlyRecorder(hass)
    writer = HourlyImportManager(hass, entry, store=disk_store, recorder=recorder)
    source = _source_hours((0.25, 0.5))
    await _select(writer, source, identity)
    native_write = disk_store._store._write_prepared_data
    writes = 0

    def fail_checkpoint(*args):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise WriteError("Synthetic checkpoint failure after Recorder effect")
        return native_write(*args)

    with patch.object(recorder, "submit", wraps=recorder.submit) as submit:
        with (
            patch.object(
                disk_store._store, "_write_prepared_data", side_effect=fail_checkpoint
            ),
            pytest.raises(HourlyStorageError, match="hourly_store_write_unverified"),
        ):
            await writer.async_import(source, identity)
        assert submit.call_count == 1
        actual = await recorder.async_snapshot(RUNTIME_ID, START)
        assert [row.sum for row in actual.rows] == [0.25, 0.75]
        saved = await disk_store.async_load()
        assert saved["pending"] is not None
        held = writer.coverage(start=START, end=START + 2 * HOUR)["series"][RUNTIME_ID]
        assert held["complete_observed_hours"] == 0

        writer.close()
        recovered = HourlyImportManager(
            hass, entry, store=disk_store, recorder=recorder
        )
        await recovered.async_reconcile()
        assert submit.call_count == 1
        assert (await disk_store.async_load())["pending"] is None
        coverage = recovered.coverage(start=START, end=START + 2 * HOUR)["series"][
            RUNTIME_ID
        ]
        assert coverage["complete"] and coverage["complete_observed_hours"] == 2
        assert coverage["observed_hour_average"] == 0.375
