"""Exact-Core lifecycle tests for cached Beestat temporal projections."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import pytest
    from homeassistant.const import CONF_API_KEY
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed_exact,
    )
except ModuleNotFoundError as err:  # pragma: no cover - local non-HA test env
    raise unittest.SkipTest(f"Home Assistant test harness unavailable: {err}") from err

from custom_components.beestat_statistics.const import API_BASE, CONF_API_BASE, DOMAIN
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)

pytestmark = pytest.mark.asyncio


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

    coordinator._async_schedule_projection_boundary(coordinator.data, now=before)
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
    old_deadline = datetime(2026, 7, 1, 13, 30, 30, 1, tzinfo=UTC)
    new_deadline = datetime(2026, 7, 1, 14, 30, 30, 1, tzinfo=UTC)
    freezer.move_to(before)
    entry, coordinator, client = _coordinator_data(
        hass,
        evaluated_at=before,
        data_end=datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
    )
    coordinator._async_schedule_projection_boundary(coordinator.data, now=before)
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

    assert coordinator.data.thermostat_metadata[1].data_lag_minutes == 121
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
    coordinator._async_schedule_projection_boundary(coordinator.data, now=before)

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
