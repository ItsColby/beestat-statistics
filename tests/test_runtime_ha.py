"""Exact-Core lifecycle tests for cached Beestat temporal projections."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.beestat_statistics import (
    BeestatStatisticsImporter,
    PreparedImport,
    SummaryImportPlan,
    _async_track_time_zone_updates,
    _dedupe_rows,
    _row_float,
    _row_start_datetime,
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


async def test_unchanged_projection_does_not_dispatch_entity_updates(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 1, 13, tzinfo=UTC)
    later = before + timedelta(minutes=5)
    freezer.move_to(later)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=before)
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    coordinator._async_rebuild_projection_from_cached(later)

    assert coordinator.data.projected_at == before
    assert client.calls == []
    assert updates == []
    await entry._async_process_on_unload(hass)


async def test_empty_local_midnight_projection_does_not_dispatch_entity_updates(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    before = datetime(2026, 7, 6, 3, 59, tzinfo=UTC)
    midnight = datetime(2026, 7, 6, 4, tzinfo=UTC)
    freezer.move_to(midnight)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=before)
    updates: list[str] = []
    coordinator.async_add_listener(lambda: updates.append("updated"))

    coordinator._async_rebuild_projection_from_cached(midnight)

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


async def test_cross_date_time_zone_update_without_sensitive_state_only_reschedules(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    now = datetime(2026, 7, 1, 1, tzinfo=UTC)
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

        await hass.config.async_update(time_zone="Europe/London")
        await hass.async_block_till_done()

    assert coordinator.local_tz == ZoneInfo("Europe/London")
    assert client.calls == []
    assert updates == []
    assert len(scheduled) == 2
    scheduled[0][1].assert_called_once_with()
    await entry._async_process_on_unload(hass)


async def test_core_time_zone_update_reschedules_without_unchanged_dispatch(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    now = datetime(2026, 7, 1, 17, tzinfo=UTC)
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

        await hass.config.async_update(time_zone="America/Chicago")
        await hass.async_block_till_done()

    assert coordinator.local_tz == ZoneInfo("America/Chicago")
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
