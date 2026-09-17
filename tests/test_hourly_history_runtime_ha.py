"""Routine provider acquisition against real native history ownership and storage."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.core import Context

from custom_components.beestat_statistics import hourly_history_runtime
from custom_components.beestat_statistics.api import (
    BeestatRawReadError,
    BeestatRawResponse,
    BeestatReadAttempt,
)
from custom_components.beestat_statistics.config_model import ConfiguredSensor
from custom_components.beestat_statistics.hourly_history_runtime import (
    async_refresh_history,
)
from custom_components.beestat_statistics.hourly_history_writer import sealed
from custom_components.beestat_statistics.hourly_import import HourlyImportError
from custom_components.beestat_statistics.hourly_import_plan import RecorderSnapshot
from tests.test_hourly_history_service_ha import (
    END,
    NOW,
    QUANTITY,
    START,
    _content,
    _plan_request,
    _runtime,
    _stage,
)

pytestmark = pytest.mark.asyncio


async def _finish(runtime):
    async with runtime.importer._lock:
        while runtime.importer.hourly.has_pending_history:
            result = await runtime.importer.hourly.async_advance_history(
                context=runtime.importer._history_context()
            )
            assert result["status"] != "blocked", result
    assert runtime.importer.hourly.history_status()["status"] == "completed"


async def _adopted(
    hass, admin, freezer, monkeypatch, tmp_path, *, finish=True, detailed=False
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    data = runtime.coordinator.data
    runtime.coordinator.data = replace(
        data,
        config=replace(
            data.config,
            sensors=(
                ConfiguredSensor(
                    10, "room", "Room", 1, "zone_a", True, False, False, True
                ),
            ),
        ),
        sensor_rows=({"id": 10, "thermostat_id": 1},),
        summary_rows=(
            ({"thermostat_id": 1, "date": "2026-09-10", "sum_humidifier": 900},)
            if detailed
            else data.summary_rows
        ),
    )
    runtime.native = {}

    async def snapshot(statistic_id, start, end):
        metadata, rows = runtime.native.get(statistic_id, (None, {}))
        return RecorderSnapshot(
            tuple(rows[stamp] for stamp in sorted(rows) if start <= stamp < end),
            deepcopy(metadata),
            complete=True,
        )

    def submit(metadata, rows):
        identifier = metadata["statistic_id"]
        previous = runtime.native.get(identifier, (None, {}))[1]
        runtime.native[identifier] = (
            deepcopy(metadata),
            {**previous, **{row.start: row for row in rows}},
        )

    monkeypatch.setattr(runtime.recorder, "async_snapshot_range", snapshot)
    runtime.write = Mock(side_effect=submit)
    monkeypatch.setattr(runtime.recorder, "submit", runtime.write)
    receipt = await _stage(runtime, monkeypatch, tmp_path, Context(user_id=admin.id))
    async with runtime.importer._lock:
        context = runtime.importer._history_context()
        request = _plan_request(runtime, receipt["source_id"])
        if detailed:
            request["quantity_ids"] = ["thermostat:1:humidifier_runtime_hours"]
        plan = await runtime.importer.hourly.async_plan_history(
            request, context=context
        )
        assert plan["status"] == "planned", plan
        accepted = await runtime.importer.hourly.async_accept_history(
            {**plan["request"], "plan_digest": plan["plan_digest"]}, context=context
        )
        assert accepted["status"] == "accepted"
    if finish:
        await _finish(runtime)
    runtime.source_id = receipt["source_id"]
    return runtime


def _response(rows):
    size = len(json.dumps(rows).encode())
    return BeestatRawResponse(
        rows,
        (BeestatReadAttempt(1, 200, size, "success"),),
        size,
        False,
    )


def _provider(runtime, monkeypatch, *, rows=None, side_effect=None):
    read = AsyncMock(
        return_value=_response(json.loads(_content()) if rows is None else rows),
        side_effect=side_effect,
    )
    monkeypatch.setattr(runtime.client, "async_read_runtime_thermostat", read)
    return read


async def _refresh(runtime, *, days=1):
    async with runtime.importer._lock:
        return await async_refresh_history(
            runtime.importer,
            runtime.importer._history_context(),
            lookback_days=days,
        )


def _objects(runtime):
    root = Path(f"{runtime.store.path}.objects")
    return {
        str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


async def _logical_hour(runtime, *, start=START, end=END, quantity=QUANTITY):
    response = await runtime.importer.async_get_hourly_history(
        {
            "config_entry_id": runtime.entry.entry_id,
            "contract_version": 3,
            "quantity_ids": [quantity],
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    )
    return response["series"][0]["buckets"][0]


@pytest.mark.parametrize("provider_rows", ["identical", "shorter", "empty"])
async def test_unchanged_or_shorter_capture_preserves_claims_without_source_objects(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, provider_rows
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    rows = json.loads(_content())
    if provider_rows == "shorter":
        rows = rows[:5]
    elif provider_rows == "empty":
        rows = []
    read = _provider(runtime, monkeypatch, rows=rows)
    before = await runtime.store.async_load()
    objects = _objects(runtime)
    writes = runtime.write.call_count
    result = await _refresh(runtime)
    assert result == {"status": "unchanged", "changed_rows": 0}
    assert await runtime.store.async_load() == before
    assert _objects(runtime) == objects
    assert runtime.write.call_count == writes
    assert (await _logical_hour(runtime))["value"] == 0.25
    read.assert_awaited_once()
    assert read.await_args.args == (
        1,
        (END - timedelta(days=1)).isoformat(),
        NOW.replace(microsecond=0).isoformat(),
    )
    assert read.await_args.kwargs["raw_response"] is True
    runtime.client.async_read_runtime_sensor.assert_not_called()
    runtime.coordinator.async_refresh_runtime.assert_not_called()
    await runtime.entry._async_process_on_unload(hass)


async def test_correction_seals_only_delta_and_keeps_current_hour_provisional(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    rows = json.loads(_content())
    rows[0]["fan"] = 150
    rows.append({"thermostat_id": 1, "timestamp": END.isoformat(), "fan": 75})

    async def later_response(*_args, **_kwargs):
        freezer.move_to(NOW + timedelta(seconds=2))
        return _response(rows)

    _provider(runtime, monkeypatch, side_effect=later_response)
    before = await runtime.store.async_load()
    originals = _objects(runtime)
    accepted = await _refresh(runtime)
    assert accepted["status"] == "accepted", accepted
    current = await runtime.store.async_load()
    operation = json.loads(
        await runtime.store.async_read_object(
            "operation", current["operation"]["object"]
        )
    )
    assert operation["request"]["end"] == (END + timedelta(hours=1)).isoformat()
    assert operation["request"]["evaluated_at"] == NOW.isoformat()
    new_source_paths = set(_objects(runtime)) - set(originals)
    sources = [path for path in new_source_paths if path.startswith("source/")]
    assert len(sources) == 1
    delta = json.loads(
        await runtime.store.async_read_object("source", Path(sources[0]).name)
    )
    assert delta["format"] == "integration_delta_v1"
    assert delta["rows"] == [rows[0], rows[-1]]
    assert delta["acquisition"]["original_bytes_retained"] is False
    assert (
        delta["acquisition"]["evaluated_at"] == (NOW + timedelta(seconds=2)).isoformat()
    )
    assert delta["baseline_source_revision"] == before["history"]["source_revision"]
    await _finish(runtime)
    bucket = await _logical_hour(runtime)
    assert bucket["reason"] == "ready"
    assert bucket["value"] == pytest.approx((150 + 11 * 75) / 3600)
    provisional = await runtime.importer.async_get_hourly_history(
        {
            "config_entry_id": runtime.entry.entry_id,
            "contract_version": 3,
            "quantity_ids": [QUANTITY],
            "start": END.isoformat(),
            "end": (END + timedelta(hours=1)).isoformat(),
        }
    )
    assert provisional["series"][0]["buckets"][0]["reason"] == "provisional"
    assert provisional["series"][0]["buckets"][0]["value"] is None
    assert runtime.write.call_count == 2
    await runtime.entry._async_process_on_unload(hass)


async def test_acquisition_crossing_hour_keeps_full_policy_window_and_fixed_evaluation(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    first_completed_at = NOW + timedelta(seconds=1)
    completed_at = END + timedelta(hours=1, seconds=1)
    rows = json.loads(_content())
    rows[0]["fan"] = 150
    calls = 0

    async def crossed_hour(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        freezer.move_to(first_completed_at if calls == 1 else completed_at)
        return _response([] if calls == 1 else rows)

    read = _provider(runtime, monkeypatch, side_effect=crossed_hour)
    exports = []
    native_read = hourly_history_runtime.async_read_raw_points

    async def retain_original_export(*args, **kwargs):
        response = await native_read(*args, **kwargs)
        exports.append(deepcopy(response))
        return response

    monkeypatch.setattr(
        hourly_history_runtime, "async_read_raw_points", retain_original_export
    )
    originals = _objects(runtime)
    accepted = await _refresh(runtime, days=45)
    assert accepted["status"] == "accepted", accepted
    current = await runtime.store.async_load()
    operation = json.loads(
        await runtime.store.async_read_object(
            "operation", current["operation"]["object"]
        )
    )
    assert operation["request"]["start"] == (END - timedelta(days=45)).isoformat()
    assert operation["request"]["end"] == (END + timedelta(hours=1)).isoformat()
    assert operation["request"]["evaluated_at"] == NOW.isoformat()
    assert read.await_count == 2
    assert read.await_args_list[0].args[1] == (END - timedelta(days=45)).isoformat()
    assert read.await_args_list[0].args[2] == read.await_args_list[1].args[1]
    assert read.await_args_list[-1].args[2] == NOW.isoformat()
    sources = [
        path
        for path in set(_objects(runtime)) - set(originals)
        if path.startswith("source/")
    ]
    assert len(sources) == 1
    delta = json.loads(
        await runtime.store.async_read_object("source", Path(sources[0]).name)
    )
    assert delta["acquisition"]["evaluated_at"] == completed_at.isoformat()
    assert delta["acquisition"]["bounds"]["end"] == NOW.isoformat()
    original_chunks = delta["acquisition"]["original_chunks"]
    assert len(original_chunks) == 2
    assert {chunk["manifest"]["acquired_at"] for chunk in original_chunks} == {
        completed_at.isoformat()
    }
    assert [chunk["manifest"]["chunk_index"] for chunk in original_chunks] == [0, 1]
    assert {chunk["manifest"]["chunk_count"] for chunk in original_chunks} == {2}
    assert [export["started_at"] for export in exports] == [
        NOW.isoformat(),
        first_completed_at.isoformat(),
    ]
    assert [export["finished_at"] for export in exports] == [
        first_completed_at.isoformat(),
        completed_at.isoformat(),
    ]
    for chunk, export in zip(original_chunks, exports, strict=True):
        content = json.dumps(
            export, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        assert (
            chunk["sha256"]
            == chunk["manifest"]["original_sha256"]
            == (sha256(content).hexdigest())
        )
        assert (
            chunk["byte_count"]
            == chunk["manifest"]["original_byte_count"]
            == len(content)
        )
        assert chunk["row_count"] == export["row_count"]
        assert chunk["manifest"]["start"] == export["request"]["start"]
        assert chunk["manifest"]["end"] == export["request"]["end"]
    await _finish(runtime)
    assert (await _logical_hour(runtime))["value"] == pytest.approx(
        (150 + 11 * 75) / 3600
    )
    await runtime.entry._async_process_on_unload(hass)


async def test_retained_complete_points_close_without_staging_unchanged_source(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    freezer.move_to(END + timedelta(minutes=59))
    data = runtime.coordinator.data
    runtime.coordinator.data = replace(
        data,
        thermostat_rows=tuple(
            {**row, "data_end": (END + timedelta(minutes=55)).isoformat()}
            for row in data.thermostat_rows
        ),
    )
    rows = [
        {
            "thermostat_id": 1,
            "timestamp": (END + timedelta(minutes=5 * i)).isoformat(),
            "fan": 75,
        }
        for i in range(12)
    ]
    read = _provider(runtime, monkeypatch, rows=rows)
    accepted = await _refresh(runtime)
    assert accepted["status"] == "accepted", accepted
    await _finish(runtime)
    assert (await _logical_hour(runtime, start=END, end=END + timedelta(hours=1)))[
        "reason"
    ] == "provisional"
    sources = {
        path: checksum
        for path, checksum in _objects(runtime).items()
        if path.startswith("source/")
    }
    writes = runtime.write.call_count
    freezer.move_to(END + timedelta(hours=1, minutes=1))
    accepted = await _refresh(runtime)
    assert accepted["status"] == "accepted", accepted
    await _finish(runtime)
    assert {
        path: checksum
        for path, checksum in _objects(runtime).items()
        if path.startswith("source/")
    } == sources
    closed = await _logical_hour(runtime, start=END, end=END + timedelta(hours=1))
    assert closed["reason"] == "ready"
    assert closed["value"] == 0.25
    assert runtime.write.call_count > writes
    assert read.await_count == 2
    await runtime.entry._async_process_on_unload(hass)


async def test_overlapping_provider_windows_resolve_final_rows_before_delta_staging(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    # A 31-day refresh has an inclusive 30-day boundary at the adopted first row.
    # The later chunk restores that row, so the earlier correction has no effect.
    later = NOW + timedelta(hours=23)
    freezer.move_to(later)
    rows = json.loads(_content())
    intermediate = {**rows[0], "fan": 150}
    read = _provider(
        runtime,
        monkeypatch,
        side_effect=[_response([intermediate]), _response(rows)],
    )
    before = await runtime.store.async_load()
    objects = _objects(runtime)
    result = await _refresh(runtime, days=31)
    assert result == {"status": "unchanged", "changed_rows": 0}
    assert await runtime.store.async_load() == before
    assert _objects(runtime) == objects
    assert read.await_count == 2
    first, second = read.await_args_list
    assert first.args[2] == second.args[1] == START.isoformat()
    assert second.args[2] == later.isoformat()
    assert (await _logical_hour(runtime))["value"] == 0.25
    runtime.write.assert_called_once()
    runtime.client.async_read_runtime_sensor.assert_not_called()
    await runtime.entry._async_process_on_unload(hass)


async def test_adopted_detailed_quantity_refreshes_after_recent_activity_disappears(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, detailed=True
    )
    quantity = "thermostat:1:humidifier_runtime_hours"
    assert runtime.importer.hourly.history_quantity_ids() == (quantity,)
    runtime.coordinator.data = replace(runtime.coordinator.data, summary_rows=())
    read = _provider(
        runtime,
        monkeypatch,
        rows=[
            {**row, "accessory_type": "off", "accessory": 0}
            for row in json.loads(_content())
        ],
    )
    context = runtime.importer._history_context()
    descriptor = next(
        item for item in context["descriptors"] if item["quantity_id"] == quantity
    )
    assert descriptor["admission"] == "eligible"
    accepted = await _refresh(runtime)
    assert accepted["status"] == "accepted", accepted
    await _finish(runtime)
    closed = await _logical_hour(runtime, quantity=quantity)
    assert closed["reason"] == "ready"
    assert closed["value"] == 0.0
    read.assert_awaited_once()
    runtime.client.async_read_runtime_sensor.assert_not_called()
    await runtime.entry._async_process_on_unload(hass)


async def test_provider_failure_preserves_committed_source_and_native_claims(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    read = _provider(
        runtime,
        monkeypatch,
        side_effect=BeestatRawReadError(
            "Bounded fixture acquisition failed",
            (BeestatReadAttempt(1, 503, 0, "transport_failed"),),
        ),
    )
    before = await runtime.store.async_load()
    objects = _objects(runtime)
    writes = runtime.write.call_count
    with pytest.raises(ValueError, match="history_routine_acquisition_unavailable"):
        await _refresh(runtime)
    assert await runtime.store.async_load() == before
    assert _objects(runtime) == objects
    assert runtime.write.call_count == writes
    assert (await _logical_hour(runtime))["value"] == 0.25
    read.assert_awaited_once()
    async with runtime.importer._lock:
        reason = await runtime.importer._async_refresh_hourly_history(
            lookback_days=1, rebuilding=False
        )
    assert reason == "history_refresh_unverified"
    assert await runtime.store.async_load() == before
    assert runtime.write.call_count == writes
    assert read.await_count == 2
    await runtime.entry._async_process_on_unload(hass)


async def test_pending_accepted_operation_blocks_capture_without_recovery_or_replay(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    read = _provider(runtime, monkeypatch)
    before = await runtime.store.async_load()
    objects = _objects(runtime)
    with pytest.raises(HourlyImportError, match="history_source_baseline_uncommitted"):
        await _refresh(runtime)
    assert await runtime.store.async_load() == before
    assert _objects(runtime) == objects
    read.assert_not_called()
    runtime.write.assert_not_called()
    assert runtime.importer.hourly.has_pending_history
    await runtime.entry._async_process_on_unload(hass)


async def test_importer_wrapper_resumes_pending_intent_but_rebuild_requires_plan(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    read = _provider(runtime, monkeypatch)
    accepted = await runtime.store.async_load()
    async with runtime.importer._lock:
        reason = await runtime.importer._async_refresh_hourly_history(
            lookback_days=1, rebuilding=False
        )
    assert reason == "history_operation_pending"
    await runtime.importer._history_worker
    completed = await runtime.store.async_load()
    assert completed["operation"]["status"] == "completed"
    assert completed["operation"]["plan_digest"] == accepted["operation"]["plan_digest"]
    async with runtime.importer._lock:
        reason = await runtime.importer._async_refresh_hourly_history(
            lookback_days=1, rebuilding=True
        )
    assert reason == "history_rebuild_requires_plan"
    assert await runtime.store.async_load() == completed
    runtime.write.assert_called_once()
    read.assert_not_called()
    await runtime.entry._async_process_on_unload(hass)


async def test_durable_baseline_change_during_capture_cannot_accept_stale_delta(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    before = await runtime.store.async_load()
    changed = deepcopy(before)
    changed["revision"] += 1
    changed["history"]["source_revision"] = sha256(b"new-baseline-fixture").hexdigest()
    changed = sealed(changed)
    rows = json.loads(_content())
    rows[0]["fan"] = 150

    async def concurrent_native_change(*_args, **_kwargs):
        # Simulate an independently changed, valid native owner while network I/O
        # is suspended. No second fake importer or alternate journal is involved.
        await runtime.store.async_save(changed)
        return _response(rows)

    _provider(runtime, monkeypatch, side_effect=concurrent_native_change)
    writes = runtime.write.call_count
    with pytest.raises((HourlyImportError, ValueError), match="baseline"):
        await _refresh(runtime)
    assert await runtime.store.async_load() == changed
    assert runtime.write.call_count == writes
    assert changed["operation"] == before["operation"]
    assert changed["pending"] is None
    await runtime.entry._async_process_on_unload(hass)
