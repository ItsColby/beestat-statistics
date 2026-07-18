"""Tests for config-entry option mutation helpers."""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys
import types
import unittest


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


class _FakeCoordinator:
    def __init__(self, *, dismissed: int = 0, dismiss_error: Exception | None = None) -> None:
        self.config_entry = types.SimpleNamespace(data={}, options={})
        self.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_update_entry=self._update_entry)
        )
        self._dismissed = dismissed
        self._dismiss_error = dismiss_error
        self.dismissed_thermostat_ids: list[int] = []
        self.refresh_skip_sync_values: list[bool] = []

    def _update_entry(self, entry, *, options):
        entry.options = options

    async def async_dismiss_filter_alerts(self, thermostat_id: int) -> int:
        self.dismissed_thermostat_ids.append(thermostat_id)
        if self._dismiss_error is not None:
            raise self._dismiss_error
        return self._dismissed

    async def async_refresh_runtime(self, *, skip_sync: bool) -> None:
        self.refresh_skip_sync_values.append(skip_sync)


if __name__ == "__main__":
    unittest.main()
