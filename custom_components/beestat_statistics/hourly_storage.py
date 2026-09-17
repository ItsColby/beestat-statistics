"""Verify the hourly journal on disk after native atomic Store writes.

Native Store may log a failed write and return normally, or satisfy a load from
pending data/cache. Neither establishes durable intent. This boundary reads only
its own journal path through the native uncached JSON reader. The importer owns
serialization and drains surviving save tasks before replacing the writer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.json import load_json

_VERSION = 1
_MINOR_VERSION = 1
_OBJECT_KINDS = frozenset({"source", "manifest", "proof", "operation"})
_MAX_OBJECT_BYTES = 8 * 1024 * 1024
_FREE_RESERVE = 256 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


class HourlyStorageError(ValueError):
    """The owned journal cannot prove the requested durable state."""


class HourlyStore:
    """Versioned native storage with uncached, fail-closed envelope verification."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        if not entry_id or any(character in entry_id for character in ("/", "\\")):
            raise HourlyStorageError("invalid_hourly_store_owner")
        self._hass = hass
        self._key = f"beestat_statistics.{entry_id}.hourly_import"
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _VERSION,
            self._key,
            private=True,
            atomic_writes=True,
            minor_version=_MINOR_VERSION,
            serialize_in_event_loop=False,
        )

    @property
    def path(self) -> str:
        """Return only this config entry's hourly journal path."""

        return str(self._store.path)

    async def async_load(self) -> dict[str, Any] | None:
        """Distinguish actual absence from corrupt or unsupported stored content."""

        return await self._hass.async_add_executor_job(self._read_data)

    async def async_save(self, data: dict[str, Any]) -> None:
        """Return only after the exact native envelope is readable from disk."""

        if self._hass.state in (CoreState.stopping, CoreState.stopped):
            raise HourlyStorageError("hourly_store_stopping")
        if not isinstance(data, dict):
            raise HourlyStorageError("invalid_hourly_store_data")
        detached = deepcopy(data)
        await self._store.async_save(detached)
        await self._hass.async_add_executor_job(self._verify_data, detached)

    async def async_write_object(self, kind: str, content: bytes) -> str:
        """Retain immutable bytes without touching the hourly journal or marker.

        The entry's existing writer serializes staging/adoption. Atomic exclusive
        installation also makes an identical replay safe after cancellation.
        """
        if self._hass.state in (CoreState.stopping, CoreState.stopped):
            raise HourlyStorageError("hourly_store_stopping")
        if not isinstance(content, bytes) or len(content) > _MAX_OBJECT_BYTES:
            raise HourlyStorageError("hourly_object_size_invalid")
        return await self._hass.async_add_executor_job(self.write_object, kind, content)

    async def async_read_object(self, kind: str, digest: str) -> bytes:
        """Read one bounded, hash-verified object from this entry's namespace."""
        return await self._hass.async_add_executor_job(self._read_object, kind, digest)

    async def async_process_source_job[T](
        self, function: Callable[..., T], *args: Any
    ) -> T:
        """Run detached source parsing/hashing on HA's existing bounded executor."""
        return await self._hass.async_add_executor_job(function, *args)

    def _object_path(self, kind: str, digest: str) -> Path:
        if kind not in _OBJECT_KINDS or not isinstance(digest, str):
            raise HourlyStorageError("hourly_object_identity_invalid")
        if _DIGEST.fullmatch(digest) is None:
            raise HourlyStorageError("hourly_object_identity_invalid")
        root = Path(f"{self.path}.objects")
        path = root / kind / digest
        # Never follow an injected link/junction outside this owned namespace.
        for part in (root, root / kind, path):
            if part.is_symlink() or part.is_junction():
                raise HourlyStorageError("hourly_object_path_invalid")
        return path

    def _read_object(self, kind: str, digest: str) -> bytes:
        path = self._object_path(kind, digest)
        with path.open("rb") as source:
            content = source.read(_MAX_OBJECT_BYTES + 1)
        if len(content) > _MAX_OBJECT_BYTES or sha256(content).hexdigest() != digest:
            raise HourlyStorageError("hourly_object_read_unverified")
        return content

    def write_object(self, kind: str, content: bytes) -> str:
        """Retain bytes in an executor, including inside a native upload lease."""
        if not isinstance(content, bytes) or len(content) > _MAX_OBJECT_BYTES:
            raise HourlyStorageError("hourly_object_size_invalid")
        digest = sha256(content).hexdigest()
        path = self._object_path(kind, digest)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._object_path(kind, digest)
        self._sync_directory(path.parent.parent)
        self._sync_directory(path.parent.parent.parent)
        if path.exists():
            if self._read_object(kind, digest) != content:
                raise HourlyStorageError("hourly_object_write_unverified")
            return digest
        if shutil.disk_usage(path.parent).free < len(content) + _FREE_RESERVE:
            raise HourlyStorageError("hourly_object_disk_reserve")
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            try:
                # Hard-link installation is atomic and refuses to replace even
                # an identical destination; readback proves concurrent replay.
                os.link(temporary_path, path)
            except FileExistsError:
                pass
            self._sync_directory(path.parent)
            if self._read_object(kind, digest) != content:
                raise HourlyStorageError("hourly_object_write_unverified")
        finally:
            temporary_path.unlink(missing_ok=True)
        return digest

    @staticmethod
    def _sync_directory(path: Path) -> None:
        # HA runs on POSIX. Windows lacks directory fsync; its atomic link still
        # receives exact uncached readback in dependency-light development.
        if os.name == "posix":
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _read_data(self) -> dict[str, Any] | None:
        # A distinct parsed empty object or JSON null must not masquerade as an
        # absent file. load_json returns this exact default only for FileNotFound.
        missing: dict[str, Any] = {}
        envelope = load_json(self.path, default=missing)
        if envelope is missing:
            return None
        if not isinstance(envelope, dict) or set(envelope) != {
            "version",
            "minor_version",
            "key",
            "data",
        }:
            raise HourlyStorageError("invalid_hourly_store_envelope")
        if (
            type(envelope["version"]) is not int
            or envelope["version"] != _VERSION
            or type(envelope["minor_version"]) is not int
            or envelope["minor_version"] != _MINOR_VERSION
        ):
            raise HourlyStorageError("unsupported_hourly_store_version")
        if envelope["key"] != self._key or not isinstance(envelope["data"], dict):
            raise HourlyStorageError("invalid_hourly_store_envelope")
        # The manager validates the versioned journal's identity, integrity and
        # per-series schema. This adapter owns the native storage envelope only.
        return envelope["data"]

    def _verify_data(self, expected: dict[str, Any]) -> None:
        stored = self._read_data()
        # JSON comparison distinguishes booleans from integers and integers from
        # floats; ordinary Python nested equality does not prove exact content.
        if stored is None or json.dumps(
            stored, sort_keys=True, separators=(",", ":"), allow_nan=False
        ) != json.dumps(
            expected, sort_keys=True, separators=(",", ":"), allow_nan=False
        ):
            raise HourlyStorageError("hourly_store_write_unverified")
