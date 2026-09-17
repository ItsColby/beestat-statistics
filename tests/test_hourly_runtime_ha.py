"""Entry-owned hourly routing and service contracts with offline source fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components import beestat_statistics as integration
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredSensor,
)
from custom_components.beestat_statistics.const import DOMAIN
from custom_components.beestat_statistics.coordinator import TemporalContext
from custom_components.beestat_statistics.hourly_import import (
    HourlyImportError,
    HourlyImportManager,
    HourlyReconciliationError,
    WriterPartition,
)
from custom_components.beestat_statistics.hourly_import_plan import HourlyStatisticRow
from custom_components.beestat_statistics.hourly_recorder import (
    HourlyRecorder,
    HourlyRecorderError,
)
from custom_components.beestat_statistics.hourly_storage import HourlyStore
from custom_components.beestat_statistics.import_evidence import SkippedWindowEvidence
from custom_components.beestat_statistics.statistics_builder import (
    CumulativeStatisticSeed,
    StatisticsSeries,
)
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 10, 18, 30, tzinfo=UTC)
START = datetime(2026, 9, 10, 17, tzinfo=UTC)
END = START + timedelta(hours=1)
FAN_ID = "beestat:zone_a_fan_runtime_hours_hourly_v2"
LEGACY_FAN_ID = FAN_ID.removesuffix("_hourly_v2")
LEGACY_COOL_ID = "beestat:zone_a_cool_runtime_hours"
LEGACY_VOC_ID = "beestat:room_voc_concentration"
_NATIVE_STORE_WRITE = Store._async_write_data


@pytest.fixture
async def native_hourly_hass(recorder_mock, freezer):
    """Let Recorder configure its disposable database before HA is constructed."""

    hass = recorder_mock.hass
    freezer.move_to(NOW)
    await hass.async_start()
    return hass


def _partition(identity, *, selected=(), blocked=None):
    resources = frozenset(identity["resources"])
    hourly = frozenset(selected)
    return WriterPartition(
        frozenset(key.removesuffix("_hourly_v2") for key in resources - hourly),
        hourly,
        frozenset(key.removesuffix("_hourly_v2") for key in hourly),
        bool(hourly),
        blocked,
    )


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
        has_pending_history=False,
        history_quantity_ids=Mock(return_value=()),
        history_status=Mock(
            return_value={
                "status": "unselected",
                "root_revision": 0,
                "has_pending": False,
            }
        ),
        history_configuration=Mock(
            return_value={"contract_version": 3, "quantities": []}
        ),
        async_mode=AsyncMock(return_value=mode),
        async_writer_partition=AsyncMock(
            side_effect=lambda identity: _partition(
                identity, selected=identity["resources"] if mode == "hourly" else ()
            )
        ),
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
        source_starts=Mock(return_value={}),
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
    entry.runtime_data = SimpleNamespace(
        client=client, coordinator=coordinator, importer=importer
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry, coordinator, client, importer, manager


def _mixed_runtime(hass, freezer, monkeypatch):
    entry, coordinator, client, importer, manager = _runtime(hass, freezer, monkeypatch)
    sensor = ConfiguredSensor(
        10, "room", "Room", 1, "zone_a", False, False, False, True
    )
    coordinator.data = replace(
        coordinator.data,
        config=BeestatConfig(coordinator.data.config.thermostats, (sensor,)),
        sensor_rows=({"id": 10, "thermostat_id": 1},),
        summary_rows=(
            {
                "thermostat_id": 1,
                "date": "2026-09-10",
                "sum_fan": 3600,
                "sum_compressor_cool_1": 7200,
            },
        ),
        summary_rows_full=True,
    )
    client.async_read_runtime_sensor.return_value = [
        {"sensor_id": 10, "timestamp": START.isoformat(), "voc_concentration": 123}
    ]
    coordinator.async_refresh_runtime.side_effect = lambda **_kwargs: coordinator.data
    manager.async_writer_partition.side_effect = lambda identity: _partition(
        identity, selected=(FAN_ID,)
    )
    return entry, coordinator, client, importer, manager


async def test_partial_selection_preserves_native_legacy_fan_and_continues_cool_voc(
    native_hourly_hass, freezer, monkeypatch, tmp_path
):
    """One real journal/Recorder owner partitions actual builder output by quantity."""

    hass = native_hourly_hass
    entry, coordinator, _client, importer, _mock_manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    store = HourlyStore(hass, entry.entry_id)
    native = store._store
    adapter = HourlyRecorder(hass)
    manager = HourlyImportManager(hass, entry, store=store, recorder=adapter)
    importer.hourly = manager
    with (
        patch.object(native, "path", str(tmp_path / "partition-journal.json")),
        patch.object(native, "_async_write_data", _NATIVE_STORE_WRITE.__get__(native)),
    ):
        prepared = await importer._async_prepare_import(
            coordinator.data,
            lookback_days=1,
            force_full_summary=True,
            rebuild_start=None,
            rebuild_end=None,
            thermostat_id=None,
            temporal_context=coordinator.capture_temporal_context(),
        )
        daily_fan = next(
            item for item in prepared.series if item.statistic_id == LEGACY_FAN_ID
        )
        daily_start = daily_fan.statistics[0]["start"]
        adapter.submit(
            daily_fan.metadata,
            (HourlyStatisticRow(daily_start, state=7.0, sum=7.0),),
        )
        before = await adapter.async_snapshot(LEGACY_FAN_ID, daily_start)
        selection = {
            "epoch_start": START,
            "statistic_ids": (FAN_ID,),
            "expected_revision": 0,
        }
        preview = await importer.async_select_hourly_statistics(**selection)
        await importer.async_select_hourly_statistics(
            **selection, preview_digest=preview["preview_digest"]
        )
        result = await importer.async_import_statistics(
            skip_sync=True, force_full_summary=True
        )

        assert await adapter.async_snapshot(LEGACY_FAN_ID, daily_start) == before
        hourly = await adapter.async_snapshot(FAN_ID, START)
        cooling = await adapter.async_snapshot(LEGACY_COOL_ID, daily_start)
        voc = await adapter.async_snapshot(LEGACY_VOC_ID, daily_start)
        assert hourly.rows == (HourlyStatisticRow(START, state=0.25, sum=0.25),)
        assert cooling.rows == (HourlyStatisticRow(daily_start, state=2.0, sum=2.0),)
        assert voc.rows == (
            HourlyStatisticRow(daily_start, mean=123.0, min=123.0, max=123.0),
        )
        original_voc = next(
            item for item in prepared.series if item.statistic_id == LEGACY_VOC_ID
        )
        assert (
            voc.metadata["unit_of_measurement"]
            == original_voc.metadata["unit_of_measurement"]
        )
        assert voc.metadata["unit_class"] == original_voc.metadata["unit_class"]
        assert result.summary_mode == "mixed"
        assert result.hourly_imported_rows == 1
        assert result.legacy_imported_rows >= 2
        assert LEGACY_FAN_ID not in result.latest_start_by_statistic_id
        assert LEGACY_VOC_ID in result.latest_start_by_statistic_id
        assert await store.async_load() is not None
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    ("blocked_at", "reason"),
    [
        ("selection_pending", "selection_pending"),
        ("async_reconcile", "hourly_reconciliation_unverified"),
        ("async_import", "hourly_effect_unverified"),
    ],
)
async def test_known_hourly_failure_continues_only_unselected_legacy_quantities(
    hass, freezer, monkeypatch, blocked_at, reason
):
    entry, coordinator, _client, importer, manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    if blocked_at == "selection_pending":
        manager.async_writer_partition.side_effect = lambda identity: _partition(
            identity, selected=(FAN_ID,), blocked="selection_pending"
        )
    elif blocked_at == "async_reconcile":
        manager.async_reconcile.side_effect = HourlyReconciliationError(
            "Unverified prior effect"
        )
    else:
        manager.async_import.side_effect = HourlyRecorderError("Unverified effect")
    monkeypatch.setattr(
        importer,
        "_async_existing_detailed_statistic_ids",
        AsyncMock(return_value=frozenset()),
    )
    write = Mock()
    monkeypatch.setattr(integration, "async_add_external_statistics", write)
    result = await importer.async_import_statistics(
        skip_sync=True, force_full_summary=True
    )
    written = {
        call.args[1]["statistic_id"]: call.args[2] for call in write.call_args_list
    }
    assert LEGACY_FAN_ID not in written
    assert written[LEGACY_COOL_ID][0]["sum"] == 2.0
    assert written[LEGACY_VOC_ID][0]["mean"] == 123.0
    assert result.hourly_blocked_reason == reason
    assert result.hourly_imported_rows is None
    assert result.legacy_imported_rows == sum(len(rows) for rows in written.values())
    assert coordinator.last_import_partial is True
    assert coordinator.last_import_writers["hourly_blocked_reason"] == reason
    if blocked_at != "async_import":
        manager.async_import.assert_not_awaited()
    await entry._async_process_on_unload(hass)


async def test_unverifiable_repartition_after_hourly_failure_blocks_all_legacy_writes(
    hass, freezer, monkeypatch
):
    entry, coordinator, _client, importer, manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    partition = _partition(
        integration._writer_identity(entry, coordinator.data), selected=(FAN_ID,)
    )
    manager.async_writer_partition.side_effect = [
        partition,
        partition,
        HourlyImportError("Journal corrupt after failure"),
    ]
    manager.async_import.side_effect = HourlyRecorderError("Unverified effect")
    prepare = AsyncMock(
        side_effect=AssertionError("Legacy source work must remain blocked")
    )
    monkeypatch.setattr(importer, "_async_prepare_import", prepare)
    write = Mock()
    monkeypatch.setattr(integration, "async_add_external_statistics", write)
    with pytest.raises(HourlyImportError, match="Journal corrupt after failure"):
        await importer.async_import_statistics(skip_sync=True)
    prepare.assert_not_awaited()
    write.assert_not_called()
    assert coordinator.last_import_success_at is None
    await entry._async_process_on_unload(hass)


async def test_hourly_settings_change_reprepares_and_passes_final_eligible_scope(
    hass, freezer, monkeypatch
):
    """A selected sensor disabled during acquisition is never written from the old view."""

    entry, coordinator, client, importer, manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    temperature_id = "beestat:room_temperature_hourly_v2"
    legacy_temperature_id = temperature_id.removesuffix("_hourly_v2")
    coordinator.data = replace(
        coordinator.data,
        config=replace(
            coordinator.data.config,
            sensors=(
                replace(coordinator.data.config.sensors[0], include_temperature=True),
            ),
        ),
    )

    def partition(identity):
        current = frozenset(identity["resources"])
        selected = current & {temperature_id}
        return WriterPartition(
            frozenset(key.removesuffix("_hourly_v2") for key in current - selected),
            selected,
            frozenset({legacy_temperature_id}),
            True,
        )

    manager.async_writer_partition.side_effect = partition
    source_reads = 0

    async def sensor_rows(*_args):
        nonlocal source_reads
        source_reads += 1
        if source_reads == 1:
            coordinator.async_set_updated_data(
                replace(
                    coordinator.data,
                    config=replace(
                        coordinator.data.config,
                        sensors=(
                            replace(
                                coordinator.data.config.sensors[0],
                                include_temperature=False,
                            ),
                        ),
                    ),
                )
            )
        return [
            {
                "sensor_id": 10,
                "timestamp": row["timestamp"],
                "temperature": 700,
                "voc_concentration": 123,
            }
            for row in _rows()
        ]

    client.async_read_runtime_sensor.side_effect = sensor_rows
    manager.async_import.return_value = {
        "imported_series": 0,
        "imported_rows": 0,
        "latest_start_by_statistic_id": {},
    }
    monkeypatch.setattr(
        importer,
        "_async_existing_detailed_statistic_ids",
        AsyncMock(return_value=frozenset()),
    )
    write = Mock()
    monkeypatch.setattr(integration, "async_add_external_statistics", write)
    result = await importer.async_import_statistics(
        skip_sync=True, force_full_summary=True
    )
    assert source_reads == 3  # stale hourly read, reprepared hourly read, legacy read
    manager.async_import.assert_awaited_once()
    submission = manager.async_import.await_args
    assert temperature_id not in submission.kwargs["eligible_resources"]
    assert not any(item.statistic_id == temperature_id for item in submission.args[0])
    written = {call.args[1]["statistic_id"] for call in write.call_args_list}
    assert LEGACY_VOC_ID in written
    assert legacy_temperature_id not in written
    assert result.hourly_imported_rows == 0
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("change", ["runtime_config", "timezone"])
async def test_legacy_reprepares_changed_runtime_or_timezone_before_any_write(
    hass, freezer, monkeypatch, change
):
    entry, coordinator, _client, importer, manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    manager.async_writer_partition.side_effect = lambda identity: _partition(
        identity, selected=(FAN_ID,), blocked="selection_pending"
    )
    monkeypatch.setattr(
        importer,
        "_async_existing_detailed_statistic_ids",
        AsyncMock(return_value=frozenset()),
    )
    original_prepare = importer._async_prepare_import
    prepared_snapshots = []

    async def prepare(data, **kwargs):
        prepared = await original_prepare(data, **kwargs)
        prepared_snapshots.append(prepared)
        if len(prepared_snapshots) == 1:
            if change == "runtime_config":
                coordinator.async_set_updated_data(
                    replace(
                        coordinator.data,
                        config=replace(
                            coordinator.data.config,
                            sensors=(
                                replace(
                                    coordinator.data.config.sensors[0],
                                    include_voc=False,
                                ),
                            ),
                        ),
                    )
                )
            else:
                coordinator.async_update_local_timezone(ZoneInfo("UTC"))
        return prepared

    monkeypatch.setattr(importer, "_async_prepare_import", prepare)
    writes = []

    def write(_hass, metadata, rows):
        assert importer._lock.locked()
        assert len(prepared_snapshots) == 2
        writes.append((metadata["statistic_id"], list(rows)))

    monkeypatch.setattr(integration, "async_add_external_statistics", write)
    await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
    assert len(prepared_snapshots) == 2
    assert {key for key, _rows_written in writes} == {
        item.statistic_id for item in prepared_snapshots[-1].series
    }
    assert LEGACY_FAN_ID not in {key for key, _rows_written in writes}
    if change == "runtime_config":
        assert any(
            item.statistic_id == LEGACY_VOC_ID for item in prepared_snapshots[0].series
        )
        assert LEGACY_VOC_ID not in {key for key, _rows_written in writes}
    else:
        first = next(
            item
            for item in prepared_snapshots[0].series
            if item.statistic_id == LEGACY_COOL_ID
        )
        final = next(rows for key, rows in writes if key == LEGACY_COOL_ID)
        assert first.statistics[0]["start"].astimezone(UTC).hour == 4
        assert final[0]["start"].astimezone(UTC).hour == 0
    await entry._async_process_on_unload(hass)


async def test_legacy_planner_excludes_frozen_inventory_latest_seeds_and_new_stages(
    native_hourly_hass, freezer, monkeypatch
):
    """A selected legacy counter cannot pin or invalidate another counter's window."""

    hass = native_hourly_hass
    entry, coordinator, client, importer, _manager = _mixed_runtime(
        hass, freezer, monkeypatch
    )
    cool_stage_1 = "beestat:zone_a_cool_stage_1_runtime_hours"
    allowed = frozenset({LEGACY_COOL_ID, cool_stage_1})
    with patch.object(
        integration, "get_metadata", wraps=integration.get_metadata
    ) as metadata:
        existing = await importer._async_existing_detailed_statistic_ids(
            coordinator.data, allowed_legacy_ids=allowed
        )
    assert existing == frozenset()
    assert metadata.call_args.kwargs["statistic_ids"] == {cool_stage_1}

    async def latest(statistic_ids):
        assert set(statistic_ids) == allowed
        return dict.fromkeys(statistic_ids, START)

    async def seeds(statistic_ids, *, seed_day, window_start, local_tz):
        assert set(statistic_ids) == allowed
        assert seed_day == window_start - timedelta(days=1)
        return {
            key: CumulativeStatisticSeed(
                datetime.combine(seed_day, datetime.min.time(), local_tz), 10.0, 10.0
            )
            for key in statistic_ids
        }

    monkeypatch.setattr(
        importer, "_async_latest_cumulative_starts", AsyncMock(side_effect=latest)
    )
    monkeypatch.setattr(
        importer, "_async_cumulative_seeds", AsyncMock(side_effect=seeds)
    )
    full = AsyncMock(side_effect=AssertionError("Frozen IDs caused a full baseline"))
    monkeypatch.setattr(importer, "_async_full_summary_rows", full)
    client.async_read_runtime_thermostat_summary = AsyncMock(
        return_value=[
            {**coordinator.data.summary_rows[0], "sum_compressor_cool_2": 3600}
        ]
    )
    plan = await importer._async_summary_import_plan(
        coordinator.data,
        force_full_summary=False,
        temporal_context=coordinator.capture_temporal_context(),
        existing_statistic_ids=existing,
        allowed_legacy_ids=allowed,
    )
    assert plan.mode == "windowed"
    assert plan.window_start == date(2026, 9, 3)
    assert set(plan.seeds) == allowed
    assert plan.rows[0]["sum_compressor_cool_2"] == 3600
    full.assert_not_awaited()
    await entry._async_process_on_unload(hass)


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

    async def partition(identity):
        assert importer._lock.locked()
        order.append("partition")
        return _partition(identity, selected=identity["resources"])

    async def reconcile():
        assert importer._lock.locked()
        order.append("reconcile")

    async def refresh(**_kwargs):
        order.append("source")
        return coordinator.data

    async def write(series, identity, *, ordinary_start, eligible_resources):
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
        assert eligible_resources[FAN_ID] == identity["resources"][FAN_ID]
        return {
            "imported_series": 1,
            "imported_rows": 1,
            "latest_start_by_statistic_id": {},
        }

    manager.async_writer_partition.side_effect = partition
    manager.async_reconcile.side_effect = reconcile
    manager.async_import.side_effect = write
    coordinator.async_refresh_runtime.side_effect = refresh
    legacy = AsyncMock(side_effect=AssertionError("Legacy preparation reached"))
    monkeypatch.setattr(importer, "_async_prepare_import", legacy)
    result = await importer.async_import_statistics(skip_sync=True)
    assert order == [
        "partition",
        "reconcile",
        "source",
        "partition",
        "verified_write",
        "partition",
    ]
    assert result.summary_mode == "hourly"
    assert result.imported_rows == 1
    await entry._async_process_on_unload(hass)
    manager.close.assert_called_once()


@pytest.mark.parametrize("blocked_at", ["async_writer_partition", "async_reconcile"])
async def test_blocked_hourly_admission_does_not_refresh_source(
    hass, freezer, monkeypatch, blocked_at
):
    entry, coordinator, client, importer, manager = _runtime(hass, freezer, monkeypatch)
    error = (
        HourlyImportError("Retained state conflict")
        if blocked_at == "async_writer_partition"
        else RuntimeError("Retained state conflict")
    )
    getattr(manager, blocked_at).side_effect = error
    with pytest.raises(type(error), match="Retained state conflict"):
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
    coordinator.data = data
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
    client.async_read_runtime_thermostat.return_value = [
        *_rows(),
        {"thermostat_id": 1, "timestamp": (START + timedelta(minutes=1)).isoformat()},
    ]
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
    assert fan.rejected_timestamps == 1
    assert measurement.rejected_timestamps == 0
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
    assert {
        key: value
        for key, value in configuration["hourly_statistics"].items()
        if key != "history_v3"
    } == status
    assert configuration["hourly_statistics"]["history_v3"] == (
        manager.history_configuration.return_value
    )
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
