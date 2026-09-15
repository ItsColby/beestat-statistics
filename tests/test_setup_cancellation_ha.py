"""Native config-entry cancellation must release each failed setup's runtime."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform, PlatformData
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.beestat_statistics import async_setup_entry as native_setup_entry
from custom_components.beestat_statistics.api import BeestatClient
from custom_components.beestat_statistics.button import BeestatButton
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


@pytest.mark.parametrize(
    ("cancel_at", "rollback_error"),
    [
        ("initial_refresh", None),
        ("platform_forwarding", None),
        ("platform_forwarding", RuntimeError("rollback failed")),
        ("platform_forwarding", asyncio.CancelledError("rollback cancelled")),
    ],
)
async def test_cancelled_setup_releases_resources_and_retries(
    hass: HomeAssistant,
    freezer: Any,
    cancel_at: str,
    rollback_error: BaseException | None,
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

    native_unload = hass.config_entries.async_unload_platforms
    native_forward = hass.config_entries.async_forward_entry_setups
    forward_cancellations: list[asyncio.CancelledError] = []
    setup_cancellations: list[asyncio.CancelledError] = []
    native_platform_setup = EntityPlatform.async_setup_entry
    native_translations = PlatformData.async_load_translations
    button_loaded = asyncio.Event()

    async def rollback(*args: Any) -> bool:
        return await _unload_then_fail(native_unload, rollback_error, *args)

    async def track_platform(platform, config_entry) -> bool:
        result = await native_platform_setup(platform, config_entry)
        if platform.platform_name == DOMAIN and platform.domain == Platform.BUTTON:
            button_loaded.set()
        return result

    date_task: asyncio.Task[Any] | None = None

    async def blocked_translations(platform_data: PlatformData) -> None:
        nonlocal date_task
        if (
            platform_data.platform_name == DOMAIN
            and platform_data.domain == Platform.DATE
        ):
            date_task = asyncio.current_task()
            forwarding_started.set()
            await release.wait()
        await native_translations(platform_data)

    old_entities = {}
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
            client.async_sync_runtime.side_effect = (
                blocked_sync if cancel_at == "initial_refresh" else None
            )
            with (
                patch(
                    "custom_components.beestat_statistics.async_setup_entry",
                    partial(
                        _record_cancellation, native_setup_entry, setup_cancellations
                    ),
                ),
                patch.object(
                    hass.config_entries,
                    "async_forward_entry_setups",
                    partial(
                        _record_cancellation, native_forward, forward_cancellations
                    ),
                ),
                patch.object(hass.config_entries, "async_unload_platforms", rollback),
                patch.object(EntityPlatform, "async_setup_entry", track_platform),
                patch.object(
                    PlatformData, "async_load_translations", blocked_translations
                ),
            ):
                async with asyncio.timeout(10):
                    setup_task = asyncio.create_task(
                        hass.config_entries.async_setup(entry.entry_id)
                    )
                    if cancel_at == "platform_forwarding":
                        await forwarding_started.wait()
                        await button_loaded.wait()
                        assert (
                            entry.entry_id
                            in hass.data["entity_components"]["date"]._platforms
                        )
                        component = hass.data["entity_components"]["button"]
                        old_platform = component._platforms[entry.entry_id]
                        old_entities = dict(old_platform.entities)
                        assert old_entities
                        assert all(hass.states.get(key) for key in old_entities)
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
                    setup_task.cancel("setup cancelled")
                    with pytest.raises(asyncio.CancelledError):
                        await setup_task

            assert entry.state is ConfigEntryState.SETUP_ERROR
            assert len(setup_cancellations) == 1
            assert setup_cancellations[0] is not rollback_error
            if cancel_at == "platform_forwarding":
                assert len(forward_cancellations) == 1
                assert setup_cancellations[0] is forward_cancellations[0]
                assert date_task is not None and date_task.cancelled()
                assert all(
                    entry.entry_id not in entity_component._platforms
                    for entity_component in hass.data["entity_components"].values()
                )
                assert not old_platform.entities
                assert all(component.get_entity(key) is None for key in old_entities)
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
            _assert_replacement_buttons(hass, old_entities, new_runtime)
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


def _assert_replacement_buttons(
    hass: HomeAssistant, old_entities: dict, runtime: Any
) -> None:
    """Native retry keeps IDs while replacing every failed runtime owner."""
    component = hass.data["entity_components"]["button"] if old_entities else None
    for entity_id, old_entity in old_entities.items():
        entity = component.get_entity(entity_id)
        assert entity is not None and entity is not old_entity
        if isinstance(entity, BeestatButton):
            assert entity._coordinator is runtime.coordinator
            assert entity._importer is runtime.importer
        else:
            assert entity.coordinator is runtime.coordinator
        assert hass.states.get(entity_id) is not None


async def _unload_then_fail(
    unload: Any, error: BaseException | None, *args: Any
) -> bool:
    """Inject rollback failure after real platform cleanup has completed."""
    result = await unload(*args)
    if error is not None:
        raise error
    return result


async def _record_cancellation(
    call: Any, observed: list[asyncio.CancelledError], *args: Any
) -> Any:
    """Observe the same native call's exception before Core can replace its message."""
    try:
        return await call(*args)
    except asyncio.CancelledError as err:
        observed.append(err)
        raise
