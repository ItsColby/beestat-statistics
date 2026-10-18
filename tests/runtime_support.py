"""Loaded-coordinator fixtures shared by Home Assistant runtime tests."""

from __future__ import annotations

import types
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.beestat_statistics.const import (
    API_BASE,
    CONF_API_BASE,
    DOMAIN,
)
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)


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
