"""Native service persistence and real Recorder correction acceptance tests."""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import (
    ImportStatisticsTask,
    RecorderTask,
    SynchronizeTask,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.beestat_statistics import BeestatStatisticsImporter, async_setup
from custom_components.beestat_statistics.const import (
    DOMAIN,
    SERVICE_RECORD_FILTER_CHANGE,
    SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
)
from custom_components.beestat_statistics.date import BeestatFilterChangedDate
from custom_components.beestat_statistics.entry_options import async_mark_filter_changed
from custom_components.beestat_statistics.statistics_builder import (
    detailed_runtime_statistic_ids,
)
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio


async def test_timestamped_action_persists_completion_time_and_safe_replay(
    hass: HomeAssistant, freezer: Any
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    changed_at = "2026-07-05T21:48:12.345678+00:00"
    request = {
        "config_entry_id": entry.entry_id,
        "thermostat_id": 1,
        "changed_at": changed_at,
        "expected_changed_at": None,
        "expected_changed_date": None,
        "expected_request_id": None,
        "request_id": "qualified-completion-1",
    }
    with (
        patch.object(
            coordinator,
            "async_refresh_runtime",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ),
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock(return_value=0)
        ) as dismiss,
        patch.object(
            coordinator, "async_schedule_filter_boundary_reconcile", new=Mock()
        ),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert result["status"] == "recorded"
        assert result["changed_at"] == changed_at
        assert result["changed_date"] == "2026-07-05"
        assert result["boundary_status"] == "pending_data"
        persisted = entry.options["thermostats"][0]
        assert persisted["filter_changed_at"] == changed_at
        assert persisted["filter_change_event"]["source"] == "service"
        assert persisted["filter_change_event"]["recorded_at"] == now.isoformat()
        replay = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert replay["status"] == "already_recorded"
        freezer.move_to(now + timedelta(days=40))
        old_replay = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert old_replay["status"] == "already_recorded"
        freezer.move_to(now)
        dismiss.assert_awaited_once()
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RECORD_FILTER_CHANGE,
                {
                    **request,
                    "request_id": "stale-completion",
                    "changed_at": now.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        assert raised.value.translation_key == "filter_change_boundary_conflict"
        assert entry.options["thermostats"][0] == persisted
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    "changes",
    [
        {"thermostat_id": 999},
        {"changed_at": "2026-07-07T00:00:00+00:00"},
        {"changed_at": "2026-06-01T00:00:00+00:00"},
        {"expected_changed_date": "2026-07-01"},
    ],
)
async def test_timestamped_action_rejects_invalid_or_stale_request_without_write(
    hass: HomeAssistant, freezer: Any, changes: dict[str, Any]
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            {
                "config_entry_id": entry.entry_id,
                "thermostat_id": 1,
                "changed_at": "2026-07-05T18:00:00+00:00",
                "expected_changed_at": None,
                "expected_changed_date": None,
                "expected_request_id": None,
                "request_id": "completion",
                **changes,
            },
            blocking=True,
            return_response=True,
        )
    assert entry.options == {}
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
async def test_bounded_rebuild_updates_real_recorder_tail_and_next_window_seed(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An old corrected day updates subsequent sums without widening measurements."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    start_day = date(2026, 6, 21)
    rows = [
        {
            "thermostat_id": 1,
            "date": (start_day + timedelta(days=index)).isoformat(),
            "count": 288,
            "sum_fan": 3600,
            "avg_indoor_humidity": 40.0,
        }
        for index in range(40)
    ]

    def rebuild_data():
        coordinator.data = coordinator._build_runtime_data(
            rows,
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

    async def refresh(**_kwargs):
        rebuild_data()
        return coordinator.data

    async def summary_window(start, end):
        return [row for row in rows if start <= row["date"] <= end]

    rebuild_data()
    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    client.async_read_runtime_thermostat_summary = AsyncMock(side_effect=summary_window)
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    fan_id = "beestat:zone_a_fan_runtime_hours"
    humidity_id = "beestat:zone_a_indoor_humidity"

    async def read():
        await async_wait_recording_done(hass)
        return await get_instance(hass).async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                datetime(2026, 6, 20, tzinfo=UTC),
                None,
                {fan_id, humidity_id},
                "hour",
                None,
                {"sum", "state", "mean"},
            )
        )

    with patch.object(coordinator, "async_refresh_runtime", new=refresh):
        await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
        initial = await read()
        assert initial[fan_id][-1]["sum"] == 40
        # Correct the second day's runtime and all humidity source values. Only
        # the selected humidity day may change, but every later sum must change.
        rows[1]["sum_fan"] = 10800
        for row in rows:
            row["avg_indoor_humidity"] = 45.0
        changed_day = start_day + timedelta(days=1)
        for _replay in range(2):
            await importer.async_import_statistics(
                skip_sync=True,
                force_full_summary=True,
                rebuild_start=changed_day,
                rebuild_end=changed_day,
            )
            corrected = await read()
            assert len(corrected[fan_id]) == 40
            assert corrected[fan_id][0]["sum"] == 1
            assert corrected[fan_id][1]["sum"] == 4
            assert corrected[fan_id][-1]["sum"] == 42
            assert corrected[humidity_id][1]["mean"] == 45
            assert corrected[humidity_id][-1]["mean"] == 40
        rows.append(
            {
                "thermostat_id": 1,
                "date": "2026-07-31",
                "count": 288,
                "sum_fan": 3600,
                "avg_indoor_humidity": 45.0,
            }
        )
        result = await importer.async_import_statistics(skip_sync=True)
        final = await read()
        assert result.summary_mode == "windowed"
        assert final[fan_id][-1]["sum"] == 43
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize("replace_importer", [False, True])
async def test_queued_rebuild_is_visible_before_next_import_seed_read(
    hass: HomeAssistant, freezer: Any, replace_importer: bool
) -> None:
    """A busy Recorder cannot let the next importer overwrite a corrected tail."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    recorder = get_instance(hass)
    start_day = date(2026, 6, 21)
    rows = [
        {
            "thermostat_id": 1,
            "date": (start_day + timedelta(days=index)).isoformat(),
            "count": 288,
            "sum_fan": 3600,
        }
        for index in range(40)
    ]

    async def refresh(**_kwargs):
        coordinator.data = coordinator._build_runtime_data(
            [dict(row) for row in rows],
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
        return coordinator.data

    async def summary_window(start, end):
        return [row for row in rows if start <= row["date"] <= end]

    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    client.async_read_runtime_thermostat_summary = AsyncMock(side_effect=summary_window)
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    fan_id = "beestat:zone_a_fan_runtime_hours"
    paused = asyncio.Event()
    release = threading.Event()
    followup_started = False
    queue_task = recorder.queue_task
    prepare_import = importer._async_prepare_import

    class GateRecorderTask(RecorderTask):
        """Pause the native Recorder queue while leaving its read executor usable."""

        def run(self, instance):
            instance.hass.loop.call_soon_threadsafe(paused.set)
            if not release.wait(timeout=10):
                raise TimeoutError("Recorder test gate was not released")

    async def prepare_and_pause(*args, **kwargs):
        prepared = await prepare_import(*args, **kwargs)
        queue_task(GateRecorderTask())
        await asyncio.wait_for(paused.wait(), timeout=5)
        return prepared

    def queue_and_release(task):
        queue_task(task)
        # Release at the next import's queue boundary. A synchronized reader
        # waits behind the rebuild; an unfenced reader has already captured its
        # stale seed by the time it queues another import. Both paths finish,
        # and the native row readback distinguishes them without timing sleeps.
        if followup_started and isinstance(
            task, ImportStatisticsTask | SynchronizeTask
        ):
            release.set()

    with patch.object(coordinator, "async_refresh_runtime", new=refresh):
        await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
        await async_wait_recording_done(hass)
        rows[1]["sum_fan"] = 10800
        try:
            with (
                patch.object(importer, "_async_prepare_import", new=prepare_and_pause),
                patch.object(recorder, "queue_task", side_effect=queue_and_release),
            ):
                await importer.async_import_statistics(
                    skip_sync=True,
                    force_full_summary=True,
                    rebuild_start=start_day + timedelta(days=1),
                    rebuild_end=start_day + timedelta(days=1),
                )
                if replace_importer:
                    importer._async_unload()
                    next_importer = BeestatStatisticsImporter(
                        hass, client, coordinator, point_lookback_days=1
                    )
                else:
                    next_importer = importer
                rows.append(
                    {
                        "thermostat_id": 1,
                        "date": "2026-07-31",
                        "count": 288,
                        "sum_fan": 3600,
                    }
                )
                followup_started = True
                # Only the old-day rebuild needs a gate after preparation.
                with patch.object(
                    importer, "_async_prepare_import", new=prepare_import
                ):
                    result = await next_importer.async_import_statistics(skip_sync=True)
                assert result.summary_mode == "windowed"
        finally:
            release.set()
        await async_wait_recording_done(hass)
        statistics = await recorder.async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                datetime(2026, 6, 20, tzinfo=UTC),
                None,
                {fan_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        assert len(statistics[fan_id]) == 41
        assert statistics[fan_id][1]["sum"] == 4
        assert statistics[fan_id][-1]["sum"] == 43
        assert statistics[fan_id][-1]["state"] == 43
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize("window_change", ["stale_recorder", "source_correction"])
async def test_summary_window_reconciles_existing_and_new_detailed_stages(
    hass: HomeAssistant, freezer: Any, window_change: str
) -> None:
    """A stage absent from status rows must not restart at an unseeded window."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    start_day = date(2026, 6, 21)
    rows = [
        {
            "thermostat_id": 1,
            "date": (start_day + timedelta(days=index)).isoformat(),
            "count": 288,
            "sum_fan": 3600,
            "sum_compressor_cool_2": 3600 if window_change == "stale_recorder" else 0,
        }
        for index in range(40)
    ]
    use_recent_cache = False

    async def refresh(**_kwargs):
        cached_rows = rows[-30:] if use_recent_cache else rows
        coordinator.data = coordinator._build_runtime_data(
            [dict(row) for row in cached_rows],
            [{"id": 1, "name": "Zone A"}],
            [],
            now,
            now,
            not use_recent_cache,
            None,
            None,
            evaluated_at=now,
            fetched_at=now,
        )
        return coordinator.data

    async def summary_window(start, end):
        if window_change == "source_correction":
            # This correction arrives after the status snapshot and seed lookup.
            rows[-2]["sum_compressor_cool_2"] = 7200
        return [row for row in rows if start <= row["date"] <= end]

    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    client.async_read_runtime_thermostat_summary = AsyncMock(side_effect=summary_window)
    client.async_read_id = AsyncMock(side_effect=lambda _resource: list(rows))
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    stage_id = "beestat:zone_a_cool_stage_2_runtime_hours"

    async def read_stage():
        await async_wait_recording_done(hass)
        statistics = await get_instance(hass).async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                datetime(2026, 6, 20, tzinfo=UTC),
                None,
                {stage_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        return statistics.get(stage_id, [])

    with patch.object(coordinator, "async_refresh_runtime", new=refresh):
        await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
        initial = await read_stage()
        if window_change == "stale_recorder":
            assert initial[-1]["sum"] == 40
        else:
            assert initial == []
        if window_change == "stale_recorder":
            # Recorder stops at July 30, but the latest 30 status days have no stage 2.
            rows.extend(
                {
                    "thermostat_id": 1,
                    "date": (start_day + timedelta(days=index)).isoformat(),
                    "count": 288,
                    "sum_fan": 3600,
                }
                for index in range(40, 80)
            )
            now += timedelta(days=40)
            freezer.move_to(now)
        use_recent_cache = True
        result = await importer.async_import_statistics(skip_sync=True)
        final = await read_stage()
        if window_change == "stale_recorder":
            assert result.summary_mode == "windowed"
            assert result.summary_fallback_reason is None
            assert result.cumulative_seed_count == 6
            client.async_read_id.assert_not_awaited()
        else:
            assert result.summary_mode == "full"
            assert result.summary_fallback_reason == "missing_prior_recorder_seed"
            assert result.cumulative_seed_count == 0
            client.async_read_id.assert_awaited_once_with("runtime_thermostat_summary")
        assert final[-1]["sum"] == (40 if window_change == "stale_recorder" else 2)
        assert len(final) == len(rows)
        client.async_read_runtime_thermostat_summary.assert_awaited_once_with(
            "2026-07-23", rows[-1]["date"]
        )
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("force_full_summary", "prior_hours", "corrected_value"),
    [
        pytest.param(True, 0, 0, id="full-all-zero"),
        pytest.param(False, 0, 0, id="window-all-zero"),
        pytest.param(False, 5, 0, id="window-with-prior-seed"),
        pytest.param(True, 0, None, id="full-invalid-stops"),
        pytest.param(False, 5, None, id="window-invalid-stops"),
    ],
)
async def test_detailed_runtime_corrections_update_real_recorder(
    hass: HomeAssistant,
    freezer: Any,
    force_full_summary: bool,
    prior_hours: int,
    corrected_value: int | None,
) -> None:
    """A retained stage corrects zero values with the right seed, but rejects invalids."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    start_day = date(2026, 6, 21)
    rows = [
        {
            "thermostat_id": 1,
            "date": (start_day + timedelta(days=index)).isoformat(),
            "count": 288,
            "sum_fan": 3600,
            "sum_compressor_cool_2": (
                prior_hours * 3600 if index == 0 else 3600 if index == 38 else 0
            ),
        }
        for index in range(40)
    ]

    async def refresh(**kwargs):
        full = not kwargs["summary_window"]
        coordinator.data = coordinator._build_runtime_data(
            [dict(row) for row in (rows if full else rows[-30:])],
            [{"id": 1, "name": "Zone A"}],
            [],
            now,
            now,
            full,
            None,
            None,
            evaluated_at=now,
            fetched_at=now,
        )
        return coordinator.data

    async def summary_window(start, end):
        return [row for row in rows if start <= row["date"] <= end]

    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    client.async_read_runtime_thermostat_summary = AsyncMock(side_effect=summary_window)
    client.async_read_id = AsyncMock(side_effect=lambda _resource: list(rows))
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    stage_id = "beestat:zone_a_cool_stage_2_runtime_hours"

    async def read_stage():
        await async_wait_recording_done(hass)
        statistics = await get_instance(hass).async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                datetime(2026, 6, 20, tzinfo=UTC),
                None,
                {stage_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        return statistics[stage_id]

    with patch.object(coordinator, "async_refresh_runtime", new=refresh):
        await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
        initial = await read_stage()
        assert initial[-1]["sum"] == prior_hours + 1
        rows[38]["sum_compressor_cool_2"] = corrected_value
        for _replay in range(2):
            result = await importer.async_import_statistics(
                skip_sync=True, force_full_summary=force_full_summary
            )
            final = await read_stage()
            assert result.summary_mode == ("full" if force_full_summary else "windowed")
            assert result.cumulative_seed_count == (0 if force_full_summary else 6)
            expected = prior_hours + (corrected_value is None)
            assert len(final) == 40
            assert [row["sum"] for row in final[-2:]] == [expected, expected]
            assert [row["state"] for row in final[-2:]] == [expected, expected]
        metadata = await get_instance(hass).async_add_executor_job(
            partial(
                get_metadata,
                hass,
                statistic_ids=set(
                    detailed_runtime_statistic_ids(coordinator.data.config)
                ),
            )
        )
        assert set(metadata) == {stage_id}
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
async def test_all_zero_source_does_not_create_unobserved_detailed_recorder_series(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An empty Recorder inventory does not invent stage/accessory hardware."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    coordinator.data = coordinator._build_runtime_data(
        [{"thermostat_id": 1, "date": "2026-07-30", "sum_compressor_cool_2": 0}],
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
    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    with patch.object(
        coordinator, "async_refresh_runtime", return_value=coordinator.data
    ):
        result = await importer.async_import_statistics(
            skip_sync=True, force_full_summary=True
        )
        assert result.imported_series == 5
        await async_wait_recording_done(hass)
        metadata = await get_instance(hass).async_add_executor_job(
            partial(
                get_metadata,
                hass,
                statistic_ids=set(
                    detailed_runtime_statistic_ids(coordinator.data.config)
                ),
            )
        )
        assert metadata == {}
    await entry._async_process_on_unload(hass)


async def test_repair_rejects_stale_projection_after_new_replacement_is_saved(
    hass: HomeAssistant, freezer: Any
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    hass.config_entries.async_update_entry(
        entry, options={"thermostats": [{"id": 1, "filter_changed_date": "2026-07-04"}]}
    )
    coordinator.async_rebuild_runtime_from_cached_rows()
    assert coordinator.data.config.thermostats[0].filter_changed_date == date(
        2026, 7, 4
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    with (
        patch.object(
            coordinator,
            "async_rebuild_runtime_from_cached_rows",
            side_effect=RuntimeError("cached failure"),
        ),
        patch.object(
            coordinator,
            "async_refresh_runtime",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ),
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock(return_value=0)
        ),
        patch.object(
            coordinator, "async_schedule_filter_boundary_reconcile", new=Mock()
        ),
    ):
        await async_mark_filter_changed(
            coordinator, 1, datetime(2026, 7, 5, 18, tzinfo=UTC)
        )
        saved = entry.options
        assert coordinator.data.config.thermostats[0].filter_changed_date == date(
            2026, 7, 4
        )
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
                {
                    "config_entry_id": entry.entry_id,
                    "thermostat_id": 1,
                    "changed_at": "2026-07-04T18:00:00+00:00",
                },
                blocking=True,
            )
        assert raised.value.translation_key == "filter_change_boundary_date_mismatch"
        assert entry.options is saved
    await entry._async_process_on_unload(hass)


async def test_native_date_correction_preserves_upstream_filter_alerts(
    hass: HomeAssistant, freezer: Any
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    prior_changed_at = "2026-07-05T18:00:00+00:00"
    hass.config_entries.async_update_entry(
        entry,
        options={
            "thermostats": [
                {
                    "id": 1,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": prior_changed_at,
                    "filter_change_day_runtime_baseline_seconds": 3600,
                }
            ],
        },
    )
    coordinator.async_rebuild_runtime_from_cached_rows()
    entity = BeestatFilterChangedDate(
        coordinator, coordinator.data.config.thermostats[0]
    )
    with (
        patch.object(
            coordinator,
            "async_refresh_runtime",
            new=AsyncMock(
                side_effect=lambda **_kwargs: (
                    coordinator.async_rebuild_runtime_from_cached_rows()
                )
            ),
        ) as refresh,
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock(return_value=1)
        ) as dismiss,
    ):
        await entity.async_set_value(date(2026, 7, 4))
        saved = entry.options["thermostats"][0]
        assert saved["filter_changed_date"] == "2026-07-04"
        assert "filter_changed_at" not in saved
        assert "filter_change_day_runtime_baseline_seconds" not in saved
        assert saved["filter_change_event"]["action"] == "correction"
        assert saved["filter_change_event"]["source"] == "date"
        assert saved["filter_change_event"]["prior_changed_at"] == prior_changed_at
        assert entity.native_value == date(2026, 7, 4)
        refresh.assert_awaited_once_with(skip_sync=True)
        dismiss.assert_not_awaited()
    await entry._async_process_on_unload(hass)
