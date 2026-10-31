"""Exact v3 journal recovery against the native Recorder queue and SQLite."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context

from custom_components.beestat_statistics.hourly_history_query import history_response
from custom_components.beestat_statistics.hourly_import import (
    _SAVES,
    HourlyImportManager,
)
from custom_components.beestat_statistics.hourly_import_core import HourlyImportError
from custom_components.beestat_statistics.hourly_recorder import HourlyRecorder
from custom_components.beestat_statistics.hourly_sources import async_stage_source
from tests.hourly_history_support import (
    END,
    QUANTITY,
    START,
    _content,
    _manifest,
    _plan_request,
    _runtime,
    _stage,
)

pytestmark = pytest.mark.usefixtures("started_recorder")

NATIVE = "beestat:zone_a_fan_runtime_rate_hourly_v3"


async def _accepted(hass, admin, freezer, monkeypatch, tmp_path):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    native = HourlyRecorder(hass)
    runtime.importer.hourly._recorder = native
    runtime.recorder = native
    receipt = await _stage(runtime, monkeypatch, tmp_path, Context(user_id=admin.id))
    context = runtime.importer._history_context()
    plan = await runtime.importer.hourly.async_plan_history(
        _plan_request(runtime, receipt["source_id"]), context=context
    )
    assert plan["status"] == "planned", plan
    apply = {**plan["request"], "plan_digest": plan["plan_digest"]}
    accepted = await runtime.importer.hourly.async_accept_history(
        apply, context=context
    )
    assert accepted["status"] == "accepted"
    return runtime, apply


@pytest.mark.usefixtures("unload_entries")
async def test_native_commit_has_mean_only_row_and_verified_checkpoint(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime, _apply = await _accepted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path
    )
    manager = runtime.importer.hourly
    assert not (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows
    result = await manager.async_advance_history(
        context=runtime.importer._history_context()
    )
    assert result["status"] == "completed"
    snapshot = await runtime.recorder.async_snapshot_range(NATIVE, START, END)
    assert snapshot.complete and len(snapshot.rows) == 1
    assert snapshot.metadata["has_sum"] is False
    assert snapshot.rows[0].mean == pytest.approx(25)
    assert snapshot.rows[0].sum is None and snapshot.rows[0].state is None
    saved = await runtime.store.async_load()
    assert saved["pending"] is None
    assert saved["history"]["coverage_revision"] == 1


@pytest.mark.usefixtures("unload_entries")
async def test_native_effect_with_failed_proof_readback_restarts_without_resubmission(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime, apply = await _accepted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path
    )
    original_read = runtime.store.async_read_object

    async def fail_proof(kind, reference):
        if kind == "proof":
            raise OSError("proof readback interrupted")
        return await original_read(kind, reference)

    monkeypatch.setattr(runtime.store, "async_read_object", fail_proof)
    with pytest.raises(OSError, match="proof readback"):
        await runtime.importer.hourly.async_advance_history(
            context=runtime.importer._history_context()
        )
    saved = await runtime.store.async_load()
    assert saved["pending"]["generation"] == 3
    assert saved["history"]["coverage_revision"] == 0
    assert (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[
        0
    ].mean == pytest.approx(25)
    monkeypatch.setattr(runtime.store, "async_read_object", original_read)
    recovered = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    submit = Mock(
        side_effect=AssertionError("Already intended native row must not resubmit")
    )
    monkeypatch.setattr(runtime.recorder, "submit", submit)
    context = runtime.importer._history_context()
    await recovered.async_accept_history(apply, context=context)
    result = await recovered.async_advance_history(context=context)
    assert result["status"] == "completed"
    submit.assert_not_called()
    assert (await runtime.store.async_load())["pending"] is None


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("blocked_before_pending", [False, True])
async def test_accepted_replacement_hides_old_native_proof_before_pending(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, blocked_before_pending
):
    runtime, _apply = await _accepted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path
    )
    manager = runtime.importer.hourly
    context = runtime.importer._history_context()
    await manager.async_advance_history(context=context)
    rows = json.loads(_content())
    for row in rows:
        row["fan"] = 150
    content = json.dumps(rows).encode()
    checksum = sha256(content).hexdigest()
    receipt = await async_stage_source(
        runtime.store,
        content,
        {
            **_manifest(runtime),
            "acquisition_id": "reviewed-conflicting-source",
            "original_sha256": checksum,
            "original_byte_count": len(content),
        },
        checksum,
        context["identity"],
    )
    plan = await manager.async_plan_history(
        {
            **_plan_request(runtime, receipt["source_id"]),
            "operation_id": "reviewed-replacement",
            "expected_revision": (await runtime.store.async_load())["revision"],
        },
        context=context,
    )
    assert plan["status"] == "planned"
    await manager.async_accept_history(
        {**plan["request"], "plan_digest": plan["plan_digest"]}, context=context
    )
    if blocked_before_pending:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                runtime.recorder,
                "async_snapshot_range",
                AsyncMock(side_effect=OSError("snapshot unavailable")),
            )
            with pytest.raises(OSError, match="snapshot unavailable"):
                await manager.async_advance_history(context=context)
    saved = await runtime.store.async_load()
    assert saved["pending"] is None
    assert saved["operation"]["status"] == (
        "blocked" if blocked_before_pending else "accepted"
    )
    recovered = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    request = {
        "contract_version": 3,
        "quantity_ids": [QUANTITY],
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    material = await recovered.async_history_material(request, context=context)
    response = history_response(request, material, context)
    bucket = response["series"][0]["buckets"][0]
    assert bucket["value"] is None and bucket["eligible_intervals"] == []
    assert response["series"][0]["summary"]["eligible_hours"] == 0
    # The old native value and committed proof still exist; sealed replacement
    # intent, rather than native absence, must make the query ineligible.
    assert material["hours"][QUANTITY][0]["status"] == "ready"
    assert (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[
        0
    ].mean == pytest.approx(25)
    assert await runtime.store.async_load() == saved


@pytest.mark.usefixtures("unload_entries")
async def test_persisted_fence_blocks_cold_old_root_and_old_entry_marker(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime, _apply = await _accepted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path
    )
    context = runtime.importer._history_context()
    await runtime.importer.hourly.async_advance_history(context=context)
    old_entry = deepcopy(dict(runtime.entry.data))
    rows = json.loads(_content())
    for row in rows:
        row["fan"] = 150
    content = json.dumps(rows).encode()
    checksum = sha256(content).hexdigest()
    manifest = {
        **_manifest(runtime),
        "acquisition_id": "disputed-source",
        "original_sha256": checksum,
        "original_byte_count": len(content),
    }
    receipt = await async_stage_source(
        runtime.store, content, manifest, checksum, context["identity"]
    )
    monkeypatch.setattr(
        runtime.store, "async_save", AsyncMock(side_effect=OSError("root write failed"))
    )
    request = {
        "source_ids": [receipt["source_id"]],
        "quantity_ids": ["thermostat:1:fan_runtime_hours"],
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    with pytest.raises(OSError, match="root write failed"):
        await runtime.importer.hourly.async_refresh_history(request, context=context)
    # Model loss of Core's delayed entry save as well as the failed root save.
    hass.config_entries.async_update_entry(runtime.entry, data=old_entry)
    recovered = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    with pytest.raises(HourlyImportError, match="history_root_invalidated"):
        await recovered.async_history_material(
            request, context=runtime.importer._history_context()
        )
    assert (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[
        0
    ].mean == pytest.approx(25)


_TOKEN = "a" * 64


async def _fence_manager(hass, freezer, monkeypatch, tmp_path):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    return runtime, runtime.importer.hourly


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("check", [True, False])
async def test_fence_refuses_while_a_store_save_is_pending(
    hass, freezer, monkeypatch, tmp_path, check
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    pending = hass.loop.create_future()
    hass.data.setdefault(_SAVES, {})[runtime.entry.entry_id] = pending
    try:
        with pytest.raises(HourlyImportError, match="history_store_write_pending"):
            if check:
                await importer._check_history_root_fence(
                    {"version": 3, "token": _TOKEN}
                )
            else:
                await importer._invalidate_history_root(_TOKEN)
    finally:
        pending.cancel()
        hass.data[_SAVES].pop(runtime.entry.entry_id)


@pytest.mark.usefixtures("unload_entries")
async def test_fence_check_rejects_a_failed_predecessor_fence_task(
    hass, freezer, monkeypatch, tmp_path
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)

    async def failed() -> None:
        raise OSError("fence write failed")

    task = hass.async_create_task(failed())
    task._beestat_history_fence = _TOKEN
    with pytest.raises(OSError, match="fence write failed"):
        await task
    hass.data.setdefault(_SAVES, {})[runtime.entry.entry_id] = task
    with pytest.raises(
        HourlyImportError, match="history_invalidation_fence_unverified"
    ):
        await importer._check_history_root_fence({"version": 3, "token": _TOKEN})


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("mismatch", ["reference", "readback"])
async def test_fence_write_rejects_reference_or_readback_mismatch(
    hass, freezer, monkeypatch, tmp_path, mismatch
):
    _runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    importer._loaded = True  # the failed fence must discard the loaded root
    store = importer._store
    if mismatch == "reference":
        write = store.async_write_object

        async def wrong_reference(kind, content):
            await write(kind, content)
            return "b" * 64

        monkeypatch.setattr(store, "async_write_object", wrong_reference)
    else:
        monkeypatch.setattr(
            store, "async_read_object", AsyncMock(return_value=b"other")
        )
    with pytest.raises(
        HourlyImportError, match="history_invalidation_fence_unverified"
    ):
        await importer._invalidate_history_root(_TOKEN)
    assert importer._error == "history_invalidation_fence_unverified"
    assert importer._loaded is False


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize(
    ("raw", "error", "reason"),
    [
        (b"not the fence", None, "corrupt"),
        (None, RuntimeError("unreadable"), "unreadable"),
    ],
)
async def test_fence_check_rejects_corrupt_or_unreadable_fence(
    hass, freezer, monkeypatch, tmp_path, raw, error, reason
):
    _runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    monkeypatch.setattr(
        importer._store,
        "async_read_object",
        AsyncMock(return_value=raw, side_effect=error),
    )
    with pytest.raises(HourlyImportError, match=f"history_invalidation_fence_{reason}"):
        await importer._check_history_root_fence({"version": 3, "token": _TOKEN})


async def _finished_task(hass, entry_id, token, *, outcome):
    async def body() -> None:
        if outcome == "failed":
            raise OSError("fence write failed")
        if outcome == "cancelled":
            await asyncio.sleep(3600)

    task = hass.async_create_task(body())
    task._beestat_history_fence = token
    if outcome == "cancelled":
        await asyncio.sleep(0)
        task.cancel()
    expected = {"failed": OSError, "cancelled": asyncio.CancelledError}
    if outcome in expected:
        with pytest.raises(expected[outcome]):
            await task
    else:
        await task
    hass.data.setdefault(_SAVES, {})[entry_id] = task
    return task


def _fence_absent(importer, monkeypatch, *, during=None):
    async def absent(kind, name):
        if during is not None:
            during()
        raise FileNotFoundError

    monkeypatch.setattr(importer._store, "async_read_object", absent)


@pytest.mark.usefixtures("unload_entries")
async def test_fence_check_rejects_a_cancelled_predecessor_fence_task(
    hass, freezer, monkeypatch, tmp_path
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    await _finished_task(hass, runtime.entry.entry_id, _TOKEN, outcome="cancelled")
    with pytest.raises(
        HourlyImportError, match="history_invalidation_fence_unverified"
    ):
        await importer._check_history_root_fence({"version": 3, "token": _TOKEN})


@pytest.mark.usefixtures("unload_entries")
async def test_fence_check_ignores_a_failed_task_for_another_root(
    hass, freezer, monkeypatch, tmp_path
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    await _finished_task(hass, runtime.entry.entry_id, "b" * 64, outcome="failed")
    _fence_absent(importer, monkeypatch)
    await importer._check_history_root_fence({"version": 3, "token": _TOKEN})


@pytest.mark.usefixtures("unload_entries")
async def test_fence_check_rejects_a_save_task_replaced_during_the_read(
    hass, freezer, monkeypatch, tmp_path
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    done = hass.loop.create_future()
    done.set_result(None)
    _fence_absent(
        importer,
        monkeypatch,
        during=lambda: hass.data.setdefault(_SAVES, {}).__setitem__(
            runtime.entry.entry_id, done
        ),
    )
    with pytest.raises(HourlyImportError, match="history_root_changed"):
        await importer._check_history_root_fence({"version": 3, "token": _TOKEN})


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("source", ["invalidated_here", "matching_predecessor"])
async def test_absent_fence_still_rejects_a_root_invalidated_in_process(
    hass, freezer, monkeypatch, tmp_path, source
):
    runtime, importer = await _fence_manager(hass, freezer, monkeypatch, tmp_path)
    if source == "invalidated_here":
        await importer._invalidate_history_root(_TOKEN)
        hass.data[_SAVES].pop(runtime.entry.entry_id)
    else:
        await _finished_task(hass, runtime.entry.entry_id, _TOKEN, outcome="done")
    _fence_absent(importer, monkeypatch)
    with pytest.raises(HourlyImportError, match="history_root_invalidated"):
        await importer._check_history_root_fence({"version": 3, "token": _TOKEN})
