"""Native storage owner evidence for immutable v3 source/proof objects."""

from __future__ import annotations

import sys
from collections import namedtuple
from hashlib import sha256
from pathlib import Path
from threading import get_ident
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.core import HomeAssistant

from custom_components.beestat_statistics.hourly_storage import (
    HourlyStorageError,
    HourlyStore,
)

pytestmark = pytest.mark.asyncio


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
