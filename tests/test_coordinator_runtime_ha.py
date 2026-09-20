"""Exact-Core tests for coordinator acquisition and unload ownership."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime
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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beestat_statistics import _async_track_room_temperature_sources
from custom_components.beestat_statistics.api import BeestatClient
from custom_components.beestat_statistics.const import API_BASE, CONF_API_BASE, DOMAIN
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
)

pytestmark = pytest.mark.asyncio


def _coordinator(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
    )
    entry.add_to_hass(hass)
    client = Mock(spec=BeestatClient)
    client.async_read_id = AsyncMock(return_value=[])
    coordinator = BeestatRuntimeDataCoordinator(
        hass,
        entry,
        client,
        local_tz=ZoneInfo("America/New_York"),
    )
    return entry, coordinator, client


async def test_entry_unload_cancels_running_and_queued_runtime_refreshes(
    hass: HomeAssistant,
) -> None:
    """Old-account refreshes cannot publish or restart after native unload."""

    entry, coordinator, _client = _coordinator(hass)
    started = asyncio.Event()
    calls = 0

    async def fetch(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Cancelled acquisition must not resume")

    with patch.object(coordinator, "_async_fetch_runtime_data_locked", new=fetch):
        active = asyncio.create_task(coordinator.async_refresh_runtime())
        await started.wait()
        queued = asyncio.create_task(coordinator.async_refresh_runtime())
        await asyncio.sleep(0)
        await entry._async_process_on_unload(hass)
        outcomes = await asyncio.gather(active, queued, return_exceptions=True)

    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert calls == 1
    assert coordinator.data is None
    assert coordinator.last_error is None
    assert coordinator._cancel_filter_boundary_retry is None
    assert coordinator._cancel_projection_boundary is None
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_refresh_runtime()


async def test_entry_unload_cancels_alert_dismissal_before_next_alert(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """A pending alert command cannot continue against an unloaded account."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator(hass)
    coordinator.data = coordinator._build_runtime_data(
        [],
        [
            {
                "id": 1,
                "name": "Zone",
                "alerts": [
                    {"guid": "first", "text": "Replace filter"},
                    {"guid": "second", "text": "Replace filter"},
                ],
            }
        ],
        [],
        now,
        now,
        True,
        None,
        None,
    )
    started = asyncio.Event()

    async def dismiss(*_args):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Cancelled dismissal must not resume")

    client.async_dismiss_alert = AsyncMock(side_effect=dismiss)
    request = asyncio.create_task(coordinator.async_dismiss_filter_alerts(1))
    await started.wait()
    await entry._async_process_on_unload(hass)
    with pytest.raises(asyncio.CancelledError):
        await request
    client.async_dismiss_alert.assert_awaited_once_with(1, "first")
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_dismiss_filter_alerts(1)


async def test_framework_refresh_starts_cached_projection_scheduler(
    hass: HomeAssistant,
    freezer: Any,
) -> None:
    """Framework refreshes schedule projections without a manual import first."""

    freezer.move_to(datetime(2026, 7, 1, 16, tzinfo=UTC))
    entry, coordinator, _client = _coordinator(hass)

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data is not None
    assert coordinator._cancel_projection_boundary is not None
    await entry._async_process_on_unload(hass)
    assert coordinator._cancel_projection_boundary is None


@pytest.mark.parametrize(
    ("invalid_value", "invalid_class", "invalid_unit"),
    [
        ("22", "temperature_delta", "°C"),
        ("-274.15", "temperature", "°C"),
    ],
    ids=["attribute_only_invalid_class", "invalid_numeric_state"],
)
async def test_room_spread_invalid_observation_and_recovery_without_io(
    hass: HomeAssistant,
    freezer: Any,
    invalid_value: str,
    invalid_class: str,
    invalid_unit: str,
) -> None:
    """Live quantity changes affect coverage without dropping the selected source."""

    now = datetime(2026, 7, 1, 16, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "sensors": [
                {"id": 10, "temperature_entity_id": "sensor.room_a_temperature"},
                {"id": 11, "temperature_entity_id": "sensor.room_b_temperature"},
            ]
        },
    )
    attributes = {"device_class": "temperature", "unit_of_measurement": "°C"}
    hass.states.async_set("sensor.room_a_temperature", "21", attributes)
    hass.states.async_set("sensor.room_b_temperature", "22", attributes)
    await hass.async_block_till_done()
    coordinator.data = coordinator._build_runtime_data(
        [],
        [
            {
                "id": 1,
                "name": "Zone",
                "program": {
                    "currentClimateRef": "home",
                    "climates": [
                        {
                            "climateRef": "home",
                            "sensors": [
                                {"id": "rs:10:1", "name": "Room A"},
                                {"id": "rs:11:1", "name": "Room B"},
                            ],
                        }
                    ],
                },
            }
        ],
        [
            {
                "id": sensor_id,
                "thermostat_id": 1,
                "name": name,
                "identifier": f"rs:{sensor_id}",
                "type": "ecobee3_remote_sensor",
            }
            for sensor_id, name in ((10, "Room A"), (11, "Room B"))
        ],
        now,
        now,
        True,
        None,
        None,
    )
    initial = coordinator.data
    spread = initial.room_temperature_spreads[1]
    assert spread.value == 1.0
    assert spread.unit == "°C"
    assert spread.participating_sensor_count == spread.valid_sensor_count == 2
    assert spread.unavailable_sensor_names == ()
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    _async_track_room_temperature_sources(hass, entry)

    # Class cases keep the numeric state unchanged, exercising attribute-only events.
    hass.states.async_set(
        "sensor.room_b_temperature",
        invalid_value,
        {"device_class": invalid_class, "unit_of_measurement": invalid_unit},
    )
    await hass.async_block_till_done()
    invalid = coordinator.data.room_temperature_spreads[1]
    assert invalid.value is None
    assert invalid.participating_sensor_count == 2
    assert invalid.valid_sensor_count == 1
    assert invalid.participating_sensor_names == ("Room A", "Room B")
    assert invalid.unavailable_sensor_names == ("Room B",)
    assert coordinator.data.config == initial.config
    assert coordinator.data.sensor_metadata == initial.sensor_metadata

    hass.states.async_set("sensor.room_b_temperature", "22", attributes)
    await hass.async_block_till_done()
    assert coordinator.data.room_temperature_spreads[1] == spread
    for unit_attributes, value in (
        ({"unit_of_measurement": "K"}, "295.15"),
        ({"device_class": "temperature", "unit_of_measurement": "°F"}, "71.6"),
    ):
        hass.states.async_set("sensor.room_b_temperature", value, unit_attributes)
        await hass.async_block_till_done()
        assert coordinator.data.room_temperature_spreads[1] == spread
    assert coordinator.data.config == initial.config
    assert coordinator.data.fetched_at == initial.fetched_at
    assert client.mock_calls == []
    await entry._async_process_on_unload(hass)
