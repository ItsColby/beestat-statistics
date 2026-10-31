"""Native service permissions and entry ownership for qualified hourly history."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import probatio
import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

from custom_components.beestat_statistics import importer as importer_module
from custom_components.beestat_statistics.const import DOMAIN
from custom_components.beestat_statistics.hourly_history_contract import (
    MAX_SOURCE_BYTES,
)
from custom_components.beestat_statistics.hourly_import import MARKER
from custom_components.beestat_statistics.hourly_rows import RecorderSnapshot
from tests.hourly_history_support import (
    END,
    QUANTITY,
    START,
    _call,
    _content,
    _manifest,
    _plan_request,
    _request,
    _runtime,
    _stage,
    _upload,
)


@pytest.mark.parametrize("change_context", ["none", "configuration", "unload"])
async def test_query_projection_off_loop_keeps_snapshot_and_rechecks_context(
    hass, freezer, monkeypatch, tmp_path, change_context
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    material = {"synthetic": True, "root_digest": importer_module.history_digest(None)}
    monkeypatch.setattr(
        runtime.importer.hourly,
        "async_history_material",
        AsyncMock(return_value=material),
    )
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    projected_requests = []

    def project(request, snapshot, context):
        assert threading.get_ident() != loop_thread
        assert "check_current" not in context and "config" not in context
        assert snapshot is material
        started.set()
        assert release.wait(5)
        projected_requests.append(deepcopy(request))
        return {"status": "projected"}

    monkeypatch.setattr(importer_module, "history_response", project)
    request = {"quantity_ids": [QUANTITY]}
    task = asyncio.create_task(runtime.importer.async_get_hourly_history(request))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        request["quantity_ids"].append("changed-by-caller")
        if change_context == "configuration":
            hass.config_entries.async_update_entry(
                runtime.entry,
                options={**runtime.entry.options, "scan_interval_seconds": 600},
            )
        elif change_context == "unload":
            runtime.importer._async_unload()
        release.set()
        if change_context in ("configuration", "unload"):
            with pytest.raises(ValueError, match="history_context_changed"):
                await task
        else:
            assert await task == {"status": "projected"}
        assert projected_requests == [{"quantity_ids": [QUANTITY]}]
        runtime.submit.assert_not_called()
        for forbidden in runtime.forbidden:
            forbidden.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.entry._async_process_on_unload(hass)


def _native_rest_export(runtime):
    manifest = _manifest(runtime)
    return {
        "changed_states": [],
        "service_response": {
            "schema_version": 1,
            "status": "success",
            "identity": {
                key: manifest[key]
                for key in (
                    "config_entry_id",
                    "resource",
                    "resource_id",
                    "thermostat_id",
                )
            },
            "request": {
                "resource": manifest["resource"],
                "resource_id": manifest["resource_id"],
                "method": "read",
                "timestamp_operator": "between",
                "boundary": "inclusive",
                "start": manifest["start"],
                "end": manifest["end"],
            },
            "completeness": {
                "transport_complete": True,
                "truncated": False,
                "pagination_indicated": False,
            },
            "data": json.loads(_content()),
        },
    }


def _assert_no_source_io(runtime):
    for spy in runtime.forbidden:
        spy.assert_not_called()


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize(
    "service", ["stage_hourly_source", "plan_hourly_history", "apply_hourly_history"]
)
@pytest.mark.parametrize("caller", ["anonymous", "unknown", "reader", "inactive_admin"])
async def test_effect_and_plan_services_require_active_admin_before_io(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, service, caller
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    reader = await hass.auth.async_create_user(
        "History fixture reader", group_ids=[GROUP_ID_USER]
    )
    contexts = {
        "anonymous": Context(),
        "unknown": Context(user_id="unknown-fixture-user"),
        "reader": Context(user_id=reader.id),
        "inactive_admin": Context(user_id=hass_admin_user.id),
    }
    if caller == "inactive_admin":
        monkeypatch.setattr(hass_admin_user, "is_active", False)
    upload = Mock(side_effect=AssertionError("Unauthorized upload access"))
    load = AsyncMock(side_effect=AssertionError("Unauthorized journal access"))
    monkeypatch.setattr(importer_module, "process_uploaded_file", upload)
    monkeypatch.setattr(runtime.store, "async_load", load)
    with pytest.raises(HomeAssistantError):
        await _call(runtime, service, _request(runtime, service), contexts[caller])
    upload.assert_not_called()
    load.assert_not_called()
    runtime.snapshots.assert_not_called()
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
async def test_ordinary_authenticated_user_can_read_v3_coverage_and_configuration(
    hass, freezer, monkeypatch, tmp_path
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    reader = await hass.auth.async_create_user(
        "History fixture reader", group_ids=[GROUP_ID_USER]
    )
    context = Context(user_id=reader.id)
    coverage = await _call(
        runtime,
        "get_hourly_coverage",
        _request(runtime, "get_hourly_coverage"),
        context,
    )
    configuration = await _call(
        runtime, "get_configuration", _request(runtime, "get_configuration"), context
    )
    assert coverage["contract_version"] == 3
    assert coverage["pagination"]["total_buckets"] == 1
    assert coverage["series"][0]["buckets"][0]["value"] is None
    assert configuration["hourly_statistics"]["history_v3"]["contract_version"] == 3
    assert not Path(runtime.store.path).exists()
    assert MARKER not in runtime.entry.data
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize(
    "service",
    [
        "stage_hourly_source",
        "plan_hourly_history",
        "apply_hourly_history",
        "get_hourly_coverage",
        "get_configuration",
    ],
)
@pytest.mark.parametrize("entry_case", ["unknown", "not_loaded"])
async def test_native_services_require_the_explicit_loaded_entry(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, service, entry_case
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    request = _request(runtime, service)
    if entry_case == "unknown":
        request["config_entry_id"] = "missing-fixture-entry"
    else:
        runtime.entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    with pytest.raises(HomeAssistantError):
        await _call(runtime, service, request, Context(user_id=hass_admin_user.id))
    runtime.snapshots.assert_not_called()
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
async def test_apply_rejects_nested_entry_mismatch_before_journal_access(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    request = _request(runtime, "apply_hourly_history")
    request["plan"]["config_entry_id"] = "different-fixture-entry"
    load = AsyncMock(side_effect=AssertionError("Mismatched entry reached journal"))
    monkeypatch.setattr(runtime.store, "async_load", load)
    with pytest.raises(HomeAssistantError):
        await _call(
            runtime,
            "apply_hourly_history",
            request,
            Context(user_id=hass_admin_user.id),
        )
    load.assert_not_called()
    runtime.submit.assert_not_called()
    assert runtime.importer._history_worker is None


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("transport", ["rows", "native_rest_export"])
async def test_stage_upload_lease_and_cleanup_run_in_executor_after_durable_retention(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, transport
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    content = (
        json.dumps(_native_rest_export(runtime), indent=2).encode() + b"\n"
        if transport == "native_rest_export"
        else _content()
    )
    upload_path, events = _upload(
        runtime, monkeypatch, tmp_path, content, verify_retained=True
    )
    request = _request(runtime, "stage_hourly_source")
    request["sha256"] = sha256(content).hexdigest()
    request["manifest"].update(
        original_sha256=request["sha256"], original_byte_count=len(content)
    )
    before_entry = deepcopy(dict(runtime.entry.data))
    response = await _call(
        runtime,
        "stage_hourly_source",
        request,
        Context(user_id=hass_admin_user.id),
    )
    assert events == ["entered", "durably_retained", "cleaned"]
    assert not upload_path.exists()
    assert response["contract_version"] == 3
    assert response["sha256"] == sha256(content).hexdigest()
    assert response["row_count"] == 12
    assert response["observed_start"] == START.isoformat()
    assert response["observed_end"] == (END - timedelta(minutes=5)).isoformat()
    assert response["manifest"]["original_sha256"] == sha256(content).hexdigest()
    assert response["manifest"]["original_byte_count"] == len(content)
    assert (
        await runtime.store.async_read_object("source", response["sha256"]) == content
    )
    if transport == "native_rest_export":
        plan = await _call(
            runtime,
            "plan_hourly_history",
            _plan_request(runtime, response["source_id"]),
            Context(user_id=hass_admin_user.id),
        )
        assert plan["status"] == "planned"
        assert plan["numeric_rows"] == 1
        assert plan["batches"][0]["dispositions"] == {"ready": 1}
    assert not Path(runtime.store.path).exists()
    assert runtime.entry.data == before_entry
    assert runtime.importer.hourly._state is None
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("rejected", ["identity", "window", "incomplete", "shape"])
async def test_native_rest_upload_rejects_invalid_inner_export_before_retention(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, rejected
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    envelope = _native_rest_export(runtime)
    export = envelope["service_response"]
    if rejected == "identity":
        export["identity"]["config_entry_id"] = "different-fixture-entry"
    elif rejected == "window":
        export["request"]["end"] = (END + timedelta(hours=1)).isoformat()
    elif rejected == "incomplete":
        export["completeness"]["transport_complete"] = False
    else:
        envelope["service_response"] = []
    content = json.dumps(envelope).encode()
    upload_path, events = _upload(
        runtime, monkeypatch, tmp_path, content, verify_retained=False
    )
    request = _request(runtime, "stage_hourly_source")
    request["sha256"] = sha256(content).hexdigest()
    request["manifest"].update(
        original_sha256=request["sha256"], original_byte_count=len(content)
    )
    before_entry = deepcopy(dict(runtime.entry.data))
    with pytest.raises(HomeAssistantError):
        await _call(
            runtime, "stage_hourly_source", request, Context(user_id=hass_admin_user.id)
        )
    assert events == ["entered", "cleaned"]
    assert not upload_path.exists()
    assert not Path(f"{runtime.store.path}.objects").exists()
    assert not Path(runtime.store.path).exists()
    assert runtime.entry.data == before_entry
    assert runtime.importer.hourly._state is None
    runtime.snapshots.assert_not_awaited()
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("rejected", ["hash", "size"])
async def test_rejected_upload_is_cleaned_without_retaining_source_or_journal(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, rejected
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    content = _content()
    if rejected == "size":
        content += b" " * (MAX_SOURCE_BYTES + 1 - len(content))
    upload_path, events = _upload(
        runtime, monkeypatch, tmp_path, content, verify_retained=False
    )
    request = _request(runtime, "stage_hourly_source")
    request["sha256"] = sha256(content).hexdigest() if rejected == "size" else "0" * 64
    request["manifest"].update(
        original_sha256=sha256(content).hexdigest(), original_byte_count=len(content)
    )
    with pytest.raises(HomeAssistantError):
        await _call(
            runtime, "stage_hourly_source", request, Context(user_id=hass_admin_user.id)
        )
    assert events == ["entered", "cleaned"]
    assert not upload_path.exists()
    assert not Path(f"{runtime.store.path}.objects").exists()
    assert not Path(runtime.store.path).exists()
    assert MARKER not in runtime.entry.data
    runtime.submit.assert_not_called()


@pytest.mark.usefixtures("unload_entries")
async def test_plan_query_and_configuration_leave_all_writers_untouched(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    context = Context(user_id=hass_admin_user.id)
    source = await _stage(runtime, monkeypatch, tmp_path, context)
    unchanged = (
        deepcopy(dict(runtime.entry.data)),
        runtime.importer.hourly._state,
        runtime.importer.hourly._loaded,
    )
    writes = []
    for owner, name, asynchronous in (
        (runtime.store, "async_save", True),
        (runtime.store, "async_write_object", True),
        (runtime.store, "write_object", False),
        (hass.config_entries, "async_update_entry", False),
    ):
        spy = (AsyncMock if asynchronous else Mock)(
            side_effect=AssertionError(f"Read-only service attempted {name}")
        )
        monkeypatch.setattr(owner, name, spy)
        writes.append(spy)
    plan = await _call(
        runtime,
        "plan_hourly_history",
        _plan_request(runtime, source["source_id"]),
        context,
    )
    await _call(
        runtime,
        "get_hourly_coverage",
        _request(runtime, "get_hourly_coverage"),
        context,
    )
    await _call(
        runtime, "get_configuration", _request(runtime, "get_configuration"), context
    )
    assert plan["status"] == "planned"
    assert plan["request"]["config_entry_id"] == runtime.entry.entry_id
    assert plan["numeric_rows"] == 1
    assert (
        runtime.entry.data,
        runtime.importer.hourly._state,
        runtime.importer.hourly._loaded,
    ) == unchanged
    assert not Path(runtime.store.path).exists()
    runtime.snapshots.assert_awaited()
    for spy in writes:
        spy.assert_not_called()
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("service", ["plan_hourly_history", "get_hourly_coverage"])
@pytest.mark.parametrize("changed", ["runtime", "configuration", "timezone"])
async def test_context_change_during_native_read_discards_response_without_writes(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, service, changed
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    context = Context(user_id=hass_admin_user.id)
    source = await _stage(runtime, monkeypatch, tmp_path, context)
    started, proceed = asyncio.Event(), asyncio.Event()
    native_load = runtime.store.async_load

    async def paused_load():
        result = await native_load()
        started.set()
        await proceed.wait()
        return result

    monkeypatch.setattr(runtime.store, "async_load", paused_load)
    request = (
        _plan_request(runtime, source["source_id"])
        if service == "plan_hourly_history"
        else _request(runtime, service)
    )
    task = asyncio.create_task(_call(runtime, service, request, context))
    await asyncio.wait_for(started.wait(), 1)
    if changed == "runtime":
        runtime.entry.runtime_data = SimpleNamespace(
            client=runtime.client,
            coordinator=runtime.coordinator,
            importer=runtime.importer,
        )
    elif changed == "configuration":
        data = runtime.coordinator.data
        thermostat = replace(data.config.thermostats[0], name="Changed fixture")
        runtime.coordinator.data = replace(
            data, config=replace(data.config, thermostats=(thermostat,))
        )
    else:
        runtime.coordinator.async_update_local_timezone(ZoneInfo("UTC"))
    proceed.set()
    with pytest.raises(HomeAssistantError):
        await task
    assert not Path(runtime.store.path).exists()
    assert MARKER not in runtime.entry.data
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
async def test_cancelled_apply_caller_does_not_cancel_durable_acceptance_or_worker(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    context = Context(user_id=hass_admin_user.id)
    source = await _stage(runtime, monkeypatch, tmp_path, context)
    plan = await _call(
        runtime,
        "plan_hourly_history",
        _plan_request(runtime, source["source_id"]),
        context,
    )
    native = {}

    async def snapshot(statistic_id, start, end):
        metadata, rows = native.get(statistic_id, (None, ()))
        return RecorderSnapshot(
            tuple(row for row in rows if start <= row.start < end),
            deepcopy(metadata),
            complete=True,
        )

    def submit(metadata, rows):
        native[metadata["statistic_id"]] = (deepcopy(metadata), rows)

    monkeypatch.setattr(runtime.recorder, "async_snapshot_range", snapshot)
    write = Mock(side_effect=submit)
    monkeypatch.setattr(runtime.recorder, "submit", write)
    accepted, proceed, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    native_save = runtime.store.async_save

    async def paused_durable_save(data):
        await native_save(data)
        if data.get("version") != 3:
            return
        if data["phase"] == "prepared" and not accepted.is_set():
            accepted.set()
            await proceed.wait()
        if data["operation"]["status"] == "completed":
            completed.set()

    monkeypatch.setattr(runtime.store, "async_save", paused_durable_save)
    task = asyncio.create_task(
        _call(
            runtime,
            "apply_hourly_history",
            {
                "config_entry_id": runtime.entry.entry_id,
                "plan_digest": plan["plan_digest"],
                "plan": plan["request"],
            },
            context,
        )
    )
    await asyncio.wait_for(accepted.wait(), 2)
    assert (await runtime.store.async_load())["phase"] == "prepared"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proceed.set()
    await asyncio.wait_for(completed.wait(), 2)
    await asyncio.wait_for(runtime.importer._history_worker, 2)
    state = await runtime.store.async_load()
    assert state["operation"]["status"] == "completed"
    assert state["operation"]["plan_digest"] == plan["plan_digest"]
    assert state["pending"] is None
    assert runtime.entry.data[MARKER]["version"] == 3
    write.assert_called_once()
    assert write.call_args.args[1][0].mean == 25.0
    before_entry = deepcopy(dict(runtime.entry.data))
    no_save = AsyncMock(side_effect=AssertionError("Query attempted journal save"))
    no_object = AsyncMock(side_effect=AssertionError("Query attempted object save"))
    monkeypatch.setattr(runtime.store, "async_save", no_save)
    monkeypatch.setattr(runtime.store, "async_write_object", no_object)
    coverage = await _call(
        runtime,
        "get_hourly_coverage",
        _request(runtime, "get_hourly_coverage"),
        context,
    )
    configuration = await _call(
        runtime, "get_configuration", _request(runtime, "get_configuration"), context
    )
    assert coverage["series"][0]["buckets"][0]["value"] == 0.25
    assert configuration["hourly_statistics"]["history_v3"]["operation"]["status"] == (
        "completed"
    )
    assert await runtime.store.async_load() == state
    assert runtime.entry.data == before_entry
    no_save.assert_not_called()
    no_object.assert_not_called()
    write.assert_called_once()
    _assert_no_source_io(runtime)


@pytest.mark.usefixtures("unload_entries")
async def test_retired_coverage_and_selection_routes_fail_before_effects(
    hass, freezer, monkeypatch, tmp_path
):
    runtime = await _runtime(hass, freezer, monkeypatch, tmp_path)
    request = {
        "config_entry_id": runtime.entry.entry_id,
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    with pytest.raises(probatio.Invalid):
        await _call(runtime, "get_hourly_coverage", request, Context())
    assert not hass.services.has_service(DOMAIN, "select_hourly_statistics")
    assert runtime.importer.hourly.status()["mode"] == "unloaded"
    runtime.snapshots.assert_not_called()
    runtime.submit.assert_not_called()
    _assert_no_source_io(runtime)
