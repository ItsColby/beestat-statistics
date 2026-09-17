"""Native action and runtime ownership proof for offline raw point responses."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components import beestat_statistics as integration
from custom_components.beestat_statistics.api import BeestatClient
from custom_components.beestat_statistics.config_model import ConfiguredSensor
from custom_components.beestat_statistics.const import DOMAIN
from tests.test_api_response import _FakeContent, _FakeResponse, _FakeSession
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 10, 18, tzinfo=UTC)


async def _runtime(hass, freezer, monkeypatch, payloads):
    freezer.move_to(NOW)
    entry, coordinator, _ = _coordinator_data(hass, evaluated_at=NOW)
    coordinator.data = replace(
        coordinator.data,
        config=replace(
            coordinator.data.config,
            sensors=(
                ConfiguredSensor(
                    10, "room", "Room", 1, "zone_a", True, False, False, True
                ),
            ),
        ),
        sensor_rows=({"id": 10, "thermostat_id": 1},),
    )
    session = _FakeSession(payloads)
    client = BeestatClient(session, "fixture-secret", "https://api.test/", retries=1)
    importer = integration.BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    entry.runtime_data = SimpleNamespace(
        client=client, coordinator=coordinator, importer=importer
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await integration.async_setup(hass, {})
    forbidden = []
    for owner, methods in (
        (
            client,
            (
                "async_sync_runtime",
                "async_sync_resource",
                "async_read_id",
                "async_dismiss_alert",
                "async_read_runtime_thermostat_summary",
            ),
        ),
        (coordinator, ("async_refresh_runtime", "async_record_import_result")),
        (importer.hourly, ("async_reconcile", "async_select", "async_import")),
    ):
        for method in methods:
            spy = AsyncMock(
                side_effect=AssertionError(f"Forbidden side effect: {method}")
            )
            monkeypatch.setattr(owner, method, spy)
            forbidden.append(spy)
    for owner, method in (
        (integration, "async_add_external_statistics"),
        (hass.config_entries, "async_update_entry"),
        (Store, "async_delay_save"),
        (entry, "async_start_reauth_if_available"),
    ):
        spy = Mock(side_effect=AssertionError(f"Forbidden write: {method}"))
        monkeypatch.setattr(owner, method, spy)
        forbidden.append(spy)
    save = AsyncMock(side_effect=AssertionError("Forbidden Store write"))
    monkeypatch.setattr(Store, "async_save", save)
    forbidden.append(save)
    return entry, coordinator, importer, session, forbidden


def _request(entry, **changes):
    return {
        "config_entry_id": entry.entry_id,
        "resource": "runtime_thermostat",
        "resource_id": 1,
        "start": (NOW - timedelta(days=1)).isoformat(),
        "end": NOW.isoformat(),
        **changes,
    }


async def _call(hass, entry, context, **changes):
    return await hass.services.async_call(
        DOMAIN,
        "get_raw_points",
        _request(entry, **changes),
        blocking=True,
        return_response=True,
        context=context,
    )


async def test_native_action_uses_existing_client_for_both_kinds_without_writes(
    hass, hass_admin_user, freezer, monkeypatch
):
    rows = [
        {"timestamp": "duplicate", "fan": 30},
        {"timestamp": "duplicate", "deleted": True},
    ]
    keyed = {"second": {"temperature": 72}, "first": {"deleted": True}}
    entry, _, _, session, forbidden = await _runtime(
        hass, freezer, monkeypatch, [{"data": rows}, {"data": keyed}]
    )
    context = Context(user_id=hass_admin_user.id)
    thermostat = await _call(hass, entry, context)
    sensor = await _call(
        hass, entry, context, resource="runtime_sensor", resource_id=10
    )
    assert thermostat["data"] == rows
    assert sensor["data"] == keyed
    assert list(sensor["data"]) == ["second", "first"]
    assert "id" not in sensor["data"]["second"]
    assert sensor["identity"]["thermostat_id"] == 1
    assert sensor["identity"]["config_entry_id"] == entry.entry_id
    assert [request["resource"] for request in session.requests] == [
        "runtime_thermostat",
        "runtime_sensor",
    ]
    assert all(request["method"] == "read" for request in session.requests)
    for spy in forbidden:
        spy.assert_not_called()
    await entry._async_process_on_unload(hass)


async def test_native_admin_context_and_invalid_identity_make_no_transport_call(
    hass, hass_admin_user, freezer, monkeypatch
):
    reader = await hass.auth.async_create_user(
        "Raw fixture reader", group_ids=[GROUP_ID_USER]
    )
    entry, _, _, session, forbidden = await _runtime(hass, freezer, monkeypatch, [])
    for context in (
        Context(),
        Context(user_id="missing-user"),
        Context(user_id=reader.id),
    ):
        with pytest.raises(HomeAssistantError):
            await _call(hass, entry, context)
    admin = Context(user_id=hass_admin_user.id)
    for changes in (
        {"resource_id": 2},
        {"resource": "runtime_sensor", "resource_id": 99},
        {"start": (NOW - timedelta(days=32)).isoformat()},
        {"start": NOW.replace(tzinfo=None).isoformat()},
        {"end": NOW.replace(microsecond=1).isoformat()},
    ):
        with pytest.raises(HomeAssistantError):
            await _call(hass, entry, admin, **changes)
    assert session.call_count == 0
    for spy in forbidden:
        spy.assert_not_called()
    await entry._async_process_on_unload(hass)


async def test_native_provider_auth_failure_returns_receipt_without_reauth_or_writes(
    hass, hass_admin_user, freezer, monkeypatch
):
    entry, _, _, session, forbidden = await _runtime(
        hass,
        freezer,
        monkeypatch,
        [_FakeResponse({"error": "fixture-secret"}, status=401)],
    )
    result = await _call(hass, entry, Context(user_id=hass_admin_user.id))
    assert result["status"] == "failed"
    assert result["attempts"][0]["outcome"] == "authentication_failed"
    assert "data" not in result
    assert "fixture-secret" not in str(result)
    assert session.call_count == 1
    for spy in forbidden:
        spy.assert_not_called()
    await entry._async_process_on_unload(hass)


class _PausedContent(_FakeContent):
    def __init__(self, started, proceed):
        super().__init__(b'{"data": [{"fan": 30}]}')
        self.started = started
        self.proceed = proceed

    async def iter_chunked(self, size):
        self.started.set()
        await self.proceed.wait()
        async for chunk in super().iter_chunked(size):
            yield chunk


@pytest.mark.parametrize("changed_owner", ["metadata", "runtime"])
async def test_native_runtime_change_discards_inflight_response(
    hass, hass_admin_user, freezer, monkeypatch, changed_owner
):
    started, proceed = asyncio.Event(), asyncio.Event()
    response = _FakeResponse({})
    response.content = _PausedContent(started, proceed)
    entry, coordinator, _, session, forbidden = await _runtime(
        hass, freezer, monkeypatch, [response]
    )
    task = asyncio.create_task(_call(hass, entry, Context(user_id=hass_admin_user.id)))
    await asyncio.wait_for(started.wait(), 1)
    if changed_owner == "metadata":
        coordinator.data = replace(
            coordinator.data, fetched_at=NOW + timedelta(seconds=1)
        )
    else:
        entry.runtime_data = SimpleNamespace(
            client=entry.runtime_data.client,
            coordinator=coordinator,
            importer=entry.runtime_data.importer,
        )
    proceed.set()
    with pytest.raises(HomeAssistantError):
        await task
    assert session.call_count == 1
    for spy in forbidden:
        spy.assert_not_called()
    await entry._async_process_on_unload(hass)


async def test_native_unload_cancels_read_and_rejects_queued_request(
    hass, hass_admin_user, freezer, monkeypatch
):
    started, proceed = asyncio.Event(), asyncio.Event()
    response = _FakeResponse({})
    response.content = _PausedContent(started, proceed)
    entry, _, _, session, forbidden = await _runtime(
        hass, freezer, monkeypatch, [response]
    )
    context = Context(user_id=hass_admin_user.id)
    task = asyncio.create_task(_call(hass, entry, context))
    await asyncio.wait_for(started.wait(), 1)
    queued = asyncio.create_task(_call(hass, entry, context))
    await asyncio.sleep(0)
    await entry._async_process_on_unload(hass)
    for pending in (task, queued):
        with pytest.raises((asyncio.CancelledError, HomeAssistantError)):
            await pending
    with pytest.raises(HomeAssistantError):
        await _call(hass, entry, context)
    assert session.call_count == 1
    for spy in forbidden:
        spy.assert_not_called()
