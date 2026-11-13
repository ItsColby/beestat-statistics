"""Reviewed corrections through native upload, storage and Recorder."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from hashlib import sha256

import aiohttp
import pytest
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import Context
from homeassistant.setup import async_setup_component

from custom_components.beestat_statistics import hourly_corrections as correction
from custom_components.beestat_statistics import importer as importer_module
from custom_components.beestat_statistics.hourly_history_query import history_response
from custom_components.beestat_statistics.hourly_import import HourlyImportManager
from tests.hourly_history_support import END, QUANTITY, START, _call, _objects
from tests.test_hourly_history_writer_ha import NATIVE, _accepted
from tests.test_hourly_object_retention_ha import _sealed

pytestmark = [pytest.mark.usefixtures("started_recorder", "unload_entries")]


async def _stage(runtime, client, admin, action, predecessor=None):
    manager = runtime.importer.hourly
    context = runtime.importer._history_context()
    root = await runtime.store.async_load()
    request = {
        "config_entry_id": runtime.entry.entry_id,
        "source_ids": root["history"]["source_ids"],
        "quantity_ids": [QUANTITY],
        "start": START.isoformat(),
        "end": END.isoformat(),
        "archive_policy": "reject",
        "daily_policy": "complete_points_else_legacy_day",
        "operation_id": f"native-{action}",
        "expected_revision": root["revision"],
        "consumer_contract": {
            "contract_version": 3,
            "consumers": [
                {
                    "consumer_id": "fixture",
                    "version": "1",
                    "history_contract": 3,
                    "daily_policy": "complete_points_else_legacy_day",
                }
            ],
        },
        "recovery_reference": "fixture-backup",
    }
    writer = manager._history_writer()
    sources, merged = await writer.source_view(request, root["history"], context)
    descriptor = next(d for d in context["descriptors"] if d["quantity_id"] == QUANTITY)
    original = b"original reviewed evidence"
    ref = {"sha256": sha256(original).hexdigest(), "locator": "fixture rows"}
    entry = {
        "descriptor": descriptor,
        "start": START.isoformat(),
        "action": action,
        "dependency_start": request["start"],
        "dependency_end": request["end"],
        "raw_witness": correction.raw_witness(merged, descriptor, START, END),
        "resolution_witness": correction.resolution_witness(
            merged, sources, descriptor, START, END
        ),
        "qualification": [ref],
        "points": [
            {
                "timestamp": (START + timedelta(minutes=5 * i)).isoformat(),
                "thermostat_id": 1,
                "fan": 150,
            }
            for i in range(12)
        ]
        if action == "replace"
        else [],
        "predecessor": predecessor,
        "reason": "unqualified occurrence" if action == "hold" else None,
        "interpretation": {
            "timezone": context["timezone"],
            "fold": 0,
            "unit_conversion": "seconds per interval",
        },
    }
    document = {
        "format": "reviewed_quantity_correction_v1",
        "binding": writer.binding(context),
        "converter_version": "fixture-1",
        "originals": [ref],
        "entries": [entry],
    }
    content = json.dumps(
        {
            "document": document,
            "originals": [
                {
                    "sha256": ref["sha256"],
                    "content_base64": base64.b64encode(original).decode(),
                }
            ],
        }
    ).encode()
    form = aiohttp.FormData()
    form.add_field(
        "file", content, filename="correction.json", content_type="application/json"
    )
    response = await client.post("/api/file_upload", data=form)
    assert response.status == 200
    file_id = (await response.json())["file_id"]
    receipt = await _call(
        runtime,
        "stage_hourly_correction",
        {
            "config_entry_id": runtime.entry.entry_id,
            "file_id": file_id,
            "sha256": sha256(content).hexdigest(),
        },
        Context(user_id=admin.id),
    )
    request["correction_ids"] = [receipt["correction_id"]]
    return request, receipt, ref


async def _accept(runtime, request, manager=None):
    manager = manager or runtime.importer.hourly
    context = runtime.importer._history_context()
    plan = await manager.async_plan_history(request, context=context)
    assert plan["status"] == "planned", plan
    await manager.async_accept_history(
        {**plan["request"], "plan_digest": plan["plan_digest"]}, context=context
    )
    return context, plan


async def test_native_correction_hold_restart_and_collection(
    hass, hass_admin_user, hass_client, freezer, monkeypatch, tmp_path
):
    runtime, _ = await _accepted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    manager = runtime.importer.hourly
    await manager.async_advance_history(context=runtime.importer._history_context())
    monkeypatch.setattr(importer_module, "process_uploaded_file", process_uploaded_file)
    assert await async_setup_component(hass, "file_upload", {})
    refresh_token = next(iter(hass_admin_user.refresh_tokens.values()))
    client = await hass_client(hass.auth.async_create_access_token(refresh_token))
    request, replacement, _ = await _stage(runtime, client, hass_admin_user, "replace")
    context, _ = await _accept(runtime, request)
    await manager.async_advance_history(context=context)
    assert (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[
        0
    ].mean == pytest.approx(50)

    request, hold, ref = await _stage(
        runtime, client, hass_admin_user, "hold", replacement["correction_id"]
    )
    collision = await runtime.store.async_write_object(
        "source", b"original reviewed evidence"
    )
    assert collision == ref["sha256"]
    stale = await _sealed(runtime, revision="0" * 64, original=collision)
    recovered = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    await recovered._load()
    context = runtime.importer._history_context()
    assert await recovered.async_collect_history_objects(context=context) >= 2
    objects = _objects(runtime)
    assert f"source/{stale[0]}" not in objects and f"manifest/{stale[1]}" not in objects
    assert f"evidence/{ref['sha256']}" in objects
    assert f"correction/{hold['correction_id']}" in objects
    assert any(name.startswith("proof/") for name in objects)
    manager = recovered
    context, plan = await _accept(runtime, request, manager)
    query = {
        "contract_version": 3,
        "quantity_ids": [QUANTITY],
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    material = await manager.async_history_material(query, context=context)
    bucket = history_response(query, material, context)["series"][0]["buckets"][0]
    assert bucket["value"] is None and bucket["eligible_intervals"] == []

    original_read = runtime.store.async_read_object

    async def fail_proof(kind, reference):
        if kind == "proof":
            raise OSError("interrupted proof readback")
        return await original_read(kind, reference)

    monkeypatch.setattr(runtime.store, "async_read_object", fail_proof)
    with pytest.raises(OSError, match="interrupted proof"):
        await manager.async_advance_history(context=context)
    row = (await runtime.recorder.async_snapshot_range(NATIVE, START, END)).rows[0]
    assert row.mean is None and row.min is None and row.max is None
    monkeypatch.setattr(runtime.store, "async_read_object", original_read)
    recovered = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    await recovered.async_accept_history(
        {**plan["request"], "plan_digest": plan["plan_digest"]}, context=context
    )
    result = await recovered.async_advance_history(context=context)
    assert result["status"] == "completed", result
    root = await runtime.store.async_load()
    again = {
        **request,
        "correction_ids": [],
        "operation_id": "after-gc",
        "expected_revision": root["revision"],
    }
    plan = await recovered.async_plan_history(again, context=context)
    assert plan["status"] == "planned", plan
    assert any(
        batch["dispositions"].get("source_timestamp_unplaced")
        for batch in plan["batches"]
    )
