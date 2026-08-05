"""Tests for config-entry option mutation helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_entry_options_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EntryOptionsTest(unittest.IsolatedAsyncioTestCase):
    """Validate native filter-date option updates."""

    def setUp(self) -> None:
        self._old_modules = {"aiohttp": sys.modules.get("aiohttp")}
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        sys.modules["aiohttp"] = aiohttp
        _load_module("const")
        _load_module("api")
        _load_module("config_payload")
        self.entry_options = _load_module("entry_options")

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    async def test_set_filter_changed_date_saves_local_option_and_dismisses_alerts(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(dismissed=1)

        await self.entry_options.async_set_filter_changed_date(
            coordinator,
            1001,
            date(2026, 7, 5),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [{"id": 1001, "filter_changed_date": "2026-07-05"}],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.refresh_skip_sync_values, [True])
        self.assertEqual(coordinator.rebuild_count, 0)

    async def test_set_filter_changed_date_refreshes_when_dismiss_fails(self) -> None:
        api = sys.modules[f"{PACKAGE}.api"]
        coordinator = _FakeCoordinator(dismiss_error=api.BeestatApiError("failed"))

        await self.entry_options.async_set_filter_changed_date(
            coordinator,
            1001,
            date(2026, 7, 5),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [{"id": 1001, "filter_changed_date": "2026-07-05"}],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.refresh_skip_sync_values, [True])
        self.assertEqual(coordinator.rebuild_count, 0)

    async def test_set_filter_changed_date_survives_unexpected_dismiss_error(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(dismiss_error=RuntimeError("unexpected"))

        await self.entry_options.async_set_filter_changed_date(
            coordinator,
            1001,
            date(2026, 7, 5),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [{"id": 1001, "filter_changed_date": "2026-07-05"}],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.refresh_skip_sync_values, [True])
        self.assertEqual(coordinator.rebuild_count, 0)

    async def test_mark_filter_changed_persists_exact_time_before_cloud_refresh(
        self,
    ) -> None:
        coordinator = _FakeCoordinator()

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-06T01:48:00+00:00"),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-06T01:48:00+00:00",
                }
            ],
        )
        self.assertEqual(coordinator.refresh_skip_sync_values, [False])
        self.assertEqual(coordinator.rebuild_count, 1)
        self.assertEqual(coordinator.scheduled_reconcile_count, 0)

    async def test_mark_filter_changed_again_replaces_same_day_baseline(self) -> None:
        coordinator = _FakeCoordinator()

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-05T17:48:00-04:00"),
        )
        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-05T20:00:00-04:00"),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-06T00:00:00+00:00",
                }
            ],
        )

    async def test_manual_filter_date_clears_click_baseline(self) -> None:
        coordinator = _FakeCoordinator()
        coordinator.config_entry.options = {
            "thermostats": [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-05T21:48:00+00:00",
                    "filter_change_day_runtime_baseline_seconds": 28800,
                    "filter_change_boundary_reconciled_at": (
                        "2026-07-05T22:05:00+00:00"
                    ),
                    "filter_change_boundary_source_data_end": (
                        "2026-07-05T21:45:00+00:00"
                    ),
                }
            ]
        }

        await self.entry_options.async_set_filter_changed_date(
            coordinator,
            1001,
            date(2026, 6, 18),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [{"id": 1001, "filter_changed_date": "2026-06-18"}],
        )
        self.assertEqual(coordinator.refresh_skip_sync_values, [True])
        self.assertEqual(coordinator.rebuild_count, 0)

    async def test_mark_filter_changed_stays_persisted_when_fresh_sync_fails(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(refresh_error=RuntimeError("sync failed"))

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-05T17:48:00-04:00"),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"],
            [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-05T21:48:00+00:00",
                }
            ],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.scheduled_reconcile_count, 1)

    async def test_mark_filter_changed_does_not_require_today_summary(
        self,
    ) -> None:
        coordinator = _FakeCoordinator()

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-05T17:48:00-04:00"),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"][0]["filter_changed_at"],
            "2026-07-05T21:48:00+00:00",
        )
        self.assertEqual(coordinator.rebuild_count, 1)

    async def test_filter_change_stays_persisted_if_cached_rebuild_fails(self) -> None:
        coordinator = _FakeCoordinator(rebuild_error=RuntimeError("rebuild failed"))

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime.fromisoformat("2026-07-05T17:48:00-04:00"),
        )

        self.assertEqual(
            coordinator.config_entry.options["thermostats"][0]["filter_changed_at"],
            "2026-07-05T21:48:00+00:00",
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])

    async def test_manual_filter_date_rolls_back_if_refresh_fails(self) -> None:
        coordinator = _FakeCoordinator(refresh_error=RuntimeError("refresh failed"))

        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            await self.entry_options.async_set_filter_changed_date(
                coordinator,
                1001,
                date(2026, 7, 5),
            )

        self.assertEqual(coordinator.config_entry.options, {})
        self.assertEqual(coordinator.dismissed_thermostat_ids, [])


class _FakeCoordinator:
    def __init__(
        self,
        *,
        dismissed: int = 0,
        dismiss_error: Exception | None = None,
        refresh_error: Exception | None = None,
        rebuild_error: Exception | None = None,
    ) -> None:
        self.config_entry = types.SimpleNamespace(data={}, options={})
        self.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_update_entry=self._update_entry)
        )
        self._dismissed = dismissed
        self._dismiss_error = dismiss_error
        self.dismissed_thermostat_ids: list[int] = []
        self.refresh_skip_sync_values: list[bool] = []
        self.refresh_error = refresh_error
        self.rebuild_error = rebuild_error
        self.rebuild_count = 0
        self.scheduled_reconcile_count = 0
        self.local_tz = ZoneInfo("America/New_York")

    def _update_entry(self, entry, *, options):
        entry.options = options

    async def async_dismiss_filter_alerts(self, thermostat_id: int) -> int:
        self.dismissed_thermostat_ids.append(thermostat_id)
        if self._dismiss_error is not None:
            raise self._dismiss_error
        return self._dismissed

    async def async_refresh_runtime(
        self,
        *,
        skip_sync: bool,
        summary_window: bool = False,
    ) -> None:
        self.refresh_skip_sync_values.append(skip_sync)
        if self.refresh_error is not None:
            raise self.refresh_error

    def async_rebuild_runtime_from_cached_rows(self) -> None:
        self.rebuild_count += 1
        if self.rebuild_error is not None:
            raise self.rebuild_error

    def async_schedule_filter_boundary_reconcile(self) -> None:
        self.scheduled_reconcile_count += 1


if __name__ == "__main__":
    unittest.main()
