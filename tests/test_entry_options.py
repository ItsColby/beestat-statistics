"""Tests for config-entry option mutation helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_entry_options_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"{name}.py"
    )
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

    def test_filter_change_timestamp_accepts_unique_local_and_explicit_folds(
        self,
    ) -> None:
        local_tz = ZoneInfo("America/New_York")

        self.assertEqual(
            self.entry_options.resolve_filter_change_timestamp(
                datetime(2026, 7, 5, 17, 48),  # noqa: DTZ001 - local wall time
                local_tz,
            ),
            datetime(2026, 7, 5, 21, 48, tzinfo=UTC),
        )
        self.assertEqual(
            self.entry_options.resolve_filter_change_timestamp(
                datetime.fromisoformat("2026-11-01T01:30:00-04:00"),
                local_tz,
            ),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )
        self.assertEqual(
            self.entry_options.resolve_filter_change_timestamp(
                datetime.fromisoformat("2026-11-01T01:30:00-05:00"),
                local_tz,
            ),
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        )

    def test_filter_change_timestamp_rejects_ambiguous_local_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous or does not exist"):
            self.entry_options.resolve_filter_change_timestamp(
                datetime(2026, 11, 1, 1, 30),  # noqa: DTZ001 - ambiguous wall time
                ZoneInfo("America/New_York"),
            )

    def test_filter_change_timestamp_rejects_nonexistent_local_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous or does not exist"):
            self.entry_options.resolve_filter_change_timestamp(
                datetime(2026, 3, 8, 2, 30),  # noqa: DTZ001 - nonexistent wall time
                ZoneInfo("America/New_York"),
            )

    async def test_set_filter_changed_date_saves_correction_without_dismissing_alerts(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(dismissed=1)

        await self.entry_options.async_set_filter_changed_date(
            coordinator,
            1001,
            date(2026, 7, 5),
        )

        self.assertEqual(
            _boundary_options(coordinator),
            [{"id": 1001, "filter_changed_date": "2026-07-05"}],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [])
        self.assertEqual(coordinator.refresh_skip_sync_values, [True])
        self.assertEqual(coordinator.rebuild_count, 0)

    async def test_replacement_refreshes_when_dismiss_fails(self) -> None:
        api = sys.modules[f"{PACKAGE}.api"]
        coordinator = _FakeCoordinator(dismiss_error=api.BeestatApiError("failed"))

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime(2026, 7, 5, 21, 48, tzinfo=UTC),
        )

        self.assertEqual(
            _boundary_options(coordinator),
            [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-05T21:48:00+00:00",
                }
            ],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.refresh_skip_sync_values, [False])
        self.assertEqual(coordinator.rebuild_count, 1)

    async def test_replacement_survives_unexpected_dismiss_error(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(dismiss_error=RuntimeError("unexpected"))

        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime(2026, 7, 5, 21, 48, tzinfo=UTC),
        )

        self.assertEqual(
            _boundary_options(coordinator),
            [
                {
                    "id": 1001,
                    "filter_changed_date": "2026-07-05",
                    "filter_changed_at": "2026-07-05T21:48:00+00:00",
                }
            ],
        )
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])
        self.assertEqual(coordinator.refresh_skip_sync_values, [False])
        self.assertEqual(coordinator.rebuild_count, 1)

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
            _boundary_options(coordinator),
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
            _boundary_options(coordinator),
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
            _boundary_options(coordinator),
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
            _boundary_options(coordinator),
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

    async def test_failed_refresh_does_not_overwrite_newer_options(self) -> None:
        def update_options(coordinator: _FakeCoordinator) -> None:
            coordinator.config_entry.options = {
                **coordinator.config_entry.options,
                "concurrent_option": "preserved",
            }

        coordinator = _FakeCoordinator(
            refresh_error=RuntimeError("refresh failed"),
            during_refresh=update_options,
        )

        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            await self.entry_options.async_set_filter_changed_date(
                coordinator,
                1001,
                date(2026, 7, 5),
            )

        self.assertEqual(
            "preserved",
            coordinator.config_entry.options["concurrent_option"],
        )
        self.assertEqual(
            "2026-07-05",
            coordinator.config_entry.options["thermostats"][0]["filter_changed_date"],
        )

    async def test_replacement_pending_result_survives_refresh_error(self):
        coordinator = _FakeCoordinator(refresh_error=RuntimeError("offline"))
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        response = await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            changed_at,
            source="service",
            request_id="completion-1",
            expected_boundary=(None, None, None),
        )
        self.assertEqual(response["status"], "recorded")
        self.assertEqual(response["changed_at"], changed_at.isoformat())
        self.assertEqual(response["boundary_status"], "pending_data")
        event = coordinator.config_entry.options["thermostats"][0][
            "filter_change_event"
        ]
        self.assertEqual(event["action"], "replacement")
        self.assertEqual(event["source"], "service")
        self.assertEqual(event["request_id"], "completion-1")
        self.assertIsNone(event["prior_changed_at"])

    async def test_replay_uses_persisted_receipt_without_refresh_or_dismiss(self):
        coordinator = _FakeCoordinator()
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            changed_at,
            source="service",
            request_id="completion-1",
            expected_boundary=(None, None, None),
        )
        restarted = _FakeCoordinator()
        restarted.config_entry.options = coordinator.config_entry.options
        response = await self.entry_options.async_mark_filter_changed(
            restarted,
            1001,
            changed_at,
            source="service",
            request_id="completion-1",
            expected_boundary=(None, None, None),
        )
        self.assertEqual(response["status"], "already_recorded")
        self.assertEqual(restarted.refresh_skip_sync_values, [])
        self.assertEqual(restarted.dismissed_thermostat_ids, [])

    async def test_stale_prior_and_reused_request_cannot_overwrite_new_cycle(self):
        coordinator = _FakeCoordinator()
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            changed_at,
            source="service",
            request_id="completion-1",
            expected_boundary=(None, None, None),
        )
        saved = coordinator.config_entry.options
        for request_id, reason in (
            ("completion-1", "request_id_reused"),
            ("completion-2", "filter_change_boundary_conflict"),
        ):
            with self.assertRaisesRegex(
                self.entry_options.FilterChangeConflictError, reason
            ):
                await self.entry_options.async_mark_filter_changed(
                    coordinator,
                    1001,
                    changed_at.replace(hour=22),
                    source="service",
                    request_id=request_id,
                    expected_boundary=(None, None, None),
                )
        self.assertIs(coordinator.config_entry.options, saved)
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])

    async def test_matching_prior_allows_second_same_day_replacement(self):
        coordinator = _FakeCoordinator()
        first = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        second = first.replace(hour=22)
        await self.entry_options.async_mark_filter_changed(coordinator, 1001, first)
        first_event = coordinator.config_entry.options["thermostats"][0][
            "filter_change_event"
        ]
        result = await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            second,
            source="service",
            request_id="second",
            expected_boundary=(first, date(2026, 7, 5), first_event["request_id"]),
        )
        event = coordinator.config_entry.options["thermostats"][0][
            "filter_change_event"
        ]
        self.assertEqual(first_event["source"], "button")
        self.assertNotEqual(first_event["request_id"], event["request_id"])
        self.assertEqual(event["prior_changed_at"], first.isoformat())
        self.assertEqual(result["status"], "recorded")
        with self.assertRaisesRegex(
            self.entry_options.FilterChangeConflictError, "not_after_prior"
        ):
            await self.entry_options.async_mark_filter_changed(
                coordinator,
                1001,
                first,
                source="service",
                request_id="older",
                expected_boundary=(second, date(2026, 7, 5), event["request_id"]),
            )

    async def test_newer_action_during_refresh_is_not_our_success(self):
        def clear_boundary(coordinator):
            coordinator.config_entry.options = {"thermostats": [{"id": 1001}]}

        coordinator = _FakeCoordinator(during_refresh=clear_boundary)
        response = await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            datetime(2026, 7, 5, 21, 48, tzinfo=UTC),
            source="service",
            request_id="completion-1",
            expected_boundary=(None, None, None),
        )
        self.assertEqual(response["status"], "superseded")
        self.assertIsNone(response["changed_at"])
        self.assertIsNone(response["changed_date"])

    async def test_manual_date_and_historical_repair_are_corrections(self):
        coordinator = _FakeCoordinator()
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        await self.entry_options.async_mark_filter_changed(
            coordinator, 1001, changed_at
        )
        await self.entry_options.async_set_filter_changed_date(
            coordinator, 1001, changed_at.date()
        )
        row = coordinator.config_entry.options["thermostats"][0]
        self.assertEqual(row["filter_change_event"]["action"], "correction")
        self.assertEqual(row["filter_change_event"]["source"], "date")
        self.assertIsNone(row["filter_change_event"]["changed_at"])
        await self.entry_options.async_mark_filter_changed(
            coordinator, 1001, changed_at, source="repair", dismiss_alerts=False
        )
        event = coordinator.config_entry.options["thermostats"][0][
            "filter_change_event"
        ]
        self.assertEqual(event["action"], "correction")
        self.assertEqual(event["source"], "repair")

    def test_event_parser_rejects_incomplete_or_contradictory_provenance(self):
        parse = self.entry_options.parse_filter_change_event
        base = {
            "schema_version": 1,
            "action": "replacement",
            "source": "service",
            "request_id": "test-request",
            "prior_request_id": None,
            "prior_changed_at": None,
            "prior_changed_date": None,
            "changed_at": "2026-07-05T21:48:00+00:00",
            "changed_date": "2026-07-05",
            "recorded_at": "2026-07-05T22:00:00+00:00",
        }
        self.assertEqual(parse(base).as_dict(), base)
        for updates in (
            {"source": "repair"},
            {"request_id": "x" * 129},
            {"recorded_at": "unknown"},
            {"schema_version": 2},
            {"changed_at": None},
            {"changed_date": None},
        ):
            self.assertIsNone(parse({**base, **updates}))

    async def test_correction_back_to_prior_date_cannot_authorize_old_replay(self):
        coordinator = _FakeCoordinator()
        coordinator.config_entry.options = {
            "thermostats": [{"id": 1001, "filter_changed_date": "2026-07-04"}]
        }
        original_guard = self.entry_options.saved_filter_boundary(coordinator, 1001)
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        await self.entry_options.async_mark_filter_changed(
            coordinator,
            1001,
            changed_at,
            source="service",
            request_id="original",
            expected_boundary=original_guard,
        )
        await self.entry_options.async_set_filter_changed_date(
            coordinator, 1001, date(2026, 7, 4)
        )
        current = coordinator.config_entry.options
        event = current["thermostats"][0]["filter_change_event"]
        self.assertEqual(event["prior_request_id"], "original")
        with self.assertRaisesRegex(
            self.entry_options.FilterChangeConflictError, "boundary_conflict"
        ):
            await self.entry_options.async_mark_filter_changed(
                coordinator,
                1001,
                changed_at,
                source="service",
                request_id="original",
                expected_boundary=original_guard,
            )
        self.assertIs(coordinator.config_entry.options, current)
        self.assertEqual(coordinator.dismissed_thermostat_ids, [1001])

    async def test_closed_runtime_rejects_date_replacement_repair_and_replay(self):
        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        for action in ("date", "button", "service", "repair", "replay"):
            with self.subTest(action=action):
                coordinator = _FakeCoordinator()
                if action == "replay":
                    await self.entry_options.async_mark_filter_changed(
                        coordinator,
                        1001,
                        changed_at,
                        source="service",
                        request_id="saved",
                    )
                saved = coordinator.config_entry.options
                calls = (
                    list(coordinator.refresh_skip_sync_values),
                    list(coordinator.dismissed_thermostat_ids),
                    coordinator.rebuild_count,
                )
                coordinator.is_closed = True
                with self.assertRaises(asyncio.CancelledError):
                    if action == "date":
                        await self.entry_options.async_set_filter_changed_date(
                            coordinator, 1001, changed_at.date()
                        )
                    else:
                        await self.entry_options.async_mark_filter_changed(
                            coordinator,
                            1001,
                            changed_at,
                            source="service" if action == "replay" else action,
                            request_id="saved" if action == "replay" else "new",
                            dismiss_alerts=action != "repair",
                        )
                self.assertIs(coordinator.config_entry.options, saved)
                self.assertEqual(
                    calls,
                    (
                        coordinator.refresh_skip_sync_values,
                        coordinator.dismissed_thermostat_ids,
                        coordinator.rebuild_count,
                    ),
                )

    async def test_accepted_boundary_survives_unload_during_followup(self):
        def close(coordinator):
            coordinator.is_closed = True
            raise asyncio.CancelledError

        changed_at = datetime(2026, 7, 5, 21, 48, tzinfo=UTC)
        for action in ("date", "replacement"):
            with self.subTest(action=action):
                coordinator = _FakeCoordinator(during_refresh=close)
                with self.assertRaises(asyncio.CancelledError):
                    if action == "date":
                        await self.entry_options.async_set_filter_changed_date(
                            coordinator, 1001, changed_at.date()
                        )
                    else:
                        await self.entry_options.async_mark_filter_changed(
                            coordinator, 1001, changed_at
                        )
                saved = coordinator.config_entry.options["thermostats"][0]
                self.assertEqual(saved["filter_changed_date"], "2026-07-05")
                self.assertEqual(
                    saved["filter_change_event"]["action"],
                    "correction" if action == "date" else "replacement",
                )

    async def test_closed_runtime_cannot_roll_back_accepted_date_after_refresh_error(
        self,
    ):
        def close(coordinator):
            coordinator.is_closed = True

        coordinator = _FakeCoordinator(
            during_refresh=close, refresh_error=RuntimeError("refresh failed")
        )
        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            await self.entry_options.async_set_filter_changed_date(
                coordinator, 1001, date(2026, 7, 5)
            )
        saved = coordinator.config_entry.options["thermostats"][0]
        self.assertEqual(saved["filter_changed_date"], "2026-07-05")
        self.assertEqual(saved["filter_change_event"]["action"], "correction")
        self.assertEqual(coordinator.dismissed_thermostat_ids, [])


def _boundary_options(coordinator):
    """Project stored policy fields; event provenance is asserted separately."""
    return [
        {key: value for key, value in row.items() if key != "filter_change_event"}
        for row in coordinator.config_entry.options["thermostats"]
    ]


class _FakeCoordinator:
    def __init__(
        self,
        *,
        dismissed: int = 0,
        dismiss_error: Exception | None = None,
        refresh_error: Exception | None = None,
        rebuild_error: Exception | None = None,
        during_refresh: Callable[[_FakeCoordinator], None] | None = None,
    ) -> None:
        self.config_entry = types.SimpleNamespace(
            data={}, options={}, entry_id="test-entry"
        )
        self.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_update_entry=self._update_entry)
        )
        self._dismissed = dismissed
        self._dismiss_error = dismiss_error
        self.dismissed_thermostat_ids: list[int] = []
        self.refresh_skip_sync_values: list[bool] = []
        self.refresh_error = refresh_error
        self.rebuild_error = rebuild_error
        self.during_refresh = during_refresh
        self.rebuild_count = 0
        self.scheduled_reconcile_count = 0
        self.local_tz = ZoneInfo("America/New_York")
        self.is_closed = False

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
        if self.during_refresh is not None:
            self.during_refresh(self)
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
