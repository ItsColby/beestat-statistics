"""Native setup behavior when Beestat declines to synchronize."""

from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.beestat_statistics.api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
    BeestatSyncRejectedError,
)
from custom_components.beestat_statistics.const import (
    API_BASE,
    CONF_API_BASE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
from custom_components.beestat_statistics.importer import BeestatStatisticsImporter


@pytest.mark.usefixtures("recorder_mock", "unload_entries")
@pytest.mark.parametrize(
    ("sync_error", "read_error", "expected_state"),
    [
        (BeestatSyncRejectedError, None, ConfigEntryState.LOADED),
        (BeestatSyncRejectedError, BeestatApiError, ConfigEntryState.SETUP_RETRY),
        (BeestatSyncRejectedError, BeestatAuthError, ConfigEntryState.SETUP_ERROR),
        (BeestatApiError, None, ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_startup_after_sync_failure(
    hass: HomeAssistant,
    sync_error: type[BeestatApiError],
    read_error: type[BeestatApiError] | None,
    expected_state: ConfigEntryState,
) -> None:
    """Read existing data and schedule import only if startup reads succeed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
    )
    entry.add_to_hass(hass)
    client = Mock(spec=BeestatClient)
    client.async_sync_runtime.side_effect = sync_error("sync failed")
    client.redact_error.side_effect = str
    client.async_read_id.return_value = []
    if read_error is not None:
        client.async_read_id.side_effect = read_error("read failed")

    with (
        patch(
            "custom_components.beestat_statistics.BeestatClient", return_value=client
        ),
        patch.object(BeestatStatisticsImporter, "async_import_statistics") as importer,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is (
            expected_state is ConfigEntryState.LOADED
        )
        await hass.async_block_till_done(wait_background_tasks=True)

        assert entry.state is expected_state
        client.async_sync_runtime.assert_awaited_once()
        if expected_state is ConfigEntryState.LOADED:
            coordinator = entry.runtime_data.coordinator
            assert coordinator.data.sync_success_at is None
            assert coordinator.data.metadata_sync_success_at is None
            importer.assert_awaited_once_with(skip_sync=True)

            await coordinator.async_refresh()
            assert not coordinator.last_update_success
            assert coordinator.data.sync_success_at is None
        else:
            importer.assert_not_awaited()
            if sync_error is BeestatApiError:
                client.async_read_id.assert_not_awaited()
