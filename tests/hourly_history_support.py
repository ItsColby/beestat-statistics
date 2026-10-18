"""Real service, journal and runtime fixtures for qualified hourly history tests."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers.storage import Store

from custom_components import beestat_statistics as integration
from custom_components.beestat_statistics import importer as importer_module
from custom_components.beestat_statistics.api import (
    BeestatRawResponse,
    BeestatReadAttempt,
)
from custom_components.beestat_statistics.config_model import ConfiguredSensor
from custom_components.beestat_statistics.const import DOMAIN
from custom_components.beestat_statistics.hourly_history_contract import (
    DAILY_POLICY,
)
from custom_components.beestat_statistics.hourly_rows import RecorderSnapshot
from tests.runtime_support import _coordinator_data

NOW = datetime(2026, 9, 10, 18, 30, tzinfo=UTC)
START = NOW.replace(hour=17, minute=0)
END = START + timedelta(hours=1)
QUANTITY = "thermostat:1:fan_runtime_hours"
_NATIVE_STORE_WRITE = Store._async_write_data


async def _runtime(hass, freezer, monkeypatch, tmp_path):
    """Use real service, entry, importer, journal and planning implementations."""
    freezer.move_to(NOW)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=NOW, data_end=END)
    importer = importer_module.BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    entry.runtime_data = SimpleNamespace(
        client=client, coordinator=coordinator, importer=importer
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    store = importer.hourly._store
    monkeypatch.setattr(store._store, "path", str(tmp_path / "hourly-journal.json"))
    monkeypatch.setattr(
        store._store,
        "_async_write_data",
        _NATIVE_STORE_WRITE.__get__(store._store),
    )
    forbidden = []
    for owner, methods in (
        (
            client,
            (
                "async_sync_runtime",
                "async_sync_resource",
                "async_read_id",
                "async_read_runtime_thermostat_summary",
                "async_read_runtime_thermostat",
                "async_read_runtime_sensor",
            ),
        ),
        (coordinator, ("async_refresh_runtime",)),
    ):
        for method in methods:
            spy = AsyncMock(side_effect=AssertionError(f"Source I/O: {method}"))
            monkeypatch.setattr(owner, method, spy, raising=False)
            forbidden.append(spy)
    recorder = importer.hourly._recorder
    snapshots = AsyncMock(return_value=RecorderSnapshot(complete=True))
    submit = Mock(side_effect=AssertionError("Unrequested Recorder write"))
    monkeypatch.setattr(recorder, "async_snapshot_range", snapshots)
    monkeypatch.setattr(recorder, "submit", submit)
    assert await integration.async_setup(hass, {})
    return SimpleNamespace(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        client=client,
        importer=importer,
        store=store,
        recorder=recorder,
        snapshots=snapshots,
        submit=submit,
        forbidden=forbidden,
    )


def _manifest(runtime):
    identity = runtime.importer._history_context()["identity"]
    return {
        "contract_version": 3,
        "config_entry_id": runtime.entry.entry_id,
        "api_base": identity["api_base"],
        "account_anchors": identity["account_anchors"],
        "resource": "runtime_thermostat",
        "resource_id": 1,
        "thermostat_id": 1,
        "source_kind": "provider",
        "acquisition_id": "service-fixture-points",
        "chunk_index": 0,
        "chunk_count": 1,
        "format": "json",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "acquired_at": NOW.isoformat(),
        "source_end": END.isoformat(),
        "unit_contract": "beestat_points_v1",
        "original_sha256": sha256(_content()).hexdigest(),
        "original_byte_count": len(_content()),
        "chunk_byte_offset": 0,
    }


def _content():
    return json.dumps(
        [
            {
                "thermostat_id": 1,
                "timestamp": (START + timedelta(minutes=5 * index)).isoformat(),
                "fan": 75,
            }
            for index in range(12)
        ]
    ).encode()


def _plan_request(runtime, source_id="0" * 64):
    return {
        "config_entry_id": runtime.entry.entry_id,
        "source_ids": [source_id],
        "quantity_ids": [QUANTITY],
        "start": START.isoformat(),
        "end": END.isoformat(),
        "archive_policy": "reject",
        "daily_policy": DAILY_POLICY,
        "operation_id": "native-service-operation",
        "expected_revision": 0,
        "consumer_contract": {
            "contract_version": 3,
            "consumers": [
                {
                    "consumer_id": "fixture-history-consumer",
                    "version": "fixture-v3",
                    "history_contract": 3,
                    "daily_policy": DAILY_POLICY,
                }
            ],
        },
        "recovery_reference": "fixture-recovery-set",
    }


def _request(runtime, service):
    entry_id = runtime.entry.entry_id
    if service == "stage_hourly_source":
        return {
            "config_entry_id": entry_id,
            "file_id": "opaque-upload-fixture",
            "sha256": sha256(_content()).hexdigest(),
            "manifest": _manifest(runtime),
        }
    if service == "plan_hourly_history":
        return _plan_request(runtime)
    if service == "apply_hourly_history":
        return {
            "config_entry_id": entry_id,
            "plan_digest": "0" * 64,
            "plan": _plan_request(runtime),
        }
    if service == "get_configuration":
        return {"config_entry_id": entry_id}
    return {
        "config_entry_id": entry_id,
        "contract_version": 3,
        "quantity_ids": [QUANTITY],
        "start": START.isoformat(),
        "end": END.isoformat(),
    }


async def _call(runtime, service, request, context):
    return await runtime.hass.services.async_call(
        DOMAIN,
        service,
        request,
        blocking=True,
        return_response=True,
        context=context,
    )


def _upload(runtime, monkeypatch, tmp_path, content, *, verify_retained):
    """Replace only the native upload lease, retaining its destructive cleanup."""
    upload_path = tmp_path / "uploaded-points.json"
    upload_path.write_bytes(content)
    loop_thread = threading.get_ident()
    events = []

    @contextmanager
    def process_upload(hass, file_id):
        assert hass is runtime.hass
        assert file_id == "opaque-upload-fixture"
        assert threading.get_ident() != loop_thread
        events.append("entered")
        try:
            yield upload_path
            assert threading.get_ident() != loop_thread
            if verify_retained:
                digest = sha256(content).hexdigest()
                assert runtime.store._read_object("source", digest) == content
                manifests = list(
                    Path(f"{runtime.store.path}.objects").joinpath("manifest").iterdir()
                )
                assert len(manifests) == 1
                sealed = json.loads(
                    runtime.store._read_object("manifest", manifests[0].name)
                )
                assert sealed["sha256"] == digest
                assert sealed["byte_count"] == len(content)
                events.append("durably_retained")
        finally:
            assert threading.get_ident() != loop_thread
            upload_path.unlink()
            events.append("cleaned")

    monkeypatch.setattr(importer_module, "process_uploaded_file", process_upload)
    return upload_path, events


async def _stage(runtime, monkeypatch, tmp_path, context):
    _upload(runtime, monkeypatch, tmp_path, _content(), verify_retained=True)
    return await _call(
        runtime,
        "stage_hourly_source",
        _request(runtime, "stage_hourly_source"),
        context,
    )


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
