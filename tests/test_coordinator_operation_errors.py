"""Status stays in error until the failing operation itself succeeds."""

from types import SimpleNamespace
from typing import Any

from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)


def _coordinator() -> Any:
    coordinator: Any = object.__new__(BeestatRuntimeDataCoordinator)
    coordinator._client = SimpleNamespace(redact_error=str)
    coordinator._operation_errors = {}
    coordinator.last_error = None
    coordinator.last_error_at = None
    coordinator.data = object()
    coordinator.async_update_listeners = lambda: None
    return coordinator


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
    coordinator._clear_operation_error("refresh")

    assert coordinator.status == "error"
    assert coordinator.last_error == "import boom"
    assert coordinator.last_error_at is not None

    _record_import_success(coordinator)

    assert coordinator.status == "ok"
    assert coordinator.last_error is None
    assert coordinator.last_error_at is None


def test_import_success_keeps_refresh_failure() -> None:
    coordinator = _coordinator()

    coordinator._async_record_error(RuntimeError("refresh boom"))
    _record_import_success(coordinator)

    assert coordinator.status == "error"
    assert coordinator.last_error == "refresh boom"

    coordinator._clear_operation_error("refresh")

    assert coordinator.status == "ok"
    assert coordinator.last_error is None
