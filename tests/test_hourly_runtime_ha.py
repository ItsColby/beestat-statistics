"""Entry-owned hourly routing and service contracts with offline source fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

from custom_components import beestat_statistics as integration
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredSensor,
)
from custom_components.beestat_statistics.const import DOMAIN
from custom_components.beestat_statistics.coordinator import TemporalContext
from custom_components.beestat_statistics.import_evidence import SkippedWindowEvidence
from custom_components.beestat_statistics.statistics_builder import StatisticsSeries
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 10, 18, 30, tzinfo=UTC)
START = datetime(2026, 9, 10, 17, tzinfo=UTC)
END = START + timedelta(hours=1)
FAN_ID = "beestat:zone_a_fan_runtime_hours_hourly_v2"


def _rows(start=START, **changes):
    return [
        {
            "thermostat_id": 1,
            "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
            "fan": 75,
            **changes,
        }
        for index in range(12)
    ]


def _runtime(hass, freezer, monkeypatch, *, mode="hourly"):
    freezer.move_to(NOW)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=NOW)
    manager = Mock(
        async_mode=AsyncMock(return_value=mode),
        async_reconcile=AsyncMock(),
        async_import=AsyncMock(
            return_value={
                "imported_series": 1,
                "imported_rows": 1,
                "latest_start_by_statistic_id": {FAN_ID: START.isoformat()},
            }
        ),
        async_select=AsyncMock(return_value={"preview_digest": "reviewed-selection"}),
        base_statistic_ids=Mock(return_value=()),
        bootstrap_start=Mock(return_value=None),
        status=Mock(return_value={"mode": mode, "revision": 0, "series": {}}),
        coverage=Mock(return_value={"series": {}, "revision": 0}),
    )
    monkeypatch.setattr(integration, "HourlyImportManager", lambda *_args: manager)
    importer = integration.BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    coordinator.async_refresh_runtime = AsyncMock(return_value=coordinator.data)
    client.async_read_runtime_thermostat = AsyncMock(return_value=_rows())
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, importer=importer)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry, coordinator, client, importer, manager


async def test_legacy_mode_keeps_existing_preparation_and_daily_writer(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, manager = _runtime(
        hass, freezer, monkeypatch, mode="legacy"
    )
    daily = StatisticsSeries(
        {"statistic_id": "beestat:zone_a_fan_runtime_hours"},
        [{"start": START, "state": 2, "sum": 2}],
        1,
    )
    prepared = integration.PreparedImport(
        integration.SummaryImportPlan.full([], fallback_reason="fixture"),
        [],
        SkippedWindowEvidence(),
        {},
        {},
        [daily],
    )
    prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(importer, "_async_prepare_import", prepare)
    write = Mock()
    monkeypatch.setattr(integration, "async_add_external_statistics", write)
    result = await importer.async_import_statistics(
        skip_sync=True, force_full_summary=True
    )
    prepare.assert_awaited_once()
    coordinator.async_refresh_runtime.assert_awaited_once_with(
        skip_sync=True, summary_window=False
    )
    write.assert_called_once_with(hass, daily.metadata, daily.statistics)
    manager.async_import.assert_not_awaited()
    assert result.summary_mode == "full"
    await entry._async_process_on_unload(hass)


async def test_hourly_route_reconciles_before_source_and_writes_under_entry_lock(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    order = []

    async def mode():
        assert importer._lock.locked()
        order.append("mode")
        return "hourly"

    async def reconcile():
        assert importer._lock.locked()
        order.append("reconcile")

    async def refresh(**_kwargs):
        order.append("source")
        return coordinator.data

    async def write(series, identity, *, ordinary_start):
        assert importer._lock.locked()
        assert ordinary_start == END - timedelta(days=1)
        order.append("verified_write")
        fan = next(item for item in series if item.statistic_id == FAN_ID)
        assert fan.hours[-1].start == START
        assert fan.hours[-1].values == {"increment": 0.25}
        assert identity["account_anchors"] == [hashlib.sha256(b"1").hexdigest()]
        assert identity["resources"][FAN_ID] == {
            "thermostat_id": 1,
            "sensor_id": None,
            "quantity": "fan_runtime_hours",
        }
        return {
            "imported_series": 1,
            "imported_rows": 1,
            "latest_start_by_statistic_id": {},
        }

    manager.async_mode.side_effect = mode
    manager.async_reconcile.side_effect = reconcile
    manager.async_import.side_effect = write
    coordinator.async_refresh_runtime.side_effect = refresh
    legacy = AsyncMock(side_effect=AssertionError("Legacy preparation reached"))
    monkeypatch.setattr(importer, "_async_prepare_import", legacy)
    result = await importer.async_import_statistics(skip_sync=True)
    assert order == ["mode", "reconcile", "source", "verified_write"]
    assert result.summary_mode == "hourly"
    assert result.imported_rows == 1
    await entry._async_process_on_unload(hass)
    manager.close.assert_called_once()


@pytest.mark.parametrize("blocked_at", ["async_mode", "async_reconcile"])
async def test_blocked_hourly_admission_does_not_refresh_source(
    hass, freezer, monkeypatch, blocked_at
):
    entry, coordinator, client, importer, manager = _runtime(hass, freezer, monkeypatch)
    getattr(manager, blocked_at).side_effect = RuntimeError("Retained state conflict")
    with pytest.raises(RuntimeError, match="Retained state conflict"):
        await importer.async_import_statistics(skip_sync=True)
    coordinator.async_refresh_runtime.assert_not_awaited()
    client.async_read_runtime_thermostat.assert_not_awaited()
    manager.async_import.assert_not_awaited()
    await entry._async_process_on_unload(hass)


async def test_hourly_acquisition_keeps_tombstones_and_uses_observed_capped_horizon(
    hass, freezer, monkeypatch
):
    entry, coordinator, client, importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    source = _rows()
    source.append({**source[-1], "deleted": True})
    client.async_read_runtime_thermostat.return_value = source
    prepared = await importer._async_prepare_hourly(
        coordinator.data, lookback_days=1, epoch_start=START
    )
    fan = next(item for item in prepared.series if item.statistic_id == FAN_ID)
    assert fan.source_rows == 13
    assert fan.hours[0].reason == "invalid_slots"
    assert fan.hours[0].invalid_slots == 1
    assert fan.hours[0].duplicate_slots == 1
    # Misrouted, malformed and off-grid rows cannot move the observed source horizon.
    horizon = integration._observed_hourly_horizons(
        {
            1: [
                *source,
                {"thermostat_id": 2, "timestamp": END.isoformat()},
                {"thermostat_id": 1, "timestamp": "invalid"},
                {
                    "thermostat_id": 1,
                    "timestamp": (END + timedelta(minutes=1)).isoformat(),
                },
            ]
        },
        {1: START + timedelta(minutes=40)},
    )
    assert horizon == {1: START + timedelta(minutes=40)}
    assert integration._observed_hourly_horizons({1: []}, {1: END}) == {}
    await entry._async_process_on_unload(hass)


async def test_hourly_sensor_acquisition_preserves_deleted_duplicate(
    hass, freezer, monkeypatch
):
    entry, coordinator, client, importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    sensor = ConfiguredSensor(
        10, "room", "Room", 1, "zone_a", True, False, False, False
    )
    data = replace(
        coordinator.data,
        config=BeestatConfig(coordinator.data.config.thermostats, (sensor,)),
        sensor_rows=({"sensor_id": 10, "thermostat_id": 1},),
    )
    raw = [{"sensor_id": 10, "timestamp": START.isoformat(), "temperature": 70}]
    raw.append({**raw[0], "deleted": True})
    client.async_read_runtime_sensor.return_value = raw
    rows = await importer._async_fetch_sensor_rows(
        1,
        data,
        SkippedWindowEvidence(),
        temporal_context=coordinator.capture_temporal_context(),
        window=(START, END),
        preserve_source_rows=True,
    )
    assert rows[10] == raw
    client.async_read_runtime_sensor.assert_awaited_once()
    await entry._async_process_on_unload(hass)


async def test_hourly_window_bounds_preserve_local_dates_and_elapsed_limit():
    context = TemporalContext(
        datetime(2026, 11, 2, 10, 30, tzinfo=UTC), ZoneInfo("America/New_York"), 0
    )
    start, end, measurement_end = integration._hourly_window(
        context,
        lookback_days=366,
        rebuild_start=None,
        rebuild_end=None,
        epoch_start=None,
    )
    assert end - start == timedelta(days=366)
    assert end.minute == 0
    start, end, measurement_end = integration._hourly_window(
        context,
        lookback_days=1,
        rebuild_start=date(2026, 11, 1),
        rebuild_end=date(2026, 11, 1),
        epoch_start=None,
    )
    assert start == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert measurement_end - start == timedelta(hours=25)
    assert end > measurement_end
    with pytest.raises(ValueError, match="whole UTC hours"):
        integration._hourly_window(
            replace(context, local_tz=ZoneInfo("Asia/Kolkata")),
            lookback_days=1,
            rebuild_start=date(2026, 11, 1),
            rebuild_end=None,
            epoch_start=None,
        )


async def test_bootstrap_window_preserves_explicit_bounds_and_366_day_limit():
    context = TemporalContext(NOW, ZoneInfo("UTC"), 0)
    older = END - timedelta(days=2)
    options = {
        "lookback_days": 1,
        "rebuild_start": None,
        "rebuild_end": None,
        "epoch_start": None,
        "bootstrap_start": older,
    }
    assert integration._hourly_window(context, **options) == (older, END, None)
    explicit_epoch = END - timedelta(hours=6)
    assert (
        integration._hourly_window(
            context, **{**options, "epoch_start": explicit_epoch}
        )[0]
        == explicit_epoch
    )
    assert integration._hourly_window(
        context, **{**options, "rebuild_start": date(2026, 9, 10)}
    )[0] == datetime(2026, 9, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="at most 366"):
        integration._hourly_window(
            context, **{**options, "bootstrap_start": END - timedelta(days=367)}
        )


async def test_unmapped_sensor_remains_in_unavailable_preview_denominator(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    sensor = ConfiguredSensor(10, "room", "Room", None, None, True, False, False, False)
    data = replace(
        coordinator.data,
        config=BeestatConfig(coordinator.data.config.thermostats, (sensor,)),
        sensor_rows=({"sensor_id": 10},),
    )
    prepared = await importer._async_prepare_hourly(
        data, lookback_days=1, epoch_start=START
    )
    room = next(
        item
        for item in prepared.series
        if item.statistic_id == "beestat:room_temperature_hourly_v2"
    )
    assert room.blocked_reason == "source_horizon_unavailable"
    assert prepared.identity["resources"][room.statistic_id]["thermostat_id"] is None
    assert any(item.statistic_id == FAN_ID for item in prepared.series)
    await entry._async_process_on_unload(hass)


async def test_identity_requires_current_account_anchor_and_survives_label_changes(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    prepared = await importer._async_prepare_hourly(
        coordinator.data, lookback_days=1, epoch_start=START
    )
    with pytest.raises(ValueError, match="account identity"):
        integration._hourly_identity(
            entry, replace(coordinator.data, thermostat_rows=()), prepared.series
        )
    renamed = replace(
        coordinator.data.config.thermostats[0], slug="new_label", name="New label"
    )
    identities = integration._hourly_resource_identities(BeestatConfig((renamed,), ()))
    assert (
        identities["beestat:new_label_fan_runtime_hours_hourly_v2"]
        == prepared.identity["resources"][FAN_ID]
    )
    retained = integration._hourly_retained_ids(
        BeestatConfig((renamed,), ()),
        ("beestat:old_label_cool_stage_1_runtime_hours_hourly_v2",),
    )
    assert "beestat:new_label_cool_stage_1_runtime_hours_hourly_v2" in retained
    await entry._async_process_on_unload(hass)


async def test_selection_service_preserves_preview_guard_and_pending_retry_path(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, _importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    manager.async_mode.side_effect = AssertionError(
        "Selection must allow interrupted adoption retry"
    )
    assert await integration.async_setup(hass, {})
    request = {
        "config_entry_id": entry.entry_id,
        "epoch_start": START.isoformat(),
        "statistic_ids": [FAN_ID],
        "expected_revision": 0,
    }
    for digest in (None, "reviewed-selection"):
        response = await hass.services.async_call(
            DOMAIN,
            "select_hourly_statistics",
            {**request, **({"preview_digest": digest} if digest else {})},
            blocking=True,
            return_response=True,
        )
        assert response == {"preview_digest": "reviewed-selection"}
        assert manager.async_select.await_args.kwargs == {
            "epoch_start": START,
            "statistic_ids": (FAN_ID,),
            "expected_revision": 0,
            "preview_digest": digest,
        }
    assert coordinator.async_refresh_runtime.await_count == 2
    assert manager.async_reconcile.await_count == 2
    manager.async_import.assert_not_awaited()
    await entry._async_process_on_unload(hass)


async def test_hourly_rebuild_fetches_cumulative_tail_and_bounds_measurements(
    hass, freezer, monkeypatch
):
    entry, coordinator, client, importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    prepared = await importer._async_prepare_hourly(
        coordinator.data,
        lookback_days=1,
        rebuild_start=date(2026, 9, 9),
        rebuild_end=date(2026, 9, 9),
        thermostat_id=1,
    )
    fan = next(item for item in prepared.series if item.statistic_id == FAN_ID)
    measurement = next(item for item in prepared.series if not item.metadata["has_sum"])
    assert fan.hours[-1].start == START
    assert measurement.hours[-1].start == datetime(2026, 9, 10, 3, tzinfo=UTC)
    assert prepared.identity["selected_thermostat_id"] == 1
    client.async_read_runtime_thermostat.assert_awaited_once_with(
        1, "2026-09-09 04:00:00", "2026-09-10 18:00:00"
    )
    await entry._async_process_on_unload(hass)


async def test_hourly_measurement_gap_marks_import_partial_without_fake_skips(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    manager.status.return_value = {
        "pending": False,
        "series": {"measurement": {"coverage_incomplete": True}},
    }
    result = await importer.async_import_statistics(skip_sync=True)
    assert coordinator.last_import_partial is True
    assert result.skipped_windows == 0
    assert result.summary_fallback_reason == "hourly_coverage_incomplete"
    await entry._async_process_on_unload(hass)


async def test_coverage_and_configuration_are_cached_detached_reads(
    hass, freezer, monkeypatch
):
    entry, coordinator, client, _importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    status = {
        "mode": "hourly",
        "revision": 4,
        "series": {FAN_ID: {"epoch_start": START.isoformat()}},
    }
    manager.status.return_value = status
    assert await integration.async_setup(hass, {})
    coverage = await hass.services.async_call(
        DOMAIN,
        "get_hourly_coverage",
        {
            "config_entry_id": entry.entry_id,
            "start": START.isoformat(),
            "end": END.isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    assert coverage == {"series": {}, "revision": 0}
    manager.coverage.assert_called_once_with(start=START, end=END, statistic_ids=None)
    configuration = await hass.services.async_call(
        DOMAIN,
        "get_configuration",
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert configuration["hourly_statistics"] == status
    configuration["hourly_statistics"]["series"].clear()
    assert status["series"]
    coordinator.async_refresh_runtime.assert_not_awaited()
    client.async_read_runtime_thermostat.assert_not_awaited()
    manager.async_mode.assert_not_awaited()
    manager.async_reconcile.assert_not_awaited()
    await entry._async_process_on_unload(hass)


async def test_unload_cancels_active_and_queued_selections(hass, freezer, monkeypatch):
    entry, coordinator, _client, importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    started = asyncio.Event()

    async def blocked_refresh(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    coordinator.async_refresh_runtime.side_effect = blocked_refresh
    request = {"epoch_start": START, "statistic_ids": (FAN_ID,), "expected_revision": 0}
    active = asyncio.create_task(importer.async_select_hourly_statistics(**request))
    await started.wait()
    queued = asyncio.create_task(importer.async_select_hourly_statistics(**request))
    await asyncio.sleep(0)
    await entry._async_process_on_unload(hass)
    results = await asyncio.gather(active, queued, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    manager.close.assert_called_once()
    manager.async_select.assert_not_awaited()
    with pytest.raises(RuntimeError, match="unloaded"):
        await importer.async_select_hourly_statistics(**request)


async def test_coverage_returns_pending_suppression_while_writer_lock_is_held(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, manager = _runtime(
        hass, freezer, monkeypatch
    )
    manager.coverage.return_value = {
        "pending": True,
        "series": {FAN_ID: {"value": None}},
    }
    async with importer._lock:
        coverage = await asyncio.wait_for(
            importer.async_get_hourly_coverage(start=START, end=END), timeout=1
        )
    assert coverage == {"pending": True, "series": {FAN_ID: {"value": None}}}
    coordinator.async_refresh_runtime.assert_not_awaited()
    manager.async_mode.assert_not_awaited()
    manager.async_reconcile.assert_not_awaited()
    await entry._async_process_on_unload(hass)


async def test_coverage_rejects_unknown_entry_without_source_io(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, _importer, _manager = _runtime(
        hass, freezer, monkeypatch
    )
    assert await integration.async_setup(hass, {})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "get_hourly_coverage",
            {
                "config_entry_id": "not-loaded",
                "start": START.isoformat(),
                "end": END.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    coordinator.async_refresh_runtime.assert_not_awaited()
    await entry._async_process_on_unload(hass)
