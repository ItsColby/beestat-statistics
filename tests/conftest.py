"""Home Assistant test fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.hourly_support import RECORDER_NOW

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the local custom integration in every HA test."""


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Resolve the recorder database before auto-enabled integrations use HA."""


@pytest.fixture
async def started_recorder(recorder_mock: Any, freezer: Any) -> AsyncIterator[None]:
    """Use the harness's disposable SQLite and restore both timezone owners."""

    # recorder_mock must create the database before its own hass dependency.
    hass: HomeAssistant = recorder_mock.hass
    previous_zone = hass.config.time_zone
    previous_default = dt_util.get_default_time_zone()
    freezer.move_to(RECORDER_NOW)
    await hass.config.async_set_time_zone("America/New_York")
    try:
        await hass.async_start()
        yield
    finally:
        await hass.config.async_set_time_zone(previous_zone)
        dt_util.set_default_time_zone(previous_default)
