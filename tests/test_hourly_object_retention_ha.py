"""Bounded retention of superseded v3 proofs, operations and integration deltas."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.beestat_statistics import backup
from custom_components.beestat_statistics import hourly_history_writer as writer_module
from custom_components.beestat_statistics.hourly_history_contract import (
    canonical_json,
    digest,
    utc,
)
from custom_components.beestat_statistics.hourly_history_writer import HistoryWriter
from custom_components.beestat_statistics.hourly_import import (
    HISTORY_FENCE_PREFIX,
    HourlyImportManager,
)
from custom_components.beestat_statistics.hourly_sources import _provider_precedence
from custom_components.beestat_statistics.hourly_storage import (
    HourlyStorageError,
    HourlyStore,
)
from tests.test_hourly_history_runtime_ha import (
    _adopted,
    _finish,
    _logical_hour,
    _objects,
    _provider,
)
from tests.test_hourly_history_service_ha import END, QUANTITY, START, _content

_FENCE = canonical_json({"kind": "history_invalidated_root", "token": "0" * 64})


@pytest.fixture
def object_store(hass: HomeAssistant, tmp_path):
    store = HourlyStore(hass, "retention-contract")
    with patch.object(store._store, "path", str(tmp_path / "hourly-journal.json")):
        yield store


async def _collect(store, keep, *, prefix=HISTORY_FENCE_PREFIX, closed=lambda: False):
    return await store.async_collect_objects(
        keep, retained_prefix=prefix, closed=closed
    )


async def _write(store, kind, *contents):
    return [await store.async_write_object(kind, content) for content in contents]


def _kind(store, kind):
    directory = Path(f"{store.path}.objects") / kind
    return sorted(path.name for path in directory.iterdir())


async def test_collection_removes_only_unreferenced_proof_and_operation_objects(
    object_store,
):
    kept_proof, old_proof = await _write(object_store, "proof", b'{"p":1}', b'{"p":0}')
    kept_operation, old_operation, fence = await _write(
        object_store, "operation", b'{"o":1}', b'{"o":0}', _FENCE
    )
    sources = await _write(object_store, "source", b"original")
    manifests = await _write(object_store, "manifest", b'{"m":0}')
    removed = await _collect(
        object_store, {"proof": {kept_proof}, "operation": {kept_operation}}
    )
    assert removed == 2
    assert _kind(object_store, "proof") == [kept_proof]
    assert _kind(object_store, "operation") == sorted({kept_operation, fence})
    assert _kind(object_store, "source") == sources
    assert _kind(object_store, "manifest") == manifests
    assert old_proof not in _kind(object_store, "proof")
    assert old_operation not in _kind(object_store, "operation")


async def test_collection_removes_only_stale_interrupted_writes(object_store):
    (kept,) = await _write(object_store, "proof", b"{}")
    directory = Path(f"{object_store.path}.objects") / "proof"
    stale, fresh = directory / ".pending-stale", directory / ".pending-fresh"
    stale.write_bytes(b"partial")
    fresh.write_bytes(b"in flight")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    assert await _collect(object_store, {"proof": {kept}, "operation": set()}) == 0
    assert not stale.exists()
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        {kept, fresh.name}
    )


async def test_collection_rejects_partial_scope_and_linked_namespace(
    object_store, tmp_path
):
    with pytest.raises(HourlyStorageError, match="scope_invalid"):
        await _collect(object_store, {"proof": set(), "source": set()})
    external = tmp_path / "other-owner"
    external.mkdir()
    (external / ("a" * 64)).write_bytes(b"foreign")
    root = Path(f"{object_store.path}.objects")
    root.mkdir()
    (root / "proof").symlink_to(external, target_is_directory=True)
    with pytest.raises(HourlyStorageError, match="path_invalid"):
        await _collect(object_store, {"proof": set(), "operation": set()})
    assert list(external.iterdir()) == [external / ("a" * 64)]


async def test_backup_pause_waits_for_a_running_sweep_and_blocks_new_ones(
    hass, object_store
):
    (old,) = await _write(object_store, "proof", b"superseded")
    started, release = threading.Event(), threading.Event()
    original = object_store._collect_objects

    def slow(*args):
        started.set()
        release.wait(5)
        return original(*args)

    keep = {"proof": set(), "operation": set()}
    with patch.object(object_store, "_collect_objects", slow):
        sweep = asyncio.ensure_future(_collect(object_store, keep))
        await hass.async_add_executor_job(started.wait, 5)
        pause = asyncio.ensure_future(backup.async_pre_backup(hass))
        await asyncio.sleep(0.05)
        assert not pause.done()
        release.set()
        await pause
        # The sweep saw the pause before deleting anything.
        assert await sweep == 0
    assert _kind(object_store, "proof") == [old]
    assert await _collect(object_store, keep) == 0
    await backup.async_post_backup(hass)
    assert await _collect(object_store, keep) == 1


async def test_replaced_writer_waits_for_and_closes_a_running_sweep(hass, object_store):
    (old,) = await _write(object_store, "proof", b"superseded")
    replacement = HourlyStore(hass, "retention-contract")
    started, release = threading.Event(), threading.Event()
    original = object_store._collect_objects
    closed = False

    def slow(*args):
        started.set()
        release.wait(5)
        return original(*args)

    keep = {"proof": set(), "operation": set()}
    with (
        patch.object(replacement._store, "path", object_store.path),
        patch.object(object_store, "_collect_objects", slow),
    ):
        sweep = asyncio.ensure_future(
            _collect(object_store, keep, closed=lambda: closed)
        )
        await hass.async_add_executor_job(started.wait, 5)
        # The reloaded writer's new generation waits out the stale keep set.
        write = asyncio.ensure_future(
            replacement.async_write_object("proof", b"current")
        )
        await asyncio.sleep(0.05)
        assert not write.done()
        release.set()
        assert await sweep == 1
        current = await write
        assert _kind(object_store, "proof") == [current]
        # An unloaded writer's running sweep stops, and it starts no new one.
        started.clear()
        release.clear()
        sweep = asyncio.ensure_future(
            _collect(object_store, keep, closed=lambda: closed)
        )
        await hass.async_add_executor_job(started.wait, 5)
        closed = True
        release.set()
        assert await sweep == 0
        assert await _collect(object_store, keep, closed=lambda: closed) == 0
    assert old != current
    assert _kind(object_store, "proof") == [current]


async def test_collection_keeps_the_fence_the_writer_persists(hass, object_store):
    manager = HourlyImportManager(
        hass,
        SimpleNamespace(entry_id="retention-contract"),
        store=object_store,
        recorder=object(),
    )
    fence = await manager._invalidate_history_root("1" * 64)
    (stale,) = await _write(object_store, "operation", b'{"o":0}')
    keep = {"proof": set(), "operation": set()}
    assert await _collect(object_store, keep) == 1
    assert _kind(object_store, "operation") == [fence]
    assert stale != fence


async def test_one_failing_file_does_not_stop_the_sweep(object_store):
    # Operations sweep first, so a locked operation must not strand proofs.
    operations = await _write(object_store, "operation", b'{"o":0}', b'{"o":1}')
    proofs = await _write(object_store, "proof", b'{"g":0}', b'{"g":1}', b'{"g":2}')
    original = Path.unlink
    calls = []

    def locked_first(path, missing_ok=False):
        calls.append(path.name)
        if len(calls) == 1:
            raise PermissionError("fixture lock")
        return original(path, missing_ok=missing_ok)

    keep = {"proof": set(), "operation": set()}
    with (
        patch.object(Path, "unlink", locked_first),
        pytest.raises(PermissionError),
    ):
        await _collect(object_store, keep)
    assert _kind(object_store, "operation") == [calls[0]]
    assert _kind(object_store, "proof") == []
    assert sorted(calls) == sorted(operations + proofs)
    assert await _collect(object_store, keep) == 1
    assert _kind(object_store, "operation") == []


async def test_collection_deletes_named_retired_objects_in_order(object_store):
    (original,) = await _write(object_store, "source", b"delta")
    (manifest,) = await _write(object_store, "manifest", b'{"m":0}')
    (kept,) = await _write(object_store, "source", b"staged")
    retired = [("source", original), ("manifest", manifest), ("source", "e" * 64)]
    keep = {"proof": set(), "operation": set()}
    assert await object_store.async_object_ids("manifest") == {manifest}
    removed = await object_store.async_collect_objects(
        keep,
        retained_prefix=HISTORY_FENCE_PREFIX,
        closed=lambda: False,
        retired=retired,
    )
    assert removed == 2
    assert _kind(object_store, "source") == [kept]
    assert _kind(object_store, "manifest") == []
    for invalid in (("proof", kept), ("source", "not-a-digest")):
        with pytest.raises(HourlyStorageError, match="scope_invalid"):
            await object_store.async_collect_objects(
                keep,
                retained_prefix=HISTORY_FENCE_PREFIX,
                closed=lambda: False,
                retired=[invalid],
            )


async def test_collection_keeps_the_manifest_of_an_original_it_cannot_delete(
    object_store,
):
    (original,) = await _write(object_store, "source", b"delta")
    (manifest,) = await _write(object_store, "manifest", b'{"m":1}')
    unlink = Path.unlink

    def refuse(path, *args, **kwargs):
        if path.name == original:
            raise PermissionError(path)
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", refuse), pytest.raises(PermissionError):
        await object_store.async_collect_objects(
            {"proof": set(), "operation": set()},
            retained_prefix=HISTORY_FENCE_PREFIX,
            closed=lambda: False,
            retired=[("source", original), ("manifest", manifest)],
        )
    assert _kind(object_store, "source") == [original]
    assert _kind(object_store, "manifest") == [manifest]


def _rows(open_hours):
    rows = json.loads(_content())
    for hour in range(open_hours):
        first = END + timedelta(hours=hour)
        rows.extend(
            {
                "thermostat_id": 1,
                "timestamp": (first + timedelta(minutes=5 * index)).isoformat(),
                "fan": 75,
            }
            for index in range(12)
        )
    return rows


def _horizon(runtime, at):
    data = runtime.coordinator.data
    runtime.coordinator.data = replace(
        data,
        thermostat_rows=tuple(
            {**row, "data_end": at.isoformat()} for row in data.thermostat_rows
        ),
    )


async def _pass(runtime):
    async with runtime.importer._lock:
        return await runtime.importer._async_refresh_hourly_history(
            lookback_days=1, rebuilding=False
        )


def _by_kind(objects):
    return Counter(Path(path).parts[0] for path in objects)


async def _accepted_pass(runtime, monkeypatch, *, rows):
    revision = (await runtime.store.async_load())["revision"]
    _provider(runtime, monkeypatch, rows=rows)
    assert await _pass(runtime) is None
    root = await runtime.store.async_load()
    assert root["operation"]["status"] == "completed"
    # Each pass commits a new generation, superseding the previous one.
    assert root["revision"] > revision
    return _objects(runtime)


async def test_repeated_changed_and_unchanged_passes_keep_storage_bounded(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    seen = set(_objects(runtime))
    sources = {path for path in seen if path.startswith("source/")}
    generations = []
    for hour in range(4):
        # Changed: a new open hour of provider points becomes a delta source.
        freezer.move_to(END + timedelta(hours=hour, minutes=59))
        _horizon(runtime, END + timedelta(hours=hour, minutes=55))
        objects = await _accepted_pass(runtime, monkeypatch, rows=_rows(hour + 1))
        current = {path for path in objects if path.startswith("source/")}
        assert len(current - sources) == 1
        sources = current
        generations.append(_by_kind(objects))
        seen |= set(objects)
        # Unchanged: identical points, but the hour closes and is re-proven.
        freezer.move_to(END + timedelta(hours=hour + 1, minutes=1))
        objects = await _accepted_pass(runtime, monkeypatch, rows=_rows(hour + 1))
        assert {path for path in objects if path.startswith("source/")} == sources
        generations.append(_by_kind(objects))
        seen |= set(objects)
    assert len({(item["proof"], item["operation"]) for item in generations}) == 1
    collected = _by_kind(seen - set(_objects(runtime)))
    assert collected["proof"] >= len(generations) - 1
    assert collected["operation"] >= len(generations) - 1
    assert collected["source"] == collected["manifest"] == 0
    for start in (START, *(END + timedelta(hours=hour) for hour in range(4))):
        bucket = await _logical_hour(
            runtime, start=start, end=start + timedelta(hours=1)
        )
        assert (bucket["reason"], bucket["value"]) == ("ready", 0.25)
    # A fresh manager recovers from the durable root and retained objects alone.
    runtime.importer.hourly = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    assert (await _logical_hour(runtime))["value"] == 0.25
    # The import pass resolves the marker and loads the root before refreshing.
    await runtime.importer.hourly.async_mode()
    freezer.move_to(END + timedelta(hours=4, minutes=59))
    _horizon(runtime, END + timedelta(hours=4, minutes=55))
    await _accepted_pass(runtime, monkeypatch, rows=_rows(5))
    await runtime.entry._async_process_on_unload(hass)


async def test_collection_waits_for_pending_operations_and_backups(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    garbage = await runtime.store.async_write_object("proof", b'{"superseded":true}')
    objects = _objects(runtime)
    async with runtime.importer._lock:
        assert (
            await runtime.importer.hourly.async_collect_history_objects(
                context=runtime.importer._history_context()
            )
            == 0
        )
    assert _objects(runtime) == objects
    await backup.async_pre_backup(hass)
    assert await _pass(runtime) == "history_operation_pending"
    await runtime.importer._history_worker
    assert runtime.importer.hourly.history_status()["status"] == "completed"
    assert f"proof/{garbage}" in _objects(runtime)
    await backup.async_post_backup(hass)
    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    objects = await _accepted_pass(runtime, monkeypatch, rows=_rows(1))
    assert f"proof/{garbage}" not in objects
    await runtime.entry._async_process_on_unload(hass)


async def test_interrupted_collection_is_inert_and_finishes_on_the_next_pass(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    garbage = {
        await runtime.store.async_write_object("proof", f'{{"g":{index}}}'.encode())
        for index in range(3)
    }
    original = Path.unlink
    calls = 0

    def crash_after_first(path, missing_ok=False):
        nonlocal calls
        # Only collection deletes digest-named objects; writes unlink temporaries.
        if len(path.name) == 64:
            calls += 1
            if calls > 1:
                raise OSError("fixture interruption")
        return original(path, missing_ok=missing_ok)

    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    with patch.object(Path, "unlink", crash_after_first):
        objects = await _accepted_pass(runtime, monkeypatch, rows=_rows(1))
    # Each failed file is skipped, not fatal to the rest of the sweep.
    assert calls > 2
    assert {f"proof/{name}" for name in garbage} & set(objects)
    assert (await _logical_hour(runtime))["value"] == 0.25
    freezer.move_to(END + timedelta(hours=1, minutes=1))
    objects = await _accepted_pass(runtime, monkeypatch, rows=_rows(1))
    assert not {f"proof/{name}" for name in garbage} & set(objects)
    bucket = await _logical_hour(runtime, start=END, end=END + timedelta(hours=1))
    assert (bucket["reason"], bucket["value"]) == ("ready", 0.25)
    await runtime.entry._async_process_on_unload(hass)


async def _routine(runtime, monkeypatch, rows, *, days=12):
    # Twelve days span two UTC months in one provider request.
    _provider(runtime, monkeypatch, rows=rows)
    async with runtime.importer._lock:
        assert (
            await runtime.importer._async_refresh_hourly_history(
                lookback_days=days, rebuilding=False
            )
            is None
        )
    return await runtime.store.async_load()


def _read(runtime, kind, reference):
    return json.loads(
        Path(f"{runtime.store.path}.objects/{kind}/{reference}").read_bytes()
    )


async def _reseal(runtime, root):
    body = {key: value for key, value in root.items() if key != "integrity"}
    await runtime.store.async_save({**body, "integrity": digest(body)})


def _proofs(root):
    return dict(root["history"]["selections"][QUANTITY]["proofs"])


async def test_routine_pass_rewrites_only_touched_months_and_hours(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    first = await _routine(runtime, monkeypatch, _rows(1))
    assert set(_proofs(first)) == {"2026-08", "2026-09"}
    freezer.move_to(END + timedelta(hours=1, minutes=59))
    _horizon(runtime, END + timedelta(hours=1, minutes=55))
    before = set(_objects(runtime))
    second = await _routine(runtime, monkeypatch, _rows(2))
    # The untouched month keeps its exact proof and gets no batch or catalog.
    assert _proofs(second)["2026-08"] == _proofs(first)["2026-08"]
    added = set(_objects(runtime)) - before
    assert _by_kind(added)["proof"] == 1
    for path in added:
        kind, reference = Path(path).parts
        if kind == "operation":
            assert _read(runtime, kind, reference).get("month") != "2026-08"
    manifest = _read(runtime, "operation", second["operation"]["object"])
    (batch,) = manifest["batches"]
    assert batch["month"] == "2026-09"
    assert END <= utc(batch["start"]) < utc(batch["end"]) <= END + timedelta(hours=2)
    # Same points within the same hour: nothing is planned, saved or written.
    objects = _objects(runtime)
    freezer.move_to(END + timedelta(hours=1, minutes=59, seconds=30))
    assert (await _routine(runtime, monkeypatch, _rows(2)))["revision"] == (
        second["revision"]
    )
    assert _objects(runtime) == objects
    await runtime.entry._async_process_on_unload(hass)


async def test_past_hour_correction_reproves_only_its_hours(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    first = await _routine(runtime, monkeypatch, _rows(1))
    corrected = [
        {**row, "fan": 150} if utc(row["timestamp"]) < END else row for row in _rows(1)
    ]
    root = await _routine(runtime, monkeypatch, corrected)
    manifest = _read(runtime, "operation", root["operation"]["object"])
    (batch,) = manifest["batches"]
    assert (batch["month"], batch["start"], batch["end"]) == (
        "2026-09",
        START.isoformat(),
        END.isoformat(),
    )
    assert _proofs(root)["2026-08"] == _proofs(first)["2026-08"]
    bucket = await _logical_hour(runtime)
    assert (bucket["reason"], bucket["value"]) == ("ready", 0.5)
    await runtime.entry._async_process_on_unload(hass)


@pytest.mark.parametrize("embedded", [True, False])
async def test_saved_intent_of_either_form_resumes(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, embedded
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    # Earlier releases embedded the whole batch; intent now names its object.
    root = await runtime.store.async_load()
    manifest = _read(runtime, "operation", root["operation"]["object"])
    summary = manifest["batches"][0]
    batch = _read(runtime, "operation", summary["object"])
    pending = {
        "generation": 3,
        "operation_id": root["operation"]["operation_id"],
        "index": 0,
        "batch_object": summary["object"],
        **(
            {"batch": batch}
            if embedded
            else {key: batch[key] for key in ("quantity_id", "start", "end")}
        ),
    }
    pending["digest"] = digest(pending)
    root.update(pending=pending, revision=root["revision"] + 1)
    root["operation"]["status"] = "in_progress"
    await _reseal(runtime, root)
    runtime.importer.hourly = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    await runtime.importer.hourly.async_mode()
    await _finish(runtime)
    assert (await _logical_hour(runtime))["value"] == 0.25
    await runtime.entry._async_process_on_unload(hass)


def _batches(runtime, root):
    manifest = _read(runtime, "operation", root["operation"]["object"])
    return manifest, [
        _read(runtime, "operation", summary["object"])
        for summary in manifest["batches"]
    ]


async def test_each_proof_is_stored_once_and_named_by_its_batch(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    _, batches = _batches(runtime, await runtime.store.async_load())
    sealed = {batch["month"]: batch["proof_object"] for batch in batches}
    assert batches and not any("proof" in batch for batch in batches)
    # Sealing writes each proof; verifying a batch only publishes its name.
    objects = _objects(runtime)
    assert {f"proof/{reference}" for reference in sealed.values()} <= set(objects)
    await _finish(runtime)
    assert _proofs(await runtime.store.async_load()) == sealed
    assert _by_kind(set(_objects(runtime)) - set(objects))["proof"] == 0
    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    root = await _routine(runtime, monkeypatch, _rows(1))
    _, batches = _batches(runtime, root)
    assert batches and not any("proof" in batch for batch in batches)
    for batch in batches:
        assert _proofs(root)[batch["month"]] == batch["proof_object"]
    await runtime.entry._async_process_on_unload(hass)


@pytest.mark.parametrize("interrupted", [False, True])
async def test_batches_that_embed_their_proof_still_replay(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, interrupted
):
    runtime = await _adopted(
        hass, hass_admin_user, freezer, monkeypatch, tmp_path, finish=False
    )
    # v2026.10.10 and v2026.10.11 sealed the proof inside each batch and wrote
    # the proof object only once the batch verified.
    root = await runtime.store.async_load()
    manifest, batches = _batches(runtime, root)
    expected = {}
    for summary, batch in zip(manifest["batches"], batches, strict=True):
        reference = batch.pop("proof_object")
        batch["proof"] = _read(runtime, "proof", reference)
        Path(f"{runtime.store.path}.objects/proof/{reference}").unlink()
        summary["object"] = await runtime.store.async_write_object(
            "operation", canonical_json(batch)
        )
        expected[batch["month"]] = reference
    root["operation"]["object"] = await runtime.store.async_write_object(
        "operation", canonical_json(manifest)
    )
    root["operation"]["plan_digest"] = digest(manifest)
    if interrupted:
        # The intent was saved, then Home Assistant stopped before submission.
        pending = {
            "generation": 3,
            "operation_id": root["operation"]["operation_id"],
            "index": 0,
            "batch_object": manifest["batches"][0]["object"],
            **{key: batches[0][key] for key in ("quantity_id", "start", "end")},
        }
        pending["digest"] = digest(pending)
        root.update(pending=pending)
        root["operation"]["status"] = "in_progress"
    root["revision"] += 1
    await _reseal(runtime, root)
    runtime.importer.hourly = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    await runtime.importer.hourly.async_mode()
    await _finish(runtime)
    assert _proofs(await runtime.store.async_load()) == expected
    assert {f"proof/{reference}" for reference in expected.values()} <= set(
        _objects(runtime)
    )
    assert (await _logical_hour(runtime))["value"] == 0.25
    await runtime.entry._async_process_on_unload(hass)


async def test_proofs_from_earlier_releases_are_reproved_once(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    freezer.move_to(END + timedelta(minutes=59))
    _horizon(runtime, END + timedelta(minutes=55))
    root = await _routine(runtime, monkeypatch, _rows(1))
    # Earlier releases bound every proof to the whole-window source revision.
    selection = root["history"]["selections"][QUANTITY]
    for month, reference in _proofs(root).items():
        legacy = {
            **_read(runtime, "proof", reference),
            "source_revision": root["history"]["source_revision"],
        }
        selection["proofs"][month] = await runtime.store.async_write_object(
            "proof", canonical_json(legacy)
        )
    root["revision"] += 1
    await _reseal(runtime, root)
    runtime.importer.hourly = HourlyImportManager(
        hass, runtime.entry, store=runtime.store, recorder=runtime.recorder
    )
    await runtime.importer.hourly.async_mode()
    assert (await _logical_hour(runtime))["value"] == 0.25
    freezer.move_to(END + timedelta(hours=1, minutes=59))
    _horizon(runtime, END + timedelta(hours=1, minutes=55))
    root = await _routine(runtime, monkeypatch, _rows(2))
    current = _proofs(root)
    # The untouched month moves to the current form too, then stays stable.
    for reference in current.values():
        assert "source_revision" not in _read(runtime, "proof", reference)
    freezer.move_to(END + timedelta(hours=2, minutes=59))
    _horizon(runtime, END + timedelta(hours=2, minutes=55))
    root = await _routine(runtime, monkeypatch, _rows(3))
    assert _proofs(root)["2026-08"] == current["2026-08"]
    assert (await _logical_hour(runtime))["value"] == 0.25
    await runtime.entry._async_process_on_unload(hass)


def test_supersession_keeps_only_nearest_predecessors_with_the_same_closure():
    resource = "runtime_thermostat:1"
    # Earlier releases listed every predecessor, growing quadratically.
    existing = {resource: {"b": ["a"], "c": ["a", "b"]}}
    fresh = [
        {
            "manifest": {
                "source_kind": "provider",
                "resource": "runtime_thermostat",
                "resource_id": 1,
                "acquisition_id": "d",
            }
        }
    ]
    baseline = {"acquisitions": {resource: ["a", "b", "c"]}}
    writer = HistoryWriter.__new__(HistoryWriter)
    result = writer.supersession(existing, fresh, baseline)
    assert result == {resource: {"b": ["a"], "c": ["b"], "d": ["c"]}}
    full = {resource: {**existing[resource], "d": ["a", "b", "c"]}}
    roots = {resource: {"a", "b", "c", "d"}}
    assert _provider_precedence({}, result, roots) == _provider_precedence(
        {}, full, roots
    )


def _series(since, until):
    # Provider reads return only the requested window's points.
    first = max(START, since)
    return [
        {
            "thermostat_id": 1,
            "timestamp": (first + timedelta(minutes=5 * index)).isoformat(),
            "fan": 75,
        }
        for index in range(int((until - first) / timedelta(minutes=5)))
    ]


async def test_daily_compaction_bounds_selected_and_retained_sources(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    passes, days, lookback = 3, 4, 3
    selected, retained = [], []
    for step in range(1, passes * days + 1):
        now = END + timedelta(hours=24 / passes * step)
        freezer.move_to(now + timedelta(minutes=1))
        _horizon(runtime, now)
        rows = _series(now - timedelta(days=lookback), now)
        root = await _routine(runtime, monkeypatch, rows, days=lookback)
        selected.append(len(root["history"]["source_ids"]))
        kinds = _by_kind(_objects(runtime))
        assert kinds["source"] == kinds["manifest"]
        retained.append(kinds["manifest"])
    # Each closed day collapses to one delta and superseded deltas are deleted,
    # so selection stays within the window and retention grows by one per day.
    assert max(selected) <= lookback + passes
    assert retained[-1] - retained[-1 - passes] == 1
    assert retained[-1] <= 1 + days + passes
    for start in (START, END + timedelta(days=1), END + timedelta(days=days - 1)):
        bucket = await _logical_hour(
            runtime, start=start, end=start + timedelta(hours=1)
        )
        assert (bucket["reason"], bucket["value"]) == ("ready", 0.25)
    await runtime.entry._async_process_on_unload(hass)


async def test_refused_retirement_keeps_every_source_until_a_later_pass_retires(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path, caplog
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    resolution = writer_module.source_resolution
    calls = iter(range(1_000))
    refused = True
    monkeypatch.setattr(
        writer_module,
        "source_resolution",
        lambda *args, **kwargs: next(calls) if refused else resolution(*args, **kwargs),
    )
    passes, lookback = 3, 3
    selected = []
    for step in range(1, 2 * passes + 2):
        refused = step <= 2 * passes
        now = END + timedelta(hours=24 / passes * step)
        freezer.move_to(now + timedelta(minutes=1))
        _horizon(runtime, now)
        rows = _series(now - timedelta(days=lookback), now)
        root = await _routine(runtime, monkeypatch, rows, days=lookback)
        selected.append(len(root["history"]["source_ids"]))
    # Refused passes keep every delta selected, like roots from earlier
    # releases; the next pass that may retire returns within the bound.
    assert selected[:-1] == sorted(set(selected[:-1]))
    assert selected[-2] > lookback + passes >= selected[-1]
    assert "would change resolved history" in caplog.text
    assert (await _logical_hour(runtime))["value"] == 0.25
    await runtime.entry._async_process_on_unload(hass)


async def _sealed(runtime, *, revision=None, original=None):
    """Write a manifest that is a delta on ``revision``, or a staged original."""
    if original is None:
        original = await runtime.store.async_write_object(
            "source", f"payload-{revision}".encode()
        )
    delta = (
        None
        if revision is None
        else {"format": "integration_delta_v1", "baseline_source_revision": revision}
    )
    sealed = {
        "sha256": original,
        "confidence": "integration_delta_v1" if delta else "provider_ordered",
        "delta": delta,
    }
    return original, await runtime.store.async_write_object(
        "manifest", canonical_json(sealed)
    )


async def test_collection_deletes_only_unreachable_deltas_on_superseded_baselines(
    hass, hass_admin_user, freezer, monkeypatch, tmp_path
):
    runtime = await _adopted(hass, hass_admin_user, freezer, monkeypatch, tmp_path)
    root = await runtime.store.async_load()
    current = root["history"]["source_revision"]
    stale = await _sealed(runtime, revision="0" * 64)
    pending = await _sealed(runtime, revision=current)
    staged = await _sealed(runtime)
    # An original another, reachable manifest names outlives its stale delta.
    reachable = _read(runtime, "manifest", root["history"]["source_ids"][0])
    shared = await _sealed(runtime, revision="1" * 64, original=reachable["sha256"])
    async with runtime.importer._lock:
        assert (
            await runtime.importer.hourly.async_collect_history_objects(
                context=runtime.importer._history_context()
            )
            >= 3
        )
    objects = _objects(runtime)
    assert {
        f"manifest/{stale[1]}",
        f"manifest/{shared[1]}",
        f"source/{stale[0]}",
    }.isdisjoint(objects)
    for original, manifest in (pending, staged):
        assert {f"source/{original}", f"manifest/{manifest}"} <= set(objects)
    assert f"source/{shared[0]}" in objects
    for source_id in root["history"]["source_ids"]:
        assert f"manifest/{source_id}" in objects
    assert (await _logical_hour(runtime))["value"] == 0.25
    await runtime.entry._async_process_on_unload(hass)
