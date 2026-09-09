"""Exact-Core tests for Beestat entity state and action boundaries."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.const import CONF_API_KEY, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beestat_statistics.api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
)
from custom_components.beestat_statistics.button import BeestatFilterChangedButton
from custom_components.beestat_statistics.const import API_BASE, CONF_API_BASE, DOMAIN
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
    RoomTemperatureSpread,
)
from custom_components.beestat_statistics.date import BeestatFilterChangedDate
from custom_components.beestat_statistics.sensor import (
    BeestatSensor,
    _thermostat_sensor_descriptions,
)

pytestmark = pytest.mark.asyncio
_LOGGER = logging.getLogger(__name__)


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, freezer: Any
) -> AsyncIterator[BeestatRuntimeDataCoordinator]:
    """Keep source and projection clocks coherent and always cancel owned timers."""

    now = datetime(2026, 7, 5, 12, tzinfo=UTC)
    freezer.move_to(now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
    )
    entry.add_to_hass(hass)
    coordinator = BeestatRuntimeDataCoordinator(
        hass, entry, Mock(spec=BeestatClient), local_tz=ZoneInfo("UTC")
    )
    coordinator.data = coordinator._build_runtime_data(
        [],
        [{"id": 1, "name": "Zone A"}],
        [],
        now,
        now,
        True,
        None,
        None,
        evaluated_at=now,
        fetched_at=now,
    )
    try:
        yield coordinator
    finally:
        await entry._async_process_on_unload(hass)


@asynccontextmanager
async def _entity_platform(
    coordinator: BeestatRuntimeDataCoordinator, domain: str
) -> AsyncIterator[EntityPlatform]:
    """Register entities under their real config-entry owner and always remove them."""

    platform = EntityPlatform(
        hass=coordinator.hass,
        logger=_LOGGER,
        domain=domain,
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = coordinator.beestat_config_entry
    try:
        yield platform
    finally:
        await platform.async_reset()


async def test_spread_recovers_and_changes_native_unit_with_its_value(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """A late projection and later unit changes must retain valid numeric state."""

    thermostat = coordinator.data.config.thermostats[0]
    description = next(
        description
        for description in _thermostat_sensor_descriptions(thermostat=thermostat)
        if description.translation_key == "current_profile_room_temperature_spread"
    )
    entity = BeestatSensor(coordinator, description, None)
    entity.entity_id = "sensor.profile_spread"
    async with _entity_platform(coordinator, "sensor") as platform:
        await platform.async_add_entities([entity])
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE

        for unit, value in (("°C", 2.0), ("°F", 3.6)):
            projection = RoomTemperatureSpread(
                value=value,
                unit=unit,
                participating_sensor_count=2,
                valid_sensor_count=2,
                participating_sensor_names=("Bedroom", "Office"),
                unavailable_sensor_names=(),
                hottest_sensor_name="Office",
                coldest_sensor_name="Bedroom",
            )
            coordinator.async_set_updated_data(
                replace(coordinator.data, room_temperature_spreads={1: projection})
            )
            await hass.async_block_till_done()
            state = hass.states.get(entity.entity_id)
            assert entity.native_unit_of_measurement == unit
            assert entity.native_value == value
            # HA may retain the display unit while converting the new native unit.
            assert state.attributes["unit_of_measurement"] in {"°C", "°F"}
            expected = 2.0 if state.attributes["unit_of_measurement"] == "°C" else 3.6
            assert float(state.state) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("error_type", "translation_key", "reauth"),
    [
        (BeestatAuthError, "beestat_auth_failed", True),
        (BeestatApiError, "beestat_request_failed", False),
        (RuntimeError, "beestat_request_failed", False),
    ],
)
async def test_filter_date_errors_are_sanitized_and_auth_starts_reauth(
    coordinator: BeestatRuntimeDataCoordinator,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    translation_key: str,
    reauth: bool,
) -> None:
    entity = BeestatFilterChangedDate(
        coordinator, coordinator.data.config.thermostats[0]
    )
    private_detail = "private-unexpected-response-detail"
    with (
        patch(
            "custom_components.beestat_statistics.date.async_set_filter_changed_date",
            new=AsyncMock(side_effect=error_type(private_detail)),
        ),
        patch.object(
            coordinator.beestat_config_entry, "async_start_reauth_if_available"
        ) as start_reauth,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await entity.async_set_value(date(2026, 7, 1))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == translation_key
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert private_detail not in str(raised.value)
    assert private_detail not in caplog.text
    assert start_reauth.called is reauth


async def test_filter_button_tracks_source_removal_recovery_and_cloud_failure(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """The physical-change control updates with source scope and survives cloud loss."""

    initial_data = coordinator.data
    entity = BeestatFilterChangedButton(coordinator, initial_data.config.thermostats[0])
    entity.entity_id = "button.mark_filter_changed"
    async with _entity_platform(coordinator, "button") as platform:
        await platform.async_add_entities([entity])
        entry_id = coordinator.beestat_config_entry.entry_id
        assert entity.registry_entry.config_entry_id == entry_id
        assert entity.device_entry.config_entry_id == entry_id
        assert hass.states.get(entity.entity_id).state != STATE_UNAVAILABLE

        now = initial_data.projected_at
        removed_data = coordinator._build_runtime_data(
            [],
            [],
            [],
            now,
            now,
            True,
            None,
            None,
            evaluated_at=now,
            fetched_at=now,
        )
        assert not removed_data.config.thermostats
        assert not removed_data.thermostat_rows
        coordinator.async_set_updated_data(removed_data)
        await hass.async_block_till_done()
        assert not entity.available
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE

        coordinator.async_set_updated_data(initial_data)
        coordinator.last_update_success = False
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert entity.available
        assert hass.states.get(entity.entity_id).state != STATE_UNAVAILABLE

    assert not coordinator._listeners
