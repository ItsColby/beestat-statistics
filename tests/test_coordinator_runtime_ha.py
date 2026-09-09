"""Exact-Core tests for coordinator acquisition and unload ownership."""

from __future__ import annotations

import asyncio
import sys
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
