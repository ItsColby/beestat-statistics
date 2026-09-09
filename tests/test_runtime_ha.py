"""Exact-Core lifecycle tests for cached Beestat temporal projections."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.beestat_statistics import (
    BeestatStatisticsImporter,
    PreparedImport,
    SummaryImportPlan,
    _async_migrate_homekit_device_assignments,
    _async_track_runtime_entity_states,
    _async_track_source_device_relinks,
    _async_track_time_zone_updates,
    _dedupe_rows,
    _filter_changed_entity_ids,
    _parse_beestat_time,
    _row_float,
    _row_start_datetime,
    _sensor_thermostat_map,
    _thermostat_data_end_map,
)
from custom_components.beestat_statistics.api import BeestatApiError
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredSensor,
)
from custom_components.beestat_statistics.const import API_BASE, CONF_API_BASE, DOMAIN
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)
from custom_components.beestat_statistics.import_evidence import SkippedWindowEvidence
from custom_components.beestat_statistics.statistics_builder import StatisticsSeries

pytestmark = pytest.mark.asyncio


async def test_recorder_seed_numbers_reject_nonfinite_values() -> None:
    """Malformed Recorder seeds must not poison cumulative imports."""

    assert _row_float(42.5) == 42.5
    assert _row_float("NaN") is None
    assert _row_float("Infinity") is None


async def test_recorder_seed_starts_reject_nonfinite_and_unrepresentable_values() -> (
    None
):
    """Malformed Recorder starts must not enter cumulative seed selection."""

    assert _row_start_datetime({"start": 0}) == datetime(1970, 1, 1, tzinfo=UTC)
    assert _row_start_datetime({"start": "NaN"}) is None
    assert _row_start_datetime({"start": "Infinity"}) is None
    assert _row_start_datetime({"start": 1e300}) is None


async def test_point_rows_collapse_duplicate_identities_before_aggregation() -> None:
    """Last source rows own point identities and winning deletions are omitted."""

    assert _dedupe_rows(
        [
            {"runtime_sensor_id": 1, "timestamp": "2026-07-01T00:00:00Z", "v": 1},
            {"runtime_sensor_id": 1, "timestamp": "2026-07-01T00:00:00Z", "v": 2},
            {"sensor_id": 10, "timestamp": "2026-07-01T00:05:00Z", "v": 3},
            {"sensor_id": 10, "timestamp": "2026-07-01T00:05:00Z", "v": 4},
            {"runtime_sensor_id": 2, "timestamp": "2026-07-01T00:10:00Z", "v": 5},
            {
                "runtime_sensor_id": 2,
                "timestamp": "2026-07-01T00:10:00Z",
                "deleted": True,
            },
        ],
        id_field="sensor_id",
    ) == [
        {"runtime_sensor_id": 1, "timestamp": "2026-07-01T00:00:00Z", "v": 2},
        {"sensor_id": 10, "timestamp": "2026-07-01T00:05:00Z", "v": 4},
    ]


async def test_point_identity_normalizes_ids_and_instants_with_malformed_fallbacks() -> (
    None
):
    """Different encodings of one point retain only its final source row."""

    latest_id_row = {"runtime_sensor_id": "1", "timestamp": "2026-07-01 12:00:00"}
    latest_time_row = {
        "runtime_sensor_id": ["invalid"],
        "sensor_id": "10",
        "timestamp": "2026-07-01T08:05:00-04:00",
    }
    assert _dedupe_rows(
        [
            {"runtime_sensor_id": 1, "timestamp": "2026-07-01 12:00:00"},
            latest_id_row,
            {"sensor_id": 10, "timestamp": "2026-07-01 12:05:00"},
            latest_time_row,
        ],
        id_field="sensor_id",
    ) == [latest_id_row, latest_time_row]


@pytest.mark.parametrize("runtime_id", [1.5, True, 0, -1, float("inf"), "1.5", [], {}])
async def test_malformed_point_ids_cannot_shadow_a_valid_source_row(
    runtime_id: Any,
) -> None:
    """Invalid IDs use the independent resource/time identity without coercion."""

    valid_row = {"runtime_sensor_id": 1, "timestamp": "2026-07-01 12:00:00"}
    fallback_row = {
        "runtime_sensor_id": runtime_id,
        "sensor_id": 10,
        "timestamp": "2026-07-01 12:05:00",
    }
    assert _dedupe_rows([valid_row, fallback_row], id_field="sensor_id") == [
        valid_row,
        fallback_row,
    ]
    deleted_fallback = {
        "sensor_id": 10,
        "timestamp": "2026-07-01 12:05:00",
        "deleted": True,
    }
    assert _dedupe_rows(
        [valid_row, fallback_row, deleted_fallback], id_field="sensor_id"
    ) == [valid_row]


@pytest.mark.parametrize(
    "value",
    ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"],
)
async def test_source_data_end_rejects_unrepresentable_utc_instants(value: str) -> None:
    assert _parse_beestat_time(value) is None


async def test_point_acquisition_maps_prefer_the_normalized_resource_identity() -> None:
    """Window caps use the same resource IDs as coordinator and configuration."""

    assert _sensor_thermostat_map(
        [
            {"id": 1000, "sensor_id": "10", "thermostat_id": 20},
            {"id": 11, "thermostat_id": 21},
        ]
    ) == {10: 20, 11: 21}
    data_end = "2026-07-01 12:00:00"
    assert _thermostat_data_end_map(
        [
            {"id": 2000, "thermostat_id": "20", "data_end": data_end},
            {"id": 21, "data_end": data_end},
        ]
    ) == {
        20: datetime(2026, 7, 1, 12, tzinfo=UTC),
        21: datetime(2026, 7, 1, 12, tzinfo=UTC),
    }


@pytest.mark.parametrize("read_fails", [False, True])
async def test_sensor_runtime_is_read_once_for_all_enabled_statistics(
    hass: HomeAssistant,
    freezer: Any,
    read_fails: bool,
) -> None:
    """All sensor metrics share one acquisition and one skipped-window count."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    sensor = ConfiguredSensor(
        sensor_id=10,
        slug="room",
        name="Room",
        thermostat_id=1,
        thermostat_slug="zone_a",
        include_temperature=True,
        include_air_quality=True,
        include_co2=True,
        include_voc=True,
        occupancy_entity_id="binary_sensor.room_occupancy",
    )
    data = replace(
        coordinator.data,
        config=BeestatConfig(
            thermostats=coordinator.data.config.thermostats,
            sensors=(sensor,),
        ),
    )
    row = {"runtime_sensor_id": 1, "sensor_id": 10, "timestamp": "2026-07-01 12:00:00"}
    client.async_read_runtime_sensor = AsyncMock(
        return_value=[row],
        side_effect=BeestatApiError("read failed") if read_fails else None,
    )
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    skipped_windows = SkippedWindowEvidence()

    rows = await importer._async_fetch_sensor_rows(
        1,
        data,
        skipped_windows,
        start_day=date(2026, 7, 1),
        thermostat_id=1,
        temporal_context=coordinator.capture_temporal_context(),
    )

    client.async_read_runtime_sensor.assert_awaited_once_with(
        10, "2026-07-01 04:00:00", "2026-07-01 16:00:00"
    )
    assert rows == {10: [] if read_fails else [row]}
    assert skipped_windows.runtime_sensor_count == int(read_fails)
    await entry._async_process_on_unload(hass)


async def test_unload_cancels_active_and_queued_service_imports_before_recorder_write(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """Service callers cannot finish old-account imports after entry unload."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    preparing = asyncio.Event()
    refresh = AsyncMock(return_value=coordinator.data)

    async def prepare(*_args, **_kwargs):
        preparing.set()
        await asyncio.Event().wait()
        raise AssertionError("Cancelled preparation must not resume")

    with (
        patch.object(coordinator, "async_refresh_runtime", new=refresh),
        patch.object(importer, "_async_prepare_import", new=prepare),
        patch(
            "custom_components.beestat_statistics.async_add_external_statistics"
        ) as write,
    ):
        active = asyncio.create_task(importer.async_import_statistics(skip_sync=True))
        await preparing.wait()
        queued = asyncio.create_task(importer.async_import_statistics(skip_sync=True))
        await asyncio.sleep(0)

        await entry._async_process_on_unload(hass)
        outcomes = await asyncio.gather(active, queued, return_exceptions=True)

        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        refresh.assert_awaited_once()
        write.assert_not_called()
        assert coordinator.last_import_success_at is None
        with pytest.raises(RuntimeError, match="config entry is unloaded"):
            await importer.async_import_statistics(skip_sync=True)


async def test_filter_helper_listener_follows_sources_and_stops_on_unload(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """A helper discovered or replaced after setup starts driving imports."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    calls: list[str] = []
    _async_track_runtime_entity_states(
        hass,
        entry,
        _filter_changed_entity_ids,
        lambda event: calls.append(event.data["entity_id"]),
    )

    def select_helper(entity_id: str | None) -> None:
        thermostat = replace(
            coordinator.data.config.thermostats[0],
            filter_changed_entity_id=entity_id,
        )
        coordinator.async_set_updated_data(
            replace(
                coordinator.data,
                config=replace(coordinator.data.config, thermostats=(thermostat,)),
            )
        )

    select_helper("input_datetime.first_filter")
    hass.states.async_set("input_datetime.first_filter", "2026-07-01")
    await hass.async_block_till_done()
    assert calls == ["input_datetime.first_filter"]

    select_helper("input_datetime.second_filter")
    hass.states.async_set("input_datetime.first_filter", "2026-07-02")
    hass.states.async_set("input_datetime.second_filter", "2026-07-02")
    await hass.async_block_till_done()
    assert calls == ["input_datetime.first_filter", "input_datetime.second_filter"]

    await entry._async_process_on_unload(hass)
    hass.states.async_set("input_datetime.second_filter", "2026-07-03")
    await hass.async_block_till_done()
    assert calls == ["input_datetime.first_filter", "input_datetime.second_filter"]
    assert client.calls == []


async def test_device_reconciliation_includes_all_owned_resource_entity_suffixes(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """New and disabled enrichment entities follow the same resource identity."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    source_device = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("homekit_controller", "source-device")},
    )
    fallback = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "thermostat_1")},
    )
    entity_registry = er.async_get(hass)
    entities = [
        entity_registry.async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            config_entry=entry,
            device_id=fallback.id,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        for domain, unique_id in (
            ("sensor", "thermostat_1_current_profile_room_temperature_spread"),
            ("sensor", "thermostat_1_compressor_minimum_off_time"),
            ("binary_sensor", "thermostat_1_follow_me_enabled"),
            ("binary_sensor", "thermostat_1_microphone_enabled"),
        )
    ]
    unrelated = entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "thermostat_1_unrelated",
        config_entry=source_entry,
    )
    other_resource = entity_registry.async_get_or_create(
        "sensor", DOMAIN, "thermostat_11_other", config_entry=entry
    )
    data = replace(
        coordinator.data,
        config=replace(
            coordinator.data.config,
            thermostats=(
                replace(
                    coordinator.data.config.thermostats[0], device_id=source_device.id
                ),
            ),
        ),
    )

    _async_migrate_homekit_device_assignments(hass, entry, data)

    for entity in entities:
        current = entity_registry.async_get(entity.entity_id)
        assert current.device_id == source_device.id
        assert current.disabled_by is er.RegistryEntryDisabler.USER
    assert device_registry.async_get(fallback.id) is None
    assert entity_registry.async_get(unrelated.entity_id).device_id is None
    assert entity_registry.async_get(other_resource.entity_id).device_id is None
    await entry._async_process_on_unload(hass)


async def test_repeated_unmapped_updates_preserve_only_the_own_resource_fallback(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """An unmapped resource keeps its native fallback across coordinator updates."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    coordinator.data = replace(
        coordinator.data,
        config=replace(
            coordinator.data.config,
            sensors=(
                ConfiguredSensor(
                    sensor_id=10,
                    name="Room",
                    slug="room",
                    thermostat_id=1,
                    thermostat_slug="zone_a",
                    include_temperature=True,
                    include_air_quality=False,
                    include_co2=False,
                    include_voc=False,
                ),
            ),
        ),
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    devices = dr.async_get(hass)
    thermostat_fallback = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "thermostat_1")},
    )
    sensor_fallback = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "sensor_10")},
    )
    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    foreign_device = devices.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("homekit_controller", "former-source")},
    )
    registry = er.async_get(hass)
    thermostat_entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "thermostat_1_filter_due_date",
        config_entry=entry,
        device_id=thermostat_fallback.id,
    )
    sensor_entity = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "sensor_10_sensor_in_use",
        config_entry=entry,
        device_id=sensor_fallback.id,
    )
    mismatched_entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "thermostat_1_compressor_minimum_off_time",
        config_entry=entry,
        device_id=sensor_fallback.id,
    )
    stale_foreign_entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "thermostat_1_current_profile_room_temperature_spread",
        config_entry=entry,
        device_id=foreign_device.id,
    )
    _async_track_source_device_relinks(hass, entry)

    for _pass in range(2):
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()

        assert (
            registry.async_get(thermostat_entity.entity_id).device_id
            == thermostat_fallback.id
        )
        assert (
            registry.async_get(sensor_entity.entity_id).device_id == sensor_fallback.id
        )
        assert registry.async_get(mismatched_entity.entity_id).device_id is None
        assert registry.async_get(stale_foreign_entity.entity_id).device_id is None
        assert devices.async_get(thermostat_fallback.id) is not None
        assert devices.async_get(sensor_fallback.id) is not None
    assert client.calls == []
    await entry._async_process_on_unload(hass)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator_data(
    hass: HomeAssistant,
    *,
    evaluated_at: datetime,
    data_end: datetime | None = None,
    latest_date: date | None = None,
    schedule: list[list[str]] | None = None,
) -> tuple[MockConfigEntry, BeestatRuntimeDataCoordinator, Any]:
    entry = _entry(hass)
    client = types.SimpleNamespace(calls=[])
    coordinator = BeestatRuntimeDataCoordinator(
        hass,
        entry,
        client,
        local_tz=ZoneInfo("America/New_York"),
    )
    thermostat_row: dict[str, Any] = {"id": 1, "name": "Zone A"}
    if data_end is not None:
        thermostat_row["data_end"] = data_end.isoformat()
    if schedule is not None:
        thermostat_row["timezone"] = "America/New_York"
        thermostat_row["program"] = {
            "currentClimateRef": "hold",
            "climates": [
                {"climateRef": "hold", "name": "Hold"},
                {"climateRef": "sleep", "name": "Sleep"},
                {"climateRef": "home", "name": "Home"},
            ],
            "schedule": schedule,
        }
    summary_rows = (
        [
            {
                "thermostat_id": 1,
                "date": latest_date.isoformat(),
                "sum_fan": 3600,
            }
        ]
        if latest_date is not None
        else []
    )
    coordinator.data = coordinator._build_runtime_data(
        summary_rows,
        [thermostat_row],
        [],
        evaluated_at,
        evaluated_at,
        True,
        None,
        None,
        evaluated_at=evaluated_at,
        fetched_at=evaluated_at,
    )
    return entry, coordinator, client


async def test_real_point_timer_projects_schedule_without_io(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 1, 13, 55, tzinfo=UTC)
    boundary = datetime(2026, 7, 1, 14, tzinfo=UTC)
    freezer.move_to(before)
    schedule = [["sleep"] * 48 for _ in range(7)]
    schedule[2][20] = "home"
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        schedule=schedule,
    )
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    coordinator._async_schedule_projection_boundary(coordinator.data)
    freezer.move_to(boundary)
    async_fire_time_changed_exact(hass, boundary)
    await hass.async_block_till_done()

    assert coordinator.data.thermostat_metadata[1].scheduled_climate_name == "Home"
    assert coordinator.data.thermostat_metadata[1].current_climate_name == "Hold"
    assert coordinator.data.projected_at == boundary
    assert client.calls == []
    assert updates == ["updated"]
    await entry._async_process_on_unload(hass)


async def test_source_refresh_replaces_real_stale_timer(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 1, 13, tzinfo=UTC)
    old_deadline = datetime(2026, 7, 1, 18, 30, 30, 1, tzinfo=UTC)
    new_deadline = datetime(2026, 7, 1, 19, 30, 30, 1, tzinfo=UTC)
    freezer.move_to(before)
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        data_end=datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
    )
    coordinator._async_schedule_projection_boundary(coordinator.data)
    refreshed = coordinator._build_runtime_data(
        [],
        [
            {
                "id": 1,
                "name": "Zone A",
                "data_end": datetime(2026, 7, 1, 12, 30, tzinfo=UTC).isoformat(),
            }
        ],
        [],
        before,
        before,
        True,
        None,
        None,
        evaluated_at=before,
        fetched_at=before,
    )
    coordinator.async_set_updated_data(refreshed)
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    freezer.move_to(old_deadline)
    async_fire_time_changed_exact(hass, old_deadline)
    await hass.async_block_till_done()
    assert updates == []

    freezer.move_to(new_deadline)
    async_fire_time_changed_exact(hass, new_deadline)
    await hass.async_block_till_done()

    assert coordinator.data.thermostat_metadata[1].data_lag_minutes == 421
    assert coordinator.data.projected_at == new_deadline
    assert client.calls == []
    assert updates == ["updated"]
    await entry._async_process_on_unload(hass)


async def test_entry_unload_cancels_projection_timer(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 6, 3, 59, tzinfo=UTC)
    midnight = datetime(2026, 7, 6, 4, tzinfo=UTC)
    freezer.move_to(before)
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        latest_date=date(2026, 7, 4),
    )
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))
    coordinator._async_schedule_projection_boundary(coordinator.data)

    await entry._async_process_on_unload(hass)
    freezer.move_to(midnight)
    async_fire_time_changed_exact(hass, midnight)
    await hass.async_block_till_done()

    assert coordinator._cancel_projection_boundary is None
    assert coordinator.data.projected_at == before
    assert client.calls == []
    assert updates == []


@pytest.mark.parametrize(
    ("before", "later"),
    [
        (datetime(2026, 7, 1, 13, tzinfo=UTC), datetime(2026, 7, 1, 13, 5, tzinfo=UTC)),
        (datetime(2026, 7, 6, 3, 59, tzinfo=UTC), datetime(2026, 7, 6, 4, tzinfo=UTC)),
    ],
    ids=["same_local_date", "empty_local_midnight"],
)
async def test_unchanged_projection_does_not_dispatch_entity_updates(
    hass: HomeAssistant,
    freezer: Any,
    before: datetime,
    later: datetime,
) -> None:
    freezer.move_to(later)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=before)
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    coordinator._async_rebuild_projection_from_cached(later)

    assert coordinator.data.projected_at == before
    assert client.calls == []
    assert updates == []
    await entry._async_process_on_unload(hass)


async def test_noop_midnight_rearms_real_timer_for_next_schedule_change(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 1, 3, 59, tzinfo=UTC)
    midnight = datetime(2026, 7, 1, 4, tzinfo=UTC)
    schedule_boundary = datetime(2026, 7, 1, 14, tzinfo=UTC)
    freezer.move_to(before)
    schedule = [["sleep"] * 48 for _ in range(7)]
    schedule[2][20] = "home"
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        schedule=schedule,
    )
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    coordinator._async_schedule_projection_boundary(coordinator.data)
    freezer.move_to(midnight)
    async_fire_time_changed_exact(hass, midnight)
    await hass.async_block_till_done()

    assert coordinator.data.projected_at == before
    assert updates == []

    freezer.move_to(schedule_boundary)
    async_fire_time_changed_exact(hass, schedule_boundary)
    await hass.async_block_till_done()

    assert coordinator.data.thermostat_metadata[1].scheduled_climate_name == "Home"
    assert coordinator.data.projected_at == schedule_boundary
    assert client.calls == []
    assert updates == ["updated"]
    await entry._async_process_on_unload(hass)


async def test_core_time_zone_update_reprojects_without_io_and_unloads(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    now = datetime(2026, 7, 1, 1, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=now,
        latest_date=date(2026, 6, 29),
    )
    scheduled: list[tuple[datetime, Mock]] = []

    def track_projection(_hass, _action, deadline):
        cancel = Mock()
        scheduled.append((deadline, cancel))
        return cancel

    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    with patch(
        "custom_components.beestat_statistics.coordinator."
        "async_track_point_in_utc_time",
        side_effect=track_projection,
    ):
        _async_track_time_zone_updates(hass, entry, coordinator)
        coordinator._async_schedule_projection_boundary(coordinator.data)
        assert coordinator.capture_temporal_context().local_tz == ZoneInfo(
            "America/New_York"
        )

        await hass.config.async_update(time_zone="Europe/London")
        await hass.async_block_till_done()

        assert coordinator.local_tz == ZoneInfo("Europe/London")
        assert coordinator.capture_temporal_context().local_tz == ZoneInfo(
            "Europe/London"
        )
        assert client.calls == []
        assert updates == ["updated"]
        assert len(scheduled) == 2
        scheduled[0][1].assert_called_once_with()

        await entry._async_process_on_unload(hass)
        scheduled[1][1].assert_called_once_with()

        await hass.config.async_update(time_zone="Asia/Tokyo")
        await hass.async_block_till_done()

    assert coordinator.local_tz == ZoneInfo("Europe/London")
    assert len(scheduled) == 2


@pytest.mark.parametrize(
    ("now", "time_zone"),
    [
        (datetime(2026, 7, 1, 1, tzinfo=UTC), "Europe/London"),
        (datetime(2026, 7, 1, 17, tzinfo=UTC), "America/Chicago"),
    ],
    ids=["cross_local_date", "same_local_date"],
)
async def test_core_time_zone_update_reschedules_without_unchanged_dispatch(
    hass: HomeAssistant,
    freezer: Any,
    now: datetime,
    time_zone: str,
) -> None:
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    scheduled: list[tuple[datetime, Mock]] = []

    def track_projection(_hass, _action, deadline):
        cancel = Mock()
        scheduled.append((deadline, cancel))
        return cancel

    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    with patch(
        "custom_components.beestat_statistics.coordinator."
        "async_track_point_in_utc_time",
        side_effect=track_projection,
    ):
        _async_track_time_zone_updates(hass, entry, coordinator)
        coordinator._async_schedule_projection_boundary(coordinator.data)

        await hass.config.async_update(time_zone=time_zone)
        await hass.async_block_till_done()

    assert coordinator.local_tz == ZoneInfo(time_zone)
    assert client.calls == []
    assert updates == []
    assert len(scheduled) == 2
    scheduled[0][1].assert_called_once_with()
    await entry._async_process_on_unload(hass)


async def test_import_restarts_before_recorder_write_after_timezone_change(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    now = datetime(2026, 7, 1, 1, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    importer = BeestatStatisticsImporter(
        hass,
        client,
        coordinator,
        point_lookback_days=31,
    )
    _async_track_time_zone_updates(hass, entry, coordinator)
    attempts: list[tuple[str, ZoneInfo, datetime]] = []
    writes: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def refresh_runtime(**_kwargs):
        return coordinator.data

    async def summary_plan(
        _runtime_data,
        *,
        force_full_summary,
        temporal_context,
    ):
        attempts.append(
            (
                "summary",
                temporal_context.local_tz,
                temporal_context.evaluated_at,
            )
        )
        return SummaryImportPlan(
            rows=[],
            seeds={},
            mode="full",
            window_start=None,
            window_end=None,
            overlap_days=None,
            fallback_reason=None,
        )

    async def thermostat_rows(
        _lookback_days,
        _runtime_data,
        _skipped_windows,
        **kwargs,
    ):
        context = kwargs["temporal_context"]
        attempts.append(("thermostat", context.local_tz, context.evaluated_at))
        if sum(name == "thermostat" for name, _zone, _at in attempts) == 1:
            await hass.config.async_update(time_zone="Europe/London")
            await hass.async_block_till_done()
        return {}

    async def sensor_rows(
        _lookback_days,
        _runtime_data,
        _skipped_windows,
        **kwargs,
    ):
        context = kwargs["temporal_context"]
        attempts.append(("sensor", context.local_tz, context.evaluated_at))
        return {}

    def build_series(_summary, _thermostat, _sensor, local_tz, _config):
        attempts.append(("build", local_tz, now))
        return [
            StatisticsSeries(
                metadata={"statistic_id": "beestat:test"},
                statistics=[{"start": now}],
                source_rows=0,
            )
        ]

    def add_statistics(_hass, metadata, statistics):
        writes.append((metadata, list(statistics)))

    with (
        patch.object(coordinator, "async_refresh_runtime", new=refresh_runtime),
        patch.object(importer, "_async_summary_import_plan", new=summary_plan),
        patch.object(importer, "_async_fetch_thermostat_rows", new=thermostat_rows),
        patch.object(importer, "_async_fetch_sensor_rows", new=sensor_rows),
        patch(
            "custom_components.beestat_statistics.build_statistics",
            side_effect=build_series,
        ),
        patch(
            "custom_components.beestat_statistics.async_add_external_statistics",
            side_effect=add_statistics,
        ),
    ):
        result = await importer.async_import_statistics(skip_sync=True)

    attempt_zones = [zone for _name, zone, _at in attempts]
    assert attempt_zones[:4] == [ZoneInfo("America/New_York")] * 4
    assert attempt_zones[4:] == [ZoneInfo("Europe/London")] * 4
    assert len({at for _name, _zone, at in attempts[:3]}) == 1
    assert len({at for _name, _zone, at in attempts[4:7]}) == 1
    assert len(writes) == 1
    assert result.imported_rows == 1
    await entry._async_process_on_unload(hass)


async def test_import_timezone_restart_is_bounded_before_recorder_write(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    now = datetime(2026, 7, 1, 1, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    importer = BeestatStatisticsImporter(
        hass,
        client,
        coordinator,
        point_lookback_days=31,
    )
    attempts = 0
    writes: list[object] = []
    summary_plan = SummaryImportPlan(
        rows=[],
        seeds={},
        mode="full",
        window_start=None,
        window_end=None,
        overlap_days=None,
        fallback_reason=None,
    )
    prepared = PreparedImport(
        summary_plan=summary_plan,
        summary_rows=[],
        skipped_windows=SkippedWindowEvidence(),
        thermostat_rows_by_id={},
        sensor_rows_by_id={},
        series=[
            StatisticsSeries(
                metadata={"statistic_id": "beestat:test"},
                statistics=[{"start": now}],
                source_rows=0,
            )
        ],
    )

    async def refresh_runtime(**_kwargs):
        return coordinator.data

    async def prepare_import(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        coordinator._timezone_revision += 1
        return prepared

    with (
        patch.object(coordinator, "async_refresh_runtime", new=refresh_runtime),
        patch.object(importer, "_async_prepare_import", new=prepare_import),
        patch(
            "custom_components.beestat_statistics.async_add_external_statistics",
            side_effect=lambda *_args: writes.append(object()),
        ),
        pytest.raises(RuntimeError, match="timezone changed repeatedly"),
    ):
        await importer.async_import_statistics(skip_sync=True)

    assert attempts == 3
    assert writes == []
    await entry._async_process_on_unload(hass)


async def test_boundary_crossed_during_timer_registration_runs_immediately(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 1, 13, 59, 59, 900000, tzinfo=UTC)
    after = datetime(2026, 7, 1, 14, 0, 0, 100000, tzinfo=UTC)
    schedule = [["sleep"] * 48 for _ in range(7)]
    schedule[2][20] = "home"
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        schedule=schedule,
    )
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    freezer.move_to(after)
    coordinator._async_schedule_projection_boundary(coordinator.data)
    await hass.async_block_till_done()

    assert coordinator.data.thermostat_metadata[1].scheduled_climate_name == "Home"
    assert coordinator.data.thermostat_metadata[1].current_climate_name == "Hold"
    assert coordinator.data.projected_at == after
    assert client.calls == []
    assert updates == ["updated"]
    await entry._async_process_on_unload(hass)
