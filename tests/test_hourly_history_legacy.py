"""Bounded legacy reader eligibility and calendar proofs with detached snapshots."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_history_legacy_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
module_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_history_legacy", ROOT / "hourly_history_legacy.py"
)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError("Unable to load legacy history")
legacy = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = legacy
module_spec.loader.exec_module(legacy)
Row = sys.modules[f"{PACKAGE}.hourly_import_plan"].HourlyStatisticRow
Snapshot = sys.modules[f"{PACKAGE}.hourly_import_plan"].RecorderSnapshot
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
START = datetime(2026, 9, 10, 4, tzinfo=UTC)
ALIAS = "beestat:fixture_heat_runtime_hours"


class FakeRecorder:
    def __init__(self, rows, metadata):
        self.rows = rows
        self.metadata = metadata
        self.calls = []
        self.error = None

    async def async_snapshot_range(self, alias, start, end):
        self.calls.append((alias, start, end))
        if self.error:
            raise self.error
        return Snapshot(
            tuple(row for row in self.rows if start <= row.start < end),
            self.metadata,
            complete=True,
        )


class LegacyHistoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.descriptor = {
            "kind": "runtime",
            "logical_unit": "h",
            "legacy_statistic_ids": [ALIAS],
            "representation": {"unit_of_measurement": "%", "unit_class": "unitless"},
        }
        self.context = {
            "timezone": "America/New_York",
            "timezone_revision": "tz-1",
            "evaluated_at": (START + 10 * DAY).isoformat(),
        }
        self.selection = {
            "timezone": "America/New_York",
            "timezone_revision": "tz-1",
            "adopted_at": (START + 5 * DAY).isoformat(),
        }
        self.metadata = {
            "statistic_id": ALIAS,
            "source": "beestat",
            "unit_of_measurement": "h",
            "unit_class": "duration",
            "mean_type": 0,
            "has_sum": True,
        }
        self.recorder = FakeRecorder(
            (Row(START - DAY, sum=10, state=10), Row(START, sum=12.5, state=12.5)),
            self.metadata,
        )
        self.check_current = AsyncMock()

    async def read(self, start=START, end=None):
        return await legacy.async_read_legacy_days(
            self.recorder,
            self.descriptor,
            start,
            end or start + DAY,
            context=self.context,
            selection=self.selection,
            check_current=self.check_current,
        )

    async def test_native_predecessor_and_saved_calendar_establish_qualified_daily_amount(
        self,
    ):
        result = await self.read()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["previous_sum"], result[0]["sum"]), (10, 12.5))
        self.assertEqual(result[0]["previous_start"], (START - DAY).isoformat())
        self.assertEqual(result[0]["source_ids"], [ALIAS])
        self.assertIn(
            "historical_sample_completeness_unproven", result[0]["confidence"]
        )
        self.assertEqual(self.recorder.calls, [(ALIAS, START - DAY, START + DAY)])
        self.assertEqual(self.check_current.await_count, 3)

    async def test_missing_actual_predecessor_is_not_replaced_by_nearby_row(self):
        self.recorder.rows = (
            Row(START - 2 * DAY, sum=10, state=10),
            Row(START, sum=12.5, state=12.5),
        )
        self.assertEqual(await self.read(), [])

    async def test_decrease_counter_shape_or_nonfinite_value_is_not_a_valid_increment(
        self,
    ):
        for replacement in (
            Row(START, sum=9, state=9),
            Row(START, sum=12.5, state=4),
            Row(START, sum=float("inf"), state=float("inf")),
            Row(START, sum=12.5, state=12.5, mean=1),
        ):
            with self.subTest(row=replacement):
                self.recorder.rows = (Row(START - DAY, sum=10, state=10), replacement)
                self.assertEqual(await self.read(), [])

    async def test_missing_calendar_changed_calendar_and_ambiguous_alias_do_not_read(
        self,
    ):
        for field, value in (
            ("timezone", None),
            ("timezone", "UTC"),
            ("timezone_revision", "old"),
            ("adopted_at", None),
        ):
            old = self.selection[field]
            self.selection[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(await self.read(), [])
            self.selection[field] = old
        self.descriptor["legacy_statistic_ids"].append("beestat:other_saved_alias")
        self.assertEqual(await self.read(), [])
        self.assertEqual(self.recorder.calls, [])

    async def test_current_and_adoption_days_and_partial_requested_days_are_excluded(
        self,
    ):
        self.selection["adopted_at"] = (START + 3 * HOUR).isoformat()
        self.assertEqual(await self.read(), [])
        self.selection["adopted_at"] = (START + 5 * DAY).isoformat()
        self.context["evaluated_at"] = (START + 23 * HOUR).isoformat()
        self.assertEqual(await self.read(), [])
        self.context["evaluated_at"] = (START + 10 * DAY).isoformat()
        self.assertEqual(await self.read(start=START + HOUR, end=START + DAY), [])
        self.assertEqual(self.recorder.calls, [])

    async def test_native_non_midnight_rows_do_not_prove_requested_calendar(self):
        self.recorder.rows = (
            replace(self.recorder.rows[0], start=START - DAY + HOUR),
            self.recorder.rows[1],
        )
        with self.assertRaisesRegex(ValueError, "history_legacy_calendar_mismatch"):
            await self.read()

    async def test_legacy_unit_and_source_must_match_amount_representation(self):
        for field, value in (
            ("unit_of_measurement", "%"),
            ("unit_class", "unitless"),
            ("source", "other"),
            ("mean_type", 1),
            ("has_sum", False),
        ):
            old = self.metadata[field]
            self.metadata[field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "history_legacy_metadata_conflict"),
            ):
                await self.read()
            self.metadata[field] = old
        self.descriptor.update(kind="degree_days", logical_unit="°F·day")
        self.metadata.update(unit_of_measurement="degree days", unit_class=None)
        self.assertEqual((await self.read())[0]["sum"], 12.5)

    async def test_mean_only_legacy_measurement_retains_null_extrema(self):
        self.descriptor.update(kind="measurement", logical_unit="%")
        self.descriptor["representation"]["unit_class"] = "unitless"
        self.metadata.update(
            unit_of_measurement="%", unit_class="unitless", mean_type=1, has_sum=False
        )
        self.recorder.rows = (Row(START, mean=45),)
        result = await self.read()
        self.assertEqual(result[0]["method"], "legacy_sample_mean")
        self.assertEqual(result[0]["mean"], 45)
        self.assertIsNone(result[0]["min"])
        self.assertIsNone(result[0]["max"])

    async def test_dst_day_uses_adjacent_local_midnights_and_actual_duration(self):
        for first, count in (
            (datetime(2026, 3, 8, 5, tzinfo=UTC), 23),
            (datetime(2026, 11, 1, 4, tzinfo=UTC), 25),
        ):
            self.recorder.rows = (
                Row(first - DAY, sum=10, state=10),
                Row(first, sum=12.5, state=12.5),
            )
            self.context["evaluated_at"] = (first + 4 * DAY).isoformat()
            self.selection["adopted_at"] = (first + 2 * DAY).isoformat()
            result = await self.read(first, first + count * HOUR)
            self.assertEqual(result[0]["end"], (first + count * HOUR).isoformat())
            self.assertEqual(result[0]["previous_start"], (first - DAY).isoformat())

    async def test_long_reads_are_partitioned_and_recheck_context_after_each_await(
        self,
    ):
        self.selection["adopted_at"] = (START + 50 * DAY).isoformat()
        self.context["evaluated_at"] = (START + 50 * DAY).isoformat()
        self.recorder.rows = tuple(
            Row(START + index * DAY, sum=10 + index, state=10 + index)
            for index in range(-1, 40)
        )
        result = await self.read(START, START + 40 * DAY)
        self.assertEqual(len(result), 40)
        self.assertEqual(len(self.recorder.calls), 3)
        self.assertTrue(
            all(end - start <= 20 * DAY for _, start, end in self.recorder.calls)
        )
        self.assertEqual(self.check_current.await_count, 7)

    async def test_revision_change_or_native_failure_propagates_without_retry_or_fake_data(
        self,
    ):
        self.check_current.side_effect = [None, ValueError("history_context_changed")]
        with self.assertRaisesRegex(ValueError, "history_context_changed"):
            await self.read()
        self.assertEqual(len(self.recorder.calls), 1)
        self.check_current.side_effect = None
        self.recorder.error = RuntimeError("native failure")
        with self.assertRaisesRegex(RuntimeError, "native failure"):
            await self.read()
        self.assertEqual(len(self.recorder.calls), 2)


if __name__ == "__main__":
    unittest.main()
