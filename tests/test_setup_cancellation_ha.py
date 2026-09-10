"""Native config-entry cancellation must release each failed setup's runtime."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.beestat_statistics.api import BeestatClient
from custom_components.beestat_statistics.const import (
    API_BASE,
    CONF_API_BASE,
    CONF_FILTER_CHANGED_ENTITY_ID,
    CONF_ID,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_THERMOSTATS,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations"),
]


@pytest.mark.parametrize("cancel_at", ["initial_refresh", "platform_forwarding"])
async def test_cancelled_setup_releases_resources_and_retries(
    hass: HomeAssistant, freezer: Any, cancel_at: str
) -> None:
    """Cancel the actual setup task, then use HA's reload path for a clean retry."""

    now = datetime(2026, 7, 6, 12, tzinfo=UTC)
    freezer.move_to(now)
    await hass.config.async_update(time_zone="UTC")
    await hass.async_block_till_done()
    # Load the integration component before adding its entry, so cancellation
    # reaches the entry's setup task instead of a shielded component loader.
    assert await async_setup_component(hass, DOMAIN, {})
    helper_id = "input_datetime.filter_changed"
    hass.states.async_set(helper_id, "2026-07-01")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
        options={
            CONF_SCAN_INTERVAL_SECONDS: MIN_SCAN_INTERVAL_SECONDS,
            CONF_THERMOSTATS: [{CONF_ID: 1, CONF_FILTER_CHANGED_ENTITY_ID: helper_id}],
        },
    )
    entry.add_to_hass(hass)
    saved_options = entry.options
    summary_rows = [{"thermostat_id": 1, "date": "2026-07-05", "sum_fan": 3600}]
    client = Mock(spec=BeestatClient)
    client.async_read_id.side_effect = lambda resource: {
        "thermostat": [{"id": 1, "name": "Zone A"}],
        "runtime_thermostat_summary": summary_rows,
    }.get(resource, [])
    client.async_read_runtime_thermostat_summary.return_value = summary_rows
    client.async_read_runtime_thermostat.return_value = []
    client.async_read_runtime_sensor.return_value = []

    acquisition_started = asyncio.Event()
    forwarding_started = asyncio.Event()
    release = asyncio.Event()
    acquisition_task: asyncio.Task[Any] | None = None

    async def blocked_sync() -> None:
        nonlocal acquisition_task
        acquisition_task = asyncio.current_task()
        acquisition_started.set()
        await release.wait()

    native_forward = hass.config_entries.async_forward_entry_setups

    async def blocked_forward(*args: Any, **kwargs: Any) -> None:
        # Hold the awaited forwarding boundary while the real timers and
        # listeners acquired before it remain active.
        forwarding_started.set()
        await release.wait()
        await native_forward(*args, **kwargs)

    setup_task: asyncio.Task[bool] | None = None
    with (
        patch(
            "custom_components.beestat_statistics.BeestatClient", return_value=client
        ),
        patch(
            "custom_components.beestat_statistics.async_add_external_statistics"
        ) as recorder_write,
    ):
        try:
            if cancel_at == "initial_refresh":
                client.async_sync_runtime.side_effect = blocked_sync
            with patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                new=blocked_forward,
            ):
                async with asyncio.timeout(10):
                    setup_task = asyncio.create_task(
                        hass.config_entries.async_setup(entry.entry_id)
                    )
                    if cancel_at == "platform_forwarding":
                        await forwarding_started.wait()
                        client.async_sync_runtime.side_effect = blocked_sync
                        # An import can already be running while platform setup
                        # awaits. Exercise its real entry-owned task as well.
                        import_at = now + timedelta(
                            seconds=MIN_SCAN_INTERVAL_SECONDS + 1
                        )
                        freezer.move_to(import_at)
                        async_fire_time_changed_exact(hass, import_at)
                    await acquisition_started.wait()
                    old_runtime = entry.runtime_data
                    setup_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await setup_task

            assert entry.state is ConfigEntryState.SETUP_ERROR
            assert old_runtime.coordinator.is_closed
            assert acquisition_task is not None and acquisition_task.cancelled()
            assert entry.options is saved_options
            recorder_write.assert_not_called()
            with pytest.raises(asyncio.CancelledError):
                await old_runtime.coordinator.async_refresh_runtime()
            with pytest.raises(RuntimeError, match="unloaded"):
                await old_runtime.importer.async_import_statistics()

            old_data = old_runtime.coordinator.data
            old_context = old_runtime.coordinator.capture_temporal_context()
            completed_io = list(client.mock_calls)
            release.set()
            await hass.config.async_update(time_zone="America/New_York")
            hass.states.async_set(helper_id, "2026-07-02")
            after_deadlines = now + timedelta(days=2)
            freezer.move_to(after_deadlines)
            async_fire_time_changed_exact(hass, after_deadlines)
            await hass.async_block_till_done()

            assert client.mock_calls == completed_io
            assert old_runtime.coordinator.data is old_data
            assert old_runtime.coordinator.local_tz == old_context.local_tz
            assert entry.options is saved_options
            recorder_write.assert_not_called()

            client.async_sync_runtime.side_effect = None
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert entry.state is ConfigEntryState.LOADED
            new_runtime = entry.runtime_data
            assert new_runtime is not old_runtime
            assert not new_runtime.coordinator.is_closed
            assert new_runtime.coordinator.local_tz.key == "America/New_York"
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert any(
                hass.states.get(entity.entity_id) is not None for entity in entities
            )
            result = await new_runtime.importer.async_import_statistics(skip_sync=True)
            assert result.imported_rows > 0
            assert new_runtime.coordinator.last_import_success_at is not None
            assert recorder_write.called
            assert old_runtime.coordinator.data is old_data
        finally:
            if setup_task is not None and not setup_task.done():
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
            release.set()
            if entry.state.recoverable:
                assert await hass.config_entries.async_unload(entry.entry_id)
