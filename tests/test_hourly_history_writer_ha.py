"""Exact v3 journal recovery against the native Recorder queue and SQLite."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context

from custom_components.beestat_statistics.hourly_import import (
    HourlyImportError,
    HourlyImportManager,
)
from custom_components.beestat_statistics.hourly_recorder import HourlyRecorder
from custom_components.beestat_statistics.hourly_sources import async_stage_source
from tests.test_hourly_history_service_ha import (
    END,
    START,
    _content,
    _manifest,
    _plan_request,
    _runtime,
    _stage,
)
from tests.test_hourly_recorder_ha import (
    _started_recorder,  # noqa: F401 - native SQLite fixture
)

pytestmark = pytest.mark.asyncio
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
    await runtime.entry._async_process_on_unload(hass)


async def test_native_effect_with_failed_proof_save_restarts_without_resubmission(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime, apply = await _accepted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path
    )
    original_write = runtime.store.async_write_object

    async def fail_proof(kind, content):
        if kind == "proof":
            raise OSError("proof persistence interrupted")
        return await original_write(kind, content)

    monkeypatch.setattr(runtime.store, "async_write_object", fail_proof)
    with pytest.raises(OSError, match="proof persistence"):
        await runtime.importer.hourly.async_advance_history(
            context=runtime.importer._history_context()
        )
    saved = await runtime.store.async_load()
    assert saved["pending"]["generation"] == 3
    assert saved["history"]["coverage_revision"] == 0
    assert (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[
        0
    ].mean == pytest.approx(25)
    monkeypatch.setattr(runtime.store, "async_write_object", original_write)
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
    await runtime.entry._async_process_on_unload(hass)


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
    await runtime.entry._async_process_on_unload(hass)
