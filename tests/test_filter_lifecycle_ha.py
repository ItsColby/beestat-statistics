"""Native entity dispatch must not revive an unloaded filter mutation owner."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.components import button
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.beestat_statistics.const import thermostat_entity_unique_id
from custom_components.beestat_statistics.date import BeestatFilterChangedDate
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_queued_filter_button_cannot_save_after_platform_unload(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A real button service queued behind refresh resumes only to be rejected."""

    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, importer=types.SimpleNamespace()
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup_component(hass, button.DOMAIN, {})
    component = hass.data[button.DATA_COMPONENT]
    assert await component.async_setup_entry(entry)
    refresh_entity = next(
        entity for entity in component.entities if entity.unique_id == "refresh_runtime"
    )
    filter_entity = next(
        entity
        for entity in component.entities
        if entity.unique_id == thermostat_entity_unique_id(1, "mark_filter_changed")
    )
    assert refresh_entity.parallel_updates is filter_entity.parallel_updates
    assert filter_entity.parallel_updates is not None
    fetch_started = asyncio.Event()
    filter_queued = asyncio.Event()
    native_request_call = filter_entity.async_request_call

    async def blocked_fetch(*args: Any, **kwargs: Any) -> None:
        fetch_started.set()
        await asyncio.Event().wait()

    async def queued_request_call(*args: Any, **kwargs: Any) -> None:
        filter_queued.set()
        await native_request_call(*args, **kwargs)

    tasks: list[asyncio.Task[Any]] = []
    platform_unloaded = False
    saved = entry.options
    try:
        with (
            patch.object(coordinator, "_async_fetch_runtime_data", new=blocked_fetch),
            patch.object(filter_entity, "async_request_call", new=queued_request_call),
            patch.object(
                filter_entity, "async_press", wraps=filter_entity.async_press
            ) as filter_press,
            patch.object(
                coordinator,
                "async_dismiss_filter_alerts",
                new=AsyncMock(return_value=0),
            ) as dismiss,
        ):
            async with asyncio.timeout(5):
                tasks.append(
                    asyncio.create_task(
                        hass.services.async_call(
                            button.DOMAIN,
                            button.SERVICE_PRESS,
                            {"entity_id": refresh_entity.entity_id},
                            blocking=True,
                        )
                    )
                )
                await fetch_started.wait()
                tasks.append(
                    asyncio.create_task(
                        hass.services.async_call(
                            button.DOMAIN,
                            button.SERVICE_PRESS,
                            {"entity_id": filter_entity.entity_id},
                            blocking=True,
                        )
                    )
                )
                await filter_queued.wait()
                assert filter_entity.parallel_updates.locked()
                assert not tasks[1].done()
                filter_press.assert_not_awaited()
                entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
                assert await component.async_unload_entry(entry)
                platform_unloaded = True
                await entry._async_process_on_unload(hass)
                results = await asyncio.gather(*tasks, return_exceptions=True)
            assert coordinator.is_closed
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            filter_press.assert_awaited_once()
            assert entry.options is saved
            dismiss.assert_not_awaited()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not platform_unloaded:
            await component.async_unload_entry(entry)
        await entry._async_process_on_unload(hass)


async def test_stale_native_date_callback_cannot_save_after_unload(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A retained date callback cannot write through its former coordinator."""

    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entity = BeestatFilterChangedDate(
        coordinator, coordinator.data.config.thermostats[0]
    )
    saved = entry.options
    await entry._async_process_on_unload(hass)
    with (
        patch.object(coordinator, "async_refresh_runtime", new=AsyncMock()) as refresh,
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock()
        ) as dismiss,
        pytest.raises(asyncio.CancelledError),
    ):
        await entity.async_set_value(date(2026, 7, 5))
    assert entry.options is saved
    refresh.assert_not_awaited()
    dismiss.assert_not_awaited()
