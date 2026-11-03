"""Status stays in error until the failing operation itself succeeds."""

import asyncio
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.beestat_statistics import coordinator as coordinator_module
from custom_components.beestat_statistics.api import BeestatApiError
from custom_components.beestat_statistics.config_model import BeestatConfig
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)


class _Client:
    def __init__(self) -> None:
        self.fail: Exception | None = None

    def redact_error(self, err: Exception) -> str:
        return str(err)

    async def async_read_id(self, _resource: str) -> list[Any]:
        if self.fail is not None:
            raise self.fail
        return []


def _coordinator() -> Any:
    coordinator: Any = object.__new__(BeestatRuntimeDataCoordinator)
    coordinator._client = _Client()
    coordinator._operation_errors = {}
    coordinator._refresh_error_exc = None
    coordinator.last_error = None
    coordinator.last_error_at = None
    coordinator.data = None
    coordinator.hass = SimpleNamespace()
    coordinator._beestat_config_entry = SimpleNamespace(data={}, options={})
    coordinator.async_update_listeners = lambda: None
    coordinator._summary_window_start = lambda _config, _rows, today: today

    async def reconcile(*_args: Any) -> None:
        return None

    coordinator._async_reconcile_pending_filter_boundaries = reconcile
    coordinator.capture_temporal_context = lambda: SimpleNamespace()
    coordinator._build_runtime_data = MethodType(
        lambda *_args, **_kwargs: SimpleNamespace(), coordinator
    )
    return coordinator


def _fetch(coordinator: Any) -> Any:
    with patch.object(
        coordinator_module,
        "build_beestat_config",
        lambda *_args, **_kwargs: BeestatConfig(thermostats=(), sensors=()),
    ):
        return asyncio.run(
            BeestatRuntimeDataCoordinator._async_fetch_runtime_data_locked(
                coordinator, skip_sync=True, summary_window=False
            )
        )


async def _refresh(coordinator: Any) -> Any:
    """Run the real entry-owned refresh around the real locked fetch."""

    async def fetch(**kwargs: Any) -> Any:
        return await BeestatRuntimeDataCoordinator._async_fetch_runtime_data_locked(
            coordinator, **kwargs
        )

    coordinator._async_fetch_runtime_data = fetch
    coordinator._beestat_config_entry.async_start_reauth_if_available = lambda _hass: (
        None
    )
    coordinator.async_set_update_error = lambda _err: None
    coordinator.async_set_updated_data = lambda _data: None
    with patch.object(
        coordinator_module,
        "build_beestat_config",
        lambda *_args, **_kwargs: BeestatConfig(thermostats=(), sensors=()),
    ):
        return await BeestatRuntimeDataCoordinator._async_refresh_runtime(
            coordinator, skip_sync=True, summary_window=False
        )


def _record_import_success(coordinator: Any) -> None:
    coordinator.async_record_import_result(
        imported_series=1,
        imported_rows=1,
        source_rows=1,
        skipped_windows=0,
        skipped_runtime_thermostat_windows=0,
        skipped_runtime_sensor_windows=0,
        skipped_window_examples=(),
        summary_mode="full",
        summary_window_start=None,
        summary_window_end=None,
        summary_overlap_days=None,
        summary_fallback_reason=None,
        cumulative_seed_count=0,
    )


def test_refresh_success_keeps_import_failure() -> None:
    coordinator = _coordinator()

    coordinator.async_record_import_error(RuntimeError("import boom"))
    _fetch(coordinator)

    assert coordinator.status == "error"
    assert coordinator.last_error == "import boom"
    assert coordinator.last_error_at is not None

    _record_import_success(coordinator)
    coordinator.data = SimpleNamespace()

    assert coordinator.status == "ok"
    assert coordinator.last_error is None
    assert coordinator.last_error_at is None


def test_import_success_keeps_refresh_failure() -> None:
    coordinator = _coordinator()

    coordinator._client.fail = RuntimeError("refresh boom")
    with pytest.raises(RuntimeError, match="refresh boom"):
        _fetch(coordinator)
    _record_import_success(coordinator)

    assert coordinator.status == "error"
    assert coordinator.last_error == "refresh boom"

    coordinator._client.fail = None
    _fetch(coordinator)
    coordinator.data = SimpleNamespace()

    assert coordinator.status == "ok"
    assert coordinator.last_error is None


@pytest.mark.parametrize(
    "failure", [RuntimeError("refresh boom"), BeestatApiError("api boom")]
)
def test_import_that_fails_in_its_own_refresh_leaves_one_error(
    failure: Exception,
) -> None:
    """The refresh failure an import re-raises is recorded once, as a refresh."""

    coordinator = _coordinator()
    coordinator._client.fail = failure

    with pytest.raises(Exception, match=r"boom|failed") as raised:
        asyncio.run(_refresh(coordinator))
    # Importer, scheduler and service boundaries all hand this exception here.
    coordinator.async_record_import_error(raised.value)

    coordinator._client.fail = None
    asyncio.run(_refresh(coordinator))
    coordinator.data = SimpleNamespace()

    assert coordinator.status == "ok"
    assert coordinator.last_error is None
