"""Native journal durability and immutable v3 source/proof object evidence."""

from __future__ import annotations

import json
from collections import namedtuple
from hashlib import sha256
from pathlib import Path
from threading import get_ident
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.beestat_statistics.hourly_storage import (
    HourlyStorageError,
    HourlyStore,
)

# Capture before hass_storage substitutes its in-memory writer.
_NATIVE_STORE_WRITE = Store._async_write_data


@pytest.fixture
def object_store(hass: HomeAssistant, tmp_path):
    store = HourlyStore(hass, "source-contract")
    with patch.object(store._store, "path", str(tmp_path / "hourly-journal.json")):
        yield store


async def test_source_cpu_job_runs_outside_home_assistant_event_loop(object_store):
    event_loop_thread = get_ident()
    assert await object_store.async_process_source_job(get_ident) != event_loop_thread


async def test_source_staging_does_not_create_or_save_active_journal(object_store):
    content = b'[{"timestamp":"2026-09-01T00:00:00Z"}]\n'
    with patch.object(
        object_store._store, "async_save", new_callable=AsyncMock
    ) as save:
        source_id = await object_store.async_write_object("source", content)
        assert source_id == sha256(content).hexdigest()
        assert await object_store.async_read_object("source", source_id) == content
        assert await object_store.async_load() is None
        save.assert_not_called()
    assert not Path(object_store.path).exists()


async def test_replay_is_immutable_and_corruption_is_not_silently_replaced(
    object_store,
):
    content = b'{"committed":true}'
    digest = await object_store.async_write_object("proof", content)
    assert await object_store.async_write_object("proof", content) == digest
    path = Path(f"{object_store.path}.objects") / "proof" / digest
    path.write_bytes(b"corrupt")
    with pytest.raises(HourlyStorageError, match="read_unverified"):
        await object_store.async_read_object("proof", digest)
    with pytest.raises(HourlyStorageError, match="read_unverified"):
        await object_store.async_write_object("proof", content)
    assert path.read_bytes() == b"corrupt"


async def test_owner_namespace_rejects_traversal_and_other_entry_cannot_read(
    hass, object_store
):
    digest = await object_store.async_write_object("source", b"private fixture")
    for kind, content_digest in (
        ("../source", digest),
        ("source", "../" + digest),
        ("unowned", digest),
    ):
        with pytest.raises(HourlyStorageError, match="identity_invalid"):
            await object_store.async_read_object(kind, content_digest)
    other = HourlyStore(hass, "different-entry")
    with pytest.raises(FileNotFoundError):
        await other.async_read_object("source", digest)


async def test_symlinked_namespace_is_not_followed(object_store, tmp_path):
    external = tmp_path / "other-owner"
    external.mkdir()
    root = Path(f"{object_store.path}.objects")
    root.symlink_to(external, target_is_directory=True)
    with pytest.raises(HourlyStorageError, match="path_invalid"):
        await object_store.async_write_object("source", b"data")
    assert list(external.iterdir()) == []


async def test_disk_reserve_failure_retains_existing_objects(object_store):
    old = await object_store.async_write_object("source", b"retained")
    usage = namedtuple("usage", "total used free")
    with (
        patch(
            "custom_components.beestat_statistics.hourly_storage.shutil.disk_usage",
            return_value=usage(100, 99, 1),
        ),
        pytest.raises(HourlyStorageError, match="disk_reserve"),
    ):
        await object_store.async_write_object("source", b"new")
    assert await object_store.async_read_object("source", old) == b"retained"
    assert not list(Path(f"{object_store.path}.objects").rglob(".pending-*"))


async def test_failed_atomic_install_cannot_create_receipt_or_touch_journal(
    object_store,
):
    with (
        patch(
            "custom_components.beestat_statistics.hourly_storage.os.link",
            side_effect=OSError("fixture installation failure"),
        ),
        pytest.raises(OSError, match="installation failure"),
    ):
        await object_store.async_write_object("manifest", b"manifest")
    root = Path(f"{object_store.path}.objects")
    assert not list(root.rglob(".pending-*"))
    assert not [path for path in root.rglob("*") if path.is_file()]
    assert await object_store.async_load() is None


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
