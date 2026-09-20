"""Exercise persisted writer transitions with explicit Store/Recorder failures."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_writer_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_import", ROOT / "hourly_import.py"
)
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)
builder = sys.modules[f"{PACKAGE}.hourly_statistics"]
planner = sys.modules[f"{PACKAGE}.hourly_import_plan"]
datetime, UTC = manager.datetime, manager.UTC
HOUR = manager._HOUR
START = datetime(2026, 9, 10, tzinfo=UTC)
ID = "beestat:zone_fan_runtime_hours_hourly_v2"


def source(values=(0.25, 0.5), *, start=START, cumulative=True, statistic_id=ID):
    metadata = {
        "statistic_id": statistic_id,
        "source": "beestat",
        "name": "Fixture",
        "unit_of_measurement": "h" if cumulative else "°F",
        "unit_class": "duration" if cumulative else "temperature",
        "mean_type": 0 if cumulative else 1,
        "has_sum": cumulative,
    }
    hours = tuple(
        builder.HourlyBucket(
            start + i * HOUR,
            None
            if value is None
            else (
                {"increment": value}
                if cumulative
                else {"mean": value, "min": value, "max": value}
            ),
            0 if value is None else 12,
            12 if value is None else 0,
            0,
            0,
            "missing_slots" if value is None else "ready",
        )
        for i, value in enumerate(values)
    )
    return builder.HourlySeries(metadata, hours, 12 * len(values))


def identity(statistic_id=ID):
    return {
        "entry_id": "test-entry",
        "api_base": "https://api.beestat.io/",
        "account_anchors": ["anchor-one"],
        "resources": {
            statistic_id: {
                "thermostat_id": 1,
                "sensor_id": None,
                "quantity": "fan_runtime_hours",
            }
        },
    }


class Store:
    def __init__(self):
        self.value = None
        self.calls = 0
        self.fail = None
        self.gate = None
        self.gate_call = None
        self.gate_entered = asyncio.Event()

    async def async_load(self):
        return deepcopy(self.value)

    async def async_save(self, value):
        self.calls += 1
        if self.gate is not None and self.gate_call in (None, self.calls):
            self.gate_entered.set()
            await self.gate.wait()
        if self.calls == self.fail:
            raise OSError("synthetic disk failure")
        self.value = deepcopy(value)


class Recorder:
    def __init__(self):
        self.metadata = {}
        self.rows = {}
        self.submissions = []
        self.partial = None

    async def async_snapshot(self, statistic_id, start):
        return planner.RecorderSnapshot(
            tuple(
                row
                for instant, row in sorted(self.rows.get(statistic_id, {}).items())
                if instant >= start
            ),
            deepcopy(self.metadata.get(statistic_id)),
            True,
        )

    def submit(self, metadata, rows):
        self.submissions.append(rows)
        statistic_id = metadata["statistic_id"]
        self.metadata[statistic_id] = deepcopy(metadata)
        target = self.rows.setdefault(statistic_id, {})
        for index, row in enumerate(rows):
            if index == self.partial:
                self.partial = None
                raise RuntimeError("synthetic partial queue effect")
            target[row.start] = row


class TestHourlyImport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.recorder = Store(), Recorder()
        self.store_tasks = []
        self.operation_tasks = []
        self.entry = types.SimpleNamespace(entry_id="test-entry", data={})
        self.hass = types.SimpleNamespace(
            data={},
            async_create_task=self.create_store_task,
            config_entries=types.SimpleNamespace(
                async_update_entry=lambda entry, data: setattr(entry, "data", data)
            ),
        )
        self.writer = self.fresh()

    def create_store_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.store_tasks.append(task)
        return task

    def create_operation_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.operation_tasks.append(task)
        return task

    async def settle_gated_tasks(self, tasks, *, timeout, cancel):
        """Retrieve all outcomes, allowing shielded saves time to finish first."""
        if not tasks:
            return []
        if cancel:
            for task in tasks:
                task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            for task in pending:
                task.cancel()
            settled, pending = await asyncio.wait(pending, timeout=timeout)
            done |= settled
        errors = (
            [AssertionError(f"{len(pending)} fixture tasks survived cleanup")]
            if pending
            else []
        )
        errors.extend(
            error
            for task in done
            if not task.cancelled() and (error := task.exception()) is not None
        )
        return errors

    @asynccontextmanager
    async def gated_import(self, *, gate_call, timeout=5):
        """Reach a selected Store save or fail, then settle every owned task."""
        self.store.gate = asyncio.Event()
        self.store.gate_entered.clear()
        self.store.gate_call = gate_call
        operation_start, store_start = len(self.operation_tasks), len(self.store_tasks)
        importing = self.create_operation_task(
            self.writer.async_import((source(),), identity())
        )
        entered = self.create_operation_task(self.store.gate_entered.wait())
        primary_error = None
        try:
            done, _ = await asyncio.wait(
                (importing, entered),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if importing in done:
                importing.result()
                self.fail("Import completed before the selected Store gate")
            if entered not in done:
                self.fail("Timed out waiting for the selected Store gate")
            async with asyncio.timeout(timeout):
                yield importing
        except BaseException as err:
            primary_error = err
            raise
        finally:
            self.store.gate.set()
            cleanup_errors = await self.settle_gated_tasks(
                self.operation_tasks[operation_start:], timeout=timeout, cancel=True
            )
            # Import cancellation does not cancel a shielded Store save. Include
            # every save created before importer/recovery operations settled.
            cleanup_errors += await self.settle_gated_tasks(
                self.store_tasks[store_start:], timeout=timeout, cancel=False
            )
            cleanup_errors = [
                error for error in cleanup_errors if error is not primary_error
            ]
            if cleanup_errors:
                if primary_error is None:
                    raise BaseExceptionGroup(
                        "Gated import cleanup failed", cleanup_errors
                    )
                for error in cleanup_errors:
                    primary_error.add_note(f"Gated import cleanup: {error!r}")

    def fresh(self):
        return manager.HourlyImportManager(
            self.hass, self.entry, store=self.store, recorder=self.recorder
        )

    async def adopt(self, item=None):
        item = item or source()
        bound = identity(item.statistic_id)
        preview = await self.writer.async_select(
            (item,),
            bound,
            epoch_start=item.hours[0].start,
            statistic_ids=(item.statistic_id,),
            expected_revision=0,
        )
        self.assertIsNone(self.store.value)
        await self.writer.async_select(
            (item,),
            bound,
            epoch_start=item.hours[0].start,
            statistic_ids=(item.statistic_id,),
            expected_revision=0,
            preview_digest=preview["preview_digest"],
        )
        return preview

    async def pending_extension(self):
        """Leave an extension journaled with a verified native predecessor."""
        initial = source((0.25, 0.5, 0.75))
        await self.adopt(initial)
        await self.writer.async_import((initial,), identity())
        self.recorder.partial = 0
        with self.assertRaises(RuntimeError):
            await self.writer.async_import(
                (source((0.75, 1.0), start=START + 2 * HOUR),), identity()
            )
        self.assertIsNotNone(self.store.value["pending"])

    def assert_coverage_suppressed(self, writer=None):
        coverage = (writer or self.writer).coverage(start=START, end=START + 2 * HOUR)[
            "series"
        ][ID]
        self.assertEqual(0, coverage["complete_observed_hours"])
        self.assertIsNone(coverage["observed_hour_average"])
        self.assertTrue(all(hour["value"] is None for hour in coverage["hours"]))

    async def test_legacy_missing_and_corrupt_store_distinctions(self):
        self.assertEqual("legacy", await self.writer.async_mode())
        self.entry.data[manager.MARKER] = {"version": 1, "token": "lost"}
        with self.assertRaises(manager.HourlyImportError):
            await self.fresh().async_mode()
        self.entry.data.clear()
        self.store.value = {"version": 2}
        with self.assertRaises(manager.HourlyImportError):
            await self.fresh().async_mode()
        self.assertFalse(self.recorder.submissions)

    async def test_explicit_adoption_and_verified_average(self):
        preview = await self.adopt()
        self.assertEqual(
            {
                "policy": "per_quantity",
                "frozen_statistic_ids": [ID.removesuffix("_hourly_v2")],
                "continuing_statistic_ids": [],
            },
            preview["legacy_writes"],
        )
        self.assertFalse(self.recorder.submissions)
        result = await self.writer.async_import((source(),), identity())
        self.assertEqual(2, result["imported_rows"])
        self.assertEqual(0.75, self.recorder.rows[ID][START + HOUR].sum)
        coverage = self.writer.coverage(start=START, end=START + 3 * HOUR)["series"][ID]
        self.assertEqual(2, coverage["complete_observed_hours"])
        self.assertEqual(0.375, coverage["observed_hour_average"])
        self.assertIsNone(coverage["hours"][2]["value"])
        self.assertFalse(coverage["complete"])

    async def test_partition_keeps_voc_legacy_across_reload_disable_and_rename(self):
        voc_id = "beestat:zone_voc_concentration_hourly_v2"
        bound = identity()
        bound["resources"][voc_id] = {
            "thermostat_id": 1,
            "sensor_id": 10,
            "quantity": "voc_concentration",
        }
        unadopted = await self.writer.async_writer_partition(bound)
        self.assertFalse(unadopted.has_hourly)
        self.assertEqual(
            {ID.removesuffix("_hourly_v2"), voc_id.removesuffix("_hourly_v2")},
            unadopted.legacy_statistic_ids,
        )
        args = {"epoch_start": START, "statistic_ids": (ID,), "expected_revision": 0}
        preview = await self.writer.async_select((source(),), bound, **args)
        self.assertEqual(
            [voc_id.removesuffix("_hourly_v2")],
            preview["legacy_writes"]["continuing_statistic_ids"],
        )
        await self.writer.async_select(
            (source(),), bound, **args, preview_digest=preview["preview_digest"]
        )
        saved = deepcopy(self.store.value)
        writer = self.fresh()
        with (
            patch.object(self.recorder, "async_snapshot") as snapshot,
            patch.object(self.store, "async_save") as save,
        ):
            selected = await writer.async_writer_partition(bound)
            self.assertTrue(selected.hourly_ready)
            self.assertEqual({ID}, selected.hourly_statistic_ids)
            self.assertEqual(
                {voc_id.removesuffix("_hourly_v2")}, selected.legacy_statistic_ids
            )
            disabled = deepcopy(bound)
            disabled["resources"].pop(ID)
            partition = await writer.async_writer_partition(disabled)
            self.assertFalse(partition.hourly_statistic_ids)
            self.assertEqual(
                {ID.removesuffix("_hourly_v2")}, partition.frozen_legacy_statistic_ids
            )
            renamed = "beestat:renamed_fan_runtime_hours_hourly_v2"
            bound["resources"][renamed] = bound["resources"].pop(ID)
            partition = await writer.async_writer_partition(bound)
            self.assertEqual({renamed}, partition.hourly_statistic_ids)
            self.assertEqual(
                {ID.removesuffix("_hourly_v2"), renamed.removesuffix("_hourly_v2")},
                partition.frozen_legacy_statistic_ids,
            )
            self.assertEqual(
                {voc_id.removesuffix("_hourly_v2")}, partition.legacy_statistic_ids
            )
            snapshot.assert_not_awaited()
            save.assert_not_awaited()
        self.assertEqual(saved, self.store.value)

    async def test_partition_rejects_rebound_original_alias_and_sensor_parent(self):
        sensor_id = "beestat:zone_temperature_hourly_v2"
        bound = identity(sensor_id)
        bound["resources"][sensor_id] = {
            "thermostat_id": 1,
            "sensor_id": 10,
            "quantity": "temperature",
        }
        item = source((70,), cumulative=False, statistic_id=sensor_id)
        args = {
            "epoch_start": START,
            "statistic_ids": (sensor_id,),
            "expected_revision": 0,
        }
        preview = await self.writer.async_select((item,), bound, **args)
        await self.writer.async_select(
            (item,), bound, **args, preview_digest=preview["preview_digest"]
        )
        for changed in (None, 2):
            with self.subTest(parent=changed):
                rebound = {**bound, "sensor_parents": {10: changed}}
                with self.assertRaises(manager.HourlyImportError):
                    await self.fresh().async_writer_partition(rebound)
        rebound = deepcopy(bound)
        rebound["resources"][sensor_id]["sensor_id"] = 11
        with self.assertRaisesRegex(manager.HourlyImportError, "rebound"):
            await self.fresh().async_writer_partition(rebound)
        renamed = "beestat:renamed_temperature_hourly_v2"
        rebound["resources"][renamed] = bound["resources"][sensor_id]
        with self.assertRaisesRegex(manager.HourlyImportError, "rebound"):
            await self.fresh().async_writer_partition(rebound)

    async def test_partition_preserves_unmapped_unselected_sensor_legacy_behavior(self):
        bound = identity()
        orphan = "beestat:unmapped_temperature_hourly_v2"
        bound["resources"][orphan] = {
            "thermostat_id": None,
            "sensor_id": 10,
            "quantity": "temperature",
        }
        bound["sensor_parents"] = {10: None}
        legacy = await self.writer.async_writer_partition(bound)
        self.assertIn(orphan.removesuffix("_hourly_v2"), legacy.legacy_statistic_ids)
        await self.adopt()
        mixed = await self.fresh().async_writer_partition(bound)
        self.assertEqual(
            {orphan.removesuffix("_hourly_v2")}, mixed.legacy_statistic_ids
        )
        self.assertEqual({ID}, mixed.hourly_statistic_ids)

    async def test_empty_configuration_retains_adopted_freeze_and_identity_checks(self):
        empty = {"entry_id": self.entry.entry_id, "resources": {}}
        legacy = await self.writer.async_writer_partition(empty)
        self.assertFalse(legacy.has_hourly or legacy.legacy_statistic_ids)
        with self.assertRaisesRegex(manager.HourlyImportError, "config-entry"):
            await self.writer.async_writer_partition({**empty, "entry_id": "other"})
        await self.adopt()
        disabled = {**identity(), "resources": {}}
        partition = await self.fresh().async_writer_partition(disabled)
        self.assertTrue(partition.has_hourly)
        self.assertFalse(
            partition.legacy_statistic_ids or partition.hourly_statistic_ids
        )
        self.assertEqual(
            {ID.removesuffix("_hourly_v2")}, partition.frozen_legacy_statistic_ids
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "account"):
            await self.fresh().async_writer_partition(empty)
        for marker in (None, {"version": 1, "token": "other"}):
            with self.subTest(marker=marker):
                self.entry.data[manager.MARKER] = marker
                with self.assertRaisesRegex(manager.HourlyImportError, "marker"):
                    await self.fresh().async_writer_partition(disabled)

    async def test_partition_rejects_marker_missing_mismatch_and_corrupt_saved_identity(
        self,
    ):
        await self.adopt()
        original_marker = deepcopy(self.entry.data[manager.MARKER])
        original_store = deepcopy(self.store.value)
        for marker in (None, {"version": 1, "token": "wrong"}):
            with self.subTest(marker=marker):
                self.entry.data[manager.MARKER] = marker
                with self.assertRaisesRegex(manager.HourlyImportError, "marker"):
                    await self.fresh().async_writer_partition(identity())
        self.entry.data[manager.MARKER] = original_marker
        for field, value in (
            ("thermostat_id", True),
            ("thermostat_id", 0),
            ("sensor_id", False),
            ("sensor_id", -1),
            ("quantity", "unrecognized_quantity"),
        ):
            with self.subTest(field=field, value=value):
                self.store.value = deepcopy(original_store)
                self.store.value["series"][ID]["resource"][field] = value
                self.store.value.pop("integrity")
                self.store.value["integrity"] = manager._digest(self.store.value)
                with self.assertRaisesRegex(manager.HourlyImportError, "corrupt"):
                    await self.fresh().async_writer_partition(identity())
        self.assertFalse(self.recorder.submissions)

    async def test_partition_rejects_duplicate_saved_quantity_owners(self):
        await self.adopt()
        duplicate = "beestat:alias_fan_runtime_hours_hourly_v2"
        record = deepcopy(self.store.value["series"][ID])
        record["statistic_id"] = duplicate
        record["metadata"]["statistic_id"] = duplicate
        self.store.value["series"][duplicate] = record
        self.store.value.pop("integrity")
        self.store.value["integrity"] = manager._digest(self.store.value)
        with self.assertRaisesRegex(manager.HourlyImportError, "corrupt"):
            await self.fresh().async_writer_partition(identity())

    async def test_partition_reserves_old_and_interrupted_additional_selection(self):
        await self.adopt()
        temperature_id = "beestat:zone_temperature_hourly_v2"
        voc_id = "beestat:zone_voc_concentration_hourly_v2"
        bound = identity()
        for statistic_id, quantity in (
            (temperature_id, "temperature"),
            (voc_id, "voc_concentration"),
        ):
            bound["resources"][statistic_id] = {
                "thermostat_id": 1,
                "sensor_id": 10,
                "quantity": quantity,
            }
        items = (source(), source((70,), cumulative=False, statistic_id=temperature_id))
        args = {
            "epoch_start": START,
            "statistic_ids": (temperature_id,),
            "expected_revision": self.writer.status()["revision"],
        }
        preview = await self.writer.async_select(items, bound, **args)
        self.assertEqual(
            [voc_id.removesuffix("_hourly_v2")],
            preview["legacy_writes"]["continuing_statistic_ids"],
        )
        self.store.fail = self.store.calls + 2
        with self.assertRaises(OSError):
            await self.writer.async_select(
                items, bound, **args, preview_digest=preview["preview_digest"]
            )
        replacement = self.fresh()
        partition = await replacement.async_writer_partition(bound)
        self.assertEqual("selection_pending", partition.hourly_blocked_reason)
        self.assertEqual({ID, temperature_id}, partition.hourly_statistic_ids)
        self.assertEqual(
            {voc_id.removesuffix("_hourly_v2")}, partition.legacy_statistic_ids
        )
        self.assertEqual(
            {ID.removesuffix("_hourly_v2"), temperature_id.removesuffix("_hourly_v2")},
            partition.frozen_legacy_statistic_ids,
        )
        await replacement.async_select(
            items, bound, **args, preview_digest=preview["preview_digest"]
        )
        recovered = await replacement.async_writer_partition(bound)
        self.assertTrue(recovered.hourly_ready)
        self.assertEqual(partition.hourly_statistic_ids, recovered.hourly_statistic_ids)
        self.assertEqual(partition.legacy_statistic_ids, recovered.legacy_statistic_ids)

    async def test_disabled_quantity_skips_hourly_writer_without_releasing_ownership(
        self,
    ):
        temperature_id = "beestat:zone_temperature_hourly_v2"
        bound = identity()
        bound["resources"][temperature_id] = {
            "thermostat_id": 1,
            "sensor_id": 10,
            "quantity": "temperature",
        }
        items = (
            source(),
            source((70, 71), cumulative=False, statistic_id=temperature_id),
        )
        args = {
            "epoch_start": START,
            "statistic_ids": (ID, temperature_id),
            "expected_revision": 0,
        }
        preview = await self.writer.async_select(items, bound, **args)
        await self.writer.async_select(
            items, bound, **args, preview_digest=preview["preview_digest"]
        )
        await self.writer.async_import(
            items, bound, eligible_resources=bound["resources"]
        )
        retained = deepcopy(self.recorder.rows[temperature_id])
        writer = self.fresh()
        await writer.async_import(
            (source(),), identity(), eligible_resources=identity()["resources"]
        )
        self.assertEqual(retained, self.recorder.rows[temperature_id])
        disabled = await writer.async_writer_partition(identity())
        self.assertIn(
            temperature_id.removesuffix("_hourly_v2"),
            disabled.frozen_legacy_statistic_ids,
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "resource is missing"):
            await writer.async_import(
                (source(),), bound, eligible_resources=bound["resources"]
            )
        corrected = source((70, 72), cumulative=False, statistic_id=temperature_id)
        await writer.async_import(
            (source(), corrected), bound, eligible_resources=bound["resources"]
        )
        self.assertEqual(72, self.recorder.rows[temperature_id][START + HOUR].mean)
        reenabled = await writer.async_writer_partition(bound)
        self.assertEqual({ID, temperature_id}, reenabled.hourly_statistic_ids)
        self.assertFalse(reenabled.legacy_statistic_ids)

    async def test_partition_survives_native_conflict_without_replaying_pending_effect(
        self,
    ):
        await self.pending_extension()
        bound = identity()
        other_id = "beestat:other_fan_runtime_hours_hourly_v2"
        bound["resources"][other_id] = {
            "thermostat_id": 2,
            "sensor_id": None,
            "quantity": "fan_runtime_hours",
        }
        self.recorder.metadata[ID]["unit_of_measurement"] = "s"
        replacement = self.fresh()
        with self.assertRaises(manager.HourlyReconciliationError):
            await replacement.async_reconcile()
        before = deepcopy(self.store.value)
        with patch.object(self.recorder, "async_snapshot") as snapshot:
            partition = await replacement.async_writer_partition(bound)
            self.assertEqual(
                {other_id.removesuffix("_hourly_v2")}, partition.legacy_statistic_ids
            )
            self.assertEqual({ID}, partition.hourly_statistic_ids)
            snapshot.assert_not_awaited()
        self.assertEqual(before, self.store.value)

    async def test_window_rolls_with_exact_saved_seed_and_immutable_id(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        renamed = "beestat:renamed_fan_runtime_hours_hourly_v2"
        item = source((0.5, 0.75), start=START + HOUR, statistic_id=renamed)
        await self.writer.async_import((item,), identity(renamed))
        self.assertNotIn(renamed, self.recorder.rows)
        self.assertEqual(1.5, self.recorder.rows[ID][START + 2 * HOUR].sum)

    async def test_bootstrap_start_tracks_uncheckpointed_cumulative_scope(self):
        other_id = "beestat:other_fan_runtime_hours_hourly_v2"
        measurement_id = "beestat:zone_temperature_hourly_v2"
        items = (
            source(),
            source(statistic_id=other_id),
            source((70, 71), cumulative=False, statistic_id=measurement_id),
        )
        bound = identity()
        bound["resources"].update(
            {
                other_id: {
                    "thermostat_id": 2,
                    "sensor_id": None,
                    "quantity": "fan_runtime_hours",
                },
                measurement_id: {
                    "thermostat_id": 3,
                    "sensor_id": 10,
                    "quantity": "temperature",
                },
            }
        )
        args = {
            "epoch_start": START,
            "statistic_ids": tuple(item.statistic_id for item in items),
            "expected_revision": 0,
        }
        self.assertIsNone(self.writer.bootstrap_start())
        preview = await self.writer.async_select(items, bound, **args)
        await self.writer.async_select(
            items, bound, **args, preview_digest=preview["preview_digest"]
        )
        selection = deepcopy(self.store.value["last_selection"])
        with (
            patch.object(self.store, "async_load") as load,
            patch.object(self.recorder, "async_snapshot") as snapshot,
        ):
            self.assertEqual(START, self.writer.bootstrap_start())
            self.assertEqual(START, self.writer.bootstrap_start(thermostat_id=1))
            self.assertEqual(START, self.writer.bootstrap_start(thermostat_id=2))
            self.assertIsNone(self.writer.bootstrap_start(thermostat_id=3))
            self.assertIsNone(self.writer.bootstrap_start(thermostat_id=4))
            load.assert_not_awaited()
            snapshot.assert_not_awaited()

        await self.writer.async_import(
            (items[0],), {**bound, "selected_thermostat_id": 1}
        )
        self.assertIsNone(self.writer.bootstrap_start(thermostat_id=1))
        self.assertEqual(START, self.writer.bootstrap_start())
        await self.writer.async_import(
            (items[1],), {**bound, "selected_thermostat_id": 2}
        )
        self.assertIsNone(self.writer.bootstrap_start())
        self.assertIsNone(self.store.value["series"][measurement_id]["checkpoint"])
        self.assertEqual(selection, self.store.value["last_selection"])
        self.assertTrue(
            all(
                record["epoch_start"] == START.isoformat()
                for record in self.store.value["series"].values()
            )
        )

    async def test_bootstrap_expansion_preserves_other_series_ordinary_window(self):
        bootstrap_id = "beestat:other_fan_runtime_hours_hourly_v2"
        measurement_id = "beestat:zone_temperature_hourly_v2"
        initial = (
            source(),
            source(statistic_id=bootstrap_id),
            source((70, 71), cumulative=False, statistic_id=measurement_id),
        )
        bound = identity()
        bound["resources"].update(
            {
                bootstrap_id: {
                    "thermostat_id": 2,
                    "sensor_id": None,
                    "quantity": "fan_runtime_hours",
                },
                measurement_id: {
                    "thermostat_id": 1,
                    "sensor_id": 10,
                    "quantity": "temperature",
                },
            }
        )
        args = {
            "epoch_start": START,
            "statistic_ids": tuple(item.statistic_id for item in initial),
            "expected_revision": 0,
        }
        preview = await self.writer.async_select(initial, bound, **args)
        await self.writer.async_select(
            initial, bound, **args, preview_digest=preview["preview_digest"]
        )
        await self.writer.async_import(
            (initial[0], initial[2]), {**bound, "selected_thermostat_id": 1}
        )
        self.assertIsNotNone(self.store.value["series"][ID]["checkpoint"])
        self.assertIsNone(self.store.value["series"][bootstrap_id]["checkpoint"])
        self.assertEqual(START, self.writer.bootstrap_start())
        previous = deepcopy(self.recorder.rows)
        selected = deepcopy(self.writer.status()["series"])
        selection = deepcopy(self.store.value["last_selection"])

        renamed = "beestat:renamed_fan_runtime_hours_hourly_v2"
        refreshed_identity = deepcopy(bound)
        refreshed_identity["resources"][renamed] = refreshed_identity["resources"].pop(
            ID
        )
        renamed_bootstrap = "beestat:renamed_other_fan_runtime_hours_hourly_v2"
        optional_id = "beestat:other_temperature_hourly_v2"
        sensor_id = "beestat:remote_fan_runtime_hours_hourly_v2"
        resources = {
            renamed: bound["resources"][ID],
            renamed_bootstrap: bound["resources"][bootstrap_id],
            measurement_id: bound["resources"][measurement_id],
            optional_id: {
                **bound["resources"][bootstrap_id],
                "quantity": "temperature",
            },
            sensor_id: {
                **bound["resources"][bootstrap_id],
                "sensor_id": 9,
            },
        }
        saved_resources = deepcopy(resources)
        saved_state = deepcopy(self.store.value)
        saved_status = deepcopy(self.writer.status())
        # Offsets are hours from START; result order follows the resources above.
        windows = (
            (-1, 4, 2, (2, 0, 2, 2, 2)),
            (-1, 4, None, (0, 0, 0, -1, -1)),
            (1, 4, None, (1, 1, 1, 1, 1)),
            (-2, -1, None, (-1, -1, -1, -2, -2)),
            (-1, 4, 5, (4, 0, 4, 4, 4)),
            (3, 3, 2, (3, 3, 3, 3, 3)),
        )
        with (
            patch.object(self.store, "async_load") as load,
            patch.object(self.store, "async_save") as save,
            patch.object(self.recorder, "async_snapshot") as snapshot,
            patch.object(self.recorder, "submit") as submit,
        ):
            for start, end, ordinary, expected in windows:
                with self.subTest(start=start, end=end, ordinary_start=ordinary):
                    self.assertEqual(
                        {
                            statistic_id: START + offset * HOUR
                            for statistic_id, offset in zip(
                                resources, expected, strict=True
                            )
                        },
                        self.writer.source_starts(
                            resources,
                            start=START + start * HOUR,
                            end=START + end * HOUR,
                            ordinary_start=(
                                None if ordinary is None else START + ordinary * HOUR
                            ),
                        ),
                    )
            self.assertEqual(
                {renamed_bootstrap: START},
                self.writer.source_starts(
                    {renamed_bootstrap: resources[renamed_bootstrap]},
                    start=START - HOUR,
                    end=START + 4 * HOUR,
                    ordinary_start=START + 2 * HOUR,
                ),
            )
            load.assert_not_awaited()
            save.assert_not_awaited()
            snapshot.assert_not_awaited()
            submit.assert_not_called()
        self.assertEqual(saved_resources, resources)
        self.assertEqual(saved_state, self.store.value)
        self.assertEqual(saved_status, self.writer.status())

        expanded = (
            source((None, 9, 0.75, 1), statistic_id=renamed),
            source((0.125, 0.25, 0.375, 0.5), statistic_id=bootstrap_id),
            source((None, 999, 72, 73), cumulative=False, statistic_id=measurement_id),
        )
        result = await self.writer.async_import(
            expanded, refreshed_identity, ordinary_start=START + 2 * HOUR
        )

        self.assertEqual(8, result["imported_rows"])
        for statistic_id in (ID, measurement_id):
            for instant in (START, START + HOUR):
                self.assertEqual(
                    previous[statistic_id][instant],
                    self.recorder.rows[statistic_id][instant],
                )
        self.assertEqual(1.5, self.recorder.rows[ID][START + 2 * HOUR].sum)
        self.assertEqual(2.5, self.recorder.rows[ID][START + 3 * HOUR].sum)
        self.assertEqual(
            [0.125, 0.375, 0.75, 1.25],
            [row.sum for row in self.recorder.rows[bootstrap_id].values()],
        )
        self.assertEqual(72, self.recorder.rows[measurement_id][START + 2 * HOUR].mean)
        self.assertEqual(73, self.recorder.rows[measurement_id][START + 3 * HOUR].mean)
        self.assertIsNone(self.writer.bootstrap_start())
        self.assertNotIn(renamed, self.recorder.rows)
        self.assertEqual(set(selected), set(self.writer.status()["series"]))
        self.assertEqual(selection, self.store.value["last_selection"])
        for base, record in self.writer.status()["series"].items():
            self.assertEqual(selected[base]["statistic_id"], record["statistic_id"])
            self.assertEqual(selected[base]["epoch_start"], record["epoch_start"])
            self.assertEqual([], record["closed"])

    async def test_missing_verified_seed_never_selects_new_epoch(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        del self.recorder.rows[ID][START + HOUR]
        with self.assertRaisesRegex(manager.HourlyImportError, "checkpoint"):
            await self.writer.async_import(
                (source((0.75,), start=START + 2 * HOUR),), identity()
            )
        self.assertEqual(
            START.isoformat(), self.store.value["series"][ID]["epoch_start"]
        )

    async def test_save_intent_failure_has_no_native_submission(self):
        await self.adopt()
        self.store.fail = self.store.calls + 1
        with self.assertRaises(OSError):
            await self.writer.async_import((source(),), identity())
        self.assertFalse(self.recorder.submissions)
        self.assertIsNone(self.store.value["pending"])
        self.store.fail = None
        result = await self.fresh().async_import((source(),), identity())
        self.assertEqual(2, result["imported_rows"])

    async def test_checkpoint_save_failure_recovers_without_resubmission(self):
        await self.adopt()
        self.store.fail = self.store.calls + 2
        with self.assertRaises(OSError):
            await self.writer.async_import((source(),), identity())
        self.assertIsNotNone(self.store.value["pending"])
        self.assertEqual(1, len(self.recorder.submissions))
        self.store.fail = None
        replacement = self.fresh()
        await replacement.async_reconcile()
        self.assertEqual(1, len(self.recorder.submissions))
        self.assertIsNone(self.store.value["pending"])
        self.assertEqual(
            2,
            replacement.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                "complete_observed_hours"
            ],
        )

    async def test_partial_effect_retries_only_identical_prior_rows(self):
        await self.adopt()
        self.recorder.partial = 1
        with self.assertRaises(RuntimeError):
            await self.writer.async_import((source(),), identity())
        self.assertEqual(1, len(self.recorder.rows[ID]))
        replacement = self.fresh()
        await replacement.async_reconcile()
        self.assertEqual(
            (START + HOUR,), tuple(row.start for row in self.recorder.submissions[-1])
        )
        self.assertEqual(0.75, self.recorder.rows[ID][START + HOUR].sum)

    async def test_third_state_stops_whole_batch_without_retry(self):
        await self.adopt()
        self.recorder.partial = 1
        with self.assertRaises(RuntimeError):
            await self.writer.async_import((source(),), identity())
        self.recorder.rows[ID][START] = planner.HourlyStatisticRow(
            START, state=99, sum=99
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "third state"):
            await self.fresh().async_reconcile()
        self.assertEqual(1, len(self.recorder.submissions))
        self.assertIsNotNone(self.store.value["pending"])

    async def test_correction_recomputes_full_suffix_and_bounds_partial_window(self):
        await self.adopt(source((0.25, 0.5, 0.75)))
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        await self.writer.async_import((source((0.5, 0.5, 0.75)),), identity())
        self.assertEqual(1.75, self.recorder.rows[ID][START + 2 * HOUR].sum)
        await self.writer.async_import((source((0.75,)),), identity())
        self.assertTrue(all(row.cleared for row in self.recorder.rows[ID].values()))
        self.assertEqual(
            START.isoformat(), self.store.value["series"][ID]["blocked_from"]
        )

    async def test_explicit_segment_preserves_old_prefix_and_retry_is_idempotent(self):
        await self.adopt(source((0.25, 0.5, 0.75)))
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        await self.writer.async_import((source((0.25, None, 0.75)),), identity())
        revision = self.writer.status()["revision"]
        args = {
            "epoch_start": START + 2 * HOUR,
            "statistic_ids": (ID,),
            "expected_revision": revision,
        }
        preview = await self.writer.async_select(
            (source((0.25, None, 0.75)),), identity(), **args
        )
        await self.writer.async_select(
            (source((0.25, None, 0.75)),),
            identity(),
            **args,
            preview_digest=preview["preview_digest"],
        )
        await self.writer.async_import(
            (source((0.75,), start=START + 2 * HOUR),), identity()
        )
        target = planner.segment_id(ID, START + 2 * HOUR)
        self.assertEqual(0.75, self.recorder.rows[target][START + 2 * HOUR].sum)
        self.assertEqual(0.25, self.recorder.rows[ID][START].sum)
        self.assertTrue(self.recorder.rows[ID][START + HOUR].cleared)
        replay = await self.writer.async_select(
            (source((0.75,), start=START + 2 * HOUR),),
            identity(),
            **args,
            preview_digest=preview["preview_digest"],
        )
        self.assertEqual("already_selected", replay["status"])

    async def test_existing_matching_metadata_is_collision(self):
        self.recorder.metadata[ID] = source().metadata
        with self.assertRaisesRegex(manager.HourlyImportError, "collides"):
            await self.writer.async_select(
                (source(),),
                identity(),
                epoch_start=START,
                statistic_ids=(ID,),
                expected_revision=0,
            )
        self.assertIsNone(self.store.value)

    async def test_interrupted_adoption_requires_exact_explicit_retry(self):
        args = {"epoch_start": START, "statistic_ids": (ID,), "expected_revision": 0}
        preview = await self.writer.async_select((source(),), identity(), **args)
        self.store.fail = 2
        with self.assertRaises(OSError):
            await self.writer.async_select(
                (source(),),
                identity(),
                **args,
                preview_digest=preview["preview_digest"],
            )
        replacement = self.fresh()
        with self.assertRaises(manager.HourlyImportError):
            await replacement.async_mode()
        self.store.fail = None
        await replacement.async_select(
            (source(),), identity(), **args, preview_digest=preview["preview_digest"]
        )
        self.assertEqual("hourly", await replacement.async_mode())
        self.assertFalse(self.recorder.submissions)

    async def test_cancelled_store_save_is_drained_before_replacement_load(self):
        await self.adopt()
        async with self.gated_import(gate_call=3) as importing:
            self.writer.close()
            importing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await importing
            replacement = self.fresh()
            recovery = self.create_operation_task(replacement.async_reconcile())
            await asyncio.sleep(0)
            self.assertFalse(recovery.done())
            self.assertFalse(self.recorder.submissions)
            self.store.gate.set()
            await recovery
            self.assertEqual(0.75, self.recorder.rows[ID][START + HOUR].sum)

    async def test_gated_import_rejects_early_success_and_settles_tasks(self):
        async def completed(*args):
            return {"imported_rows": 0}

        with (
            patch.object(self.writer, "async_import", completed),
            self.assertRaisesRegex(AssertionError, "Import completed before"),
        ):
            async with self.gated_import(gate_call=1):
                self.fail("Premature success must not enter the gated body")
        self.assertTrue(self.operation_tasks)
        self.assertTrue(
            all(task.done() for task in self.operation_tasks + self.store_tasks)
        )

    async def test_gated_import_preserves_early_exception_and_cleanup_failure(self):
        original = RuntimeError("synthetic early importer failure")

        async def failed_save():
            await self.store.gate.wait()
            raise OSError("synthetic cleanup save failure")

        async def failed(*args):
            self.hass.async_create_task(failed_save())
            raise original

        with (
            patch.object(self.writer, "async_import", failed),
            self.assertRaises(RuntimeError) as caught,
        ):
            async with self.gated_import(gate_call=1):
                self.fail("An early exception must not enter the gated body")
        self.assertIs(original, caught.exception)
        self.assertIn("synthetic cleanup save failure", " ".join(original.__notes__))
        self.assertTrue(self.store_tasks)
        self.assertTrue(
            all(task.done() for task in self.operation_tasks + self.store_tasks)
        )

    async def test_gated_import_deadline_settles_recovery_and_shielded_save(self):
        async def delayed_save():
            # Cannot reach Store's selected gate until failure cleanup releases it.
            await self.store.gate.wait()
            await self.store.async_save({"cleanup_completed": True})

        async def blocked(*args):
            save = self.hass.async_create_task(delayed_save())

            async def recovery():
                await asyncio.shield(save)

            self.create_operation_task(recovery())
            await asyncio.shield(save)

        with (
            patch.object(self.writer, "async_import", blocked),
            self.assertRaisesRegex(AssertionError, "Timed out waiting"),
        ):
            async with self.gated_import(gate_call=1, timeout=0.02):
                self.fail("A missed gate must not enter the gated body")
        self.assertEqual({"cleanup_completed": True}, self.store.value)
        self.assertTrue(
            all(task.done() for task in self.operation_tasks + self.store_tasks)
        )

    async def test_real_sensor_builder_coverage_average_is_finite_and_read_only(self):
        model = sys.modules[f"{PACKAGE}.config_model"]
        config = model.BeestatConfig(
            (model.ConfiguredThermostat(1, "zone", "Zone"),),
            (
                model.ConfiguredSensor(
                    10, "room", "Room", 1, "zone", True, False, False, False
                ),
            ),
        )
        statistic_id = "beestat:room_temperature_hourly_v2"
        bound = identity(statistic_id)
        bound["resources"][statistic_id] = {
            "thermostat_id": 1,
            "sensor_id": 10,
            "quantity": "temperature",
        }
        largest = sys.float_info.max
        cases = (
            ("largest", (largest, largest), 0, 2, largest),
            ("largest_with_gap", (largest, None, largest), 0, 3, largest),
            ("ordinary", (68.0, 74.0), 0, 2, 71.0),
            ("ordinary_with_gap", (68.0, None, 74.0), 0, 3, 71.0),
            ("zero", (0.0, 0.0), 0, 2, 0.0),
            ("sign_cancellation", (-100.0, 100.0), 0, 2, 0.0),
            ("no_observations", (70.0,), 1, 3, None),
        )
        for name, values, query_start, query_end, expected_mean in cases:
            with self.subTest(case=name):
                store, recorder = Store(), Recorder()
                entry = types.SimpleNamespace(entry_id="test-entry", data={})
                writer = manager.HourlyImportManager(
                    self.hass, entry, store=store, recorder=recorder
                )
                end = START + len(values) * HOUR
                raw = [
                    {
                        "sensor_id": 10,
                        "timestamp": (
                            START + hour * HOUR + slot * HOUR / 12
                        ).isoformat(),
                        "temperature": value,
                    }
                    for hour, value in enumerate(values)
                    if value is not None
                    for slot in range(12)
                ]
                built = builder.build_hourly_statistics(
                    {},
                    {10: raw},
                    config,
                    start=START,
                    end=end,
                    evaluated_at=end,
                    source_end_by_thermostat={1: end - HOUR / 12},
                )
                item = next(item for item in built if item.statistic_id == statistic_id)
                self.assertEqual(
                    [
                        "ready" if value is not None else "missing_slots"
                        for value in values
                    ],
                    [hour.reason for hour in item.hours],
                )
                args = {
                    "epoch_start": START,
                    "statistic_ids": (statistic_id,),
                    "expected_revision": 0,
                }
                preview = await writer.async_select((item,), bound, **args)
                await writer.async_select(
                    (item,), bound, **args, preview_digest=preview["preview_digest"]
                )
                await writer.async_import((item,), bound)
                saved_state = deepcopy(store.value)
                saved_rows = deepcopy(recorder.rows)
                saved_metadata = deepcopy(recorder.metadata)
                saved_status = deepcopy(writer.status())
                with (
                    patch.object(store, "async_load") as load,
                    patch.object(store, "async_save") as save,
                    patch.object(recorder, "async_snapshot") as snapshot,
                    patch.object(recorder, "submit") as submit,
                ):
                    response = writer.coverage(
                        start=START + query_start * HOUR,
                        end=START + query_end * HOUR,
                    )
                    load.assert_not_awaited()
                    save.assert_not_awaited()
                    snapshot.assert_not_awaited()
                    submit.assert_not_called()
                self.assertEqual(saved_state, store.value)
                self.assertEqual(saved_rows, recorder.rows)
                self.assertEqual(saved_metadata, recorder.metadata)
                self.assertEqual(saved_status, writer.status())
                result = response["series"][statistic_id]
                expected_values = [
                    values[hour] if hour < len(values) else None
                    for hour in range(query_start, query_end)
                ]
                observed_count = sum(value is not None for value in expected_values)
                self.assertEqual(observed_count, result["complete_observed_hours"])
                self.assertEqual(query_end - query_start, result["requested_hours"])
                self.assertEqual(
                    observed_count == query_end - query_start, result["complete"]
                )
                self.assertEqual(
                    expected_values, [hour["value"] for hour in result["hours"]]
                )
                self.assertEqual(
                    [
                        "missing"
                        if hour >= len(values)
                        else "missing_slots"
                        if values[hour] is None
                        else "verified"
                        for hour in range(query_start, query_end)
                    ],
                    [hour["coverage"] for hour in result["hours"]],
                )
                self.assertEqual(expected_mean, result["observed_hour_average"])
                if expected_mean is not None:
                    self.assertIsInstance(result["observed_hour_average"], float)
                self.assertEqual(
                    response, json.loads(json.dumps(response, allow_nan=False))
                )

    async def test_rolling_measurement_replay_ignores_expired_journal_history(self):
        initial = source((70, 71), cumulative=False)
        recent = source((72, 73), start=START + 2 * HOUR, cumulative=False)
        # Exercise normal journal eviction with a small retention window.
        with patch.object(manager, "_MAX_HOURS", 2):
            await self.adopt(initial)
            await self.writer.async_import((initial,), identity())
            await self.writer.async_import((recent,), identity())
            record = self.store.value["series"][ID]
            self.assertIsNone(record["checkpoint"])
            self.assertNotIn(START.isoformat(), record["coverage"])
            self.assertEqual(70, self.recorder.rows[ID][START].mean)
            retained_rows = deepcopy(self.recorder.rows[ID])
            submissions = len(self.recorder.submissions)

            replacement = self.fresh()
            with patch.object(
                self.recorder,
                "async_snapshot",
                wraps=self.recorder.async_snapshot,
            ) as snapshot:
                await replacement.async_import((recent,), identity())

            snapshot.assert_awaited_once_with(ID, START + HOUR)
            self.assertEqual(retained_rows, self.recorder.rows[ID])
            self.assertEqual(submissions, len(self.recorder.submissions))
            self.assertIsNone(self.store.value["series"][ID]["checkpoint"])
            coverage = replacement.coverage(
                start=START + 2 * HOUR, end=START + 4 * HOUR
            )["series"][ID]
            self.assertEqual(2, coverage["complete_observed_hours"])
            self.assertEqual(72.5, coverage["observed_hour_average"])

    async def test_identity_and_unit_drift_block_without_changing_saved_intent(self):
        await self.adopt()
        wrong = identity()
        wrong["account_anchors"] = ["different"]
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((source(),), wrong)
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((source(cumulative=False),), identity())
        self.assertFalse(self.recorder.submissions)

    async def test_identity_loss_suppresses_previously_verified_coverage(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        missing = identity()
        missing["account_anchors"] = []
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((source(),), missing)
        self.assert_coverage_suppressed()
        self.assertEqual(1, len(self.recorder.submissions))

    async def test_unit_drift_suppresses_previously_verified_coverage(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((source(cumulative=False),), identity())
        self.assert_coverage_suppressed()
        self.assertEqual(1, len(self.recorder.submissions))

    async def test_missing_adopted_resource_suppresses_its_old_coverage(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        other_id = "beestat:other_fan_runtime_hours_hourly_v2"
        other = identity(other_id)
        other["resources"][other_id]["thermostat_id"] = 2
        try:
            await self.writer.async_import((source(statistic_id=other_id),), other)
        except manager.HourlyImportError:
            pass
        self.assert_coverage_suppressed()
        self.assertEqual(1, len(self.recorder.submissions))

    async def test_pending_invalidation_suppresses_and_clears_beyond_source_window(
        self,
    ):
        initial = source((0.25, 0.5, 0.75, 1.0))
        await self.adopt(initial)
        await self.writer.async_import((initial,), identity())
        self.recorder.partial = 0
        with self.assertRaises(RuntimeError):
            await self.writer.async_import((source((0.25, None)),), identity())
        coverage = self.writer.coverage(start=START, end=START + 4 * HOUR)["series"][ID]
        self.assertEqual(
            [0.25, None, None, None], [hour["value"] for hour in coverage["hours"]]
        )
        self.assertEqual(
            ["pending_reconciliation"] * 3,
            [hour["coverage"] for hour in coverage["hours"][1:]],
        )
        replacement = self.fresh()
        await replacement.async_reconcile()
        self.assertTrue(
            all(self.recorder.rows[ID][START + i * HOUR].cleared for i in (1, 2, 3))
        )
        coverage = replacement.coverage(start=START, end=START + 4 * HOUR)["series"][ID]
        self.assertEqual(1, coverage["complete_observed_hours"])
        self.assertEqual(0.25, coverage["observed_hour_average"])

    async def test_renamed_slug_cannot_adopt_a_second_owner_for_same_resource(self):
        await self.adopt()
        renamed = "beestat:renamed_fan_runtime_hours_hourly_v2"
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_select(
                (source(statistic_id=renamed),),
                identity(renamed),
                epoch_start=START,
                statistic_ids=(renamed,),
                expected_revision=self.writer.status()["revision"],
            )
        self.assertEqual((ID,), self.writer.base_statistic_ids())
        self.assertFalse(self.recorder.submissions)

    async def test_persisted_base_selects_later_segment_after_source_slug_renames(self):
        initial = source((0.25, 0.5, 0.75))
        await self.adopt(initial)
        await self.writer.async_import((initial,), identity())
        await self.writer.async_import((source((0.25, None, 0.75)),), identity())
        renamed = "beestat:renamed_fan_runtime_hours_hourly_v2"
        item = source((0.75,), start=START + 2 * HOUR, statistic_id=renamed)
        args = {
            "epoch_start": START + 2 * HOUR,
            "statistic_ids": (ID,),
            "expected_revision": self.writer.status()["revision"],
        }
        preview = await self.writer.async_select((item,), identity(renamed), **args)
        await self.writer.async_select(
            (item,),
            identity(renamed),
            **args,
            preview_digest=preview["preview_digest"],
        )
        await self.writer.async_import((item,), identity(renamed))
        self.assertEqual((ID,), self.writer.base_statistic_ids())
        target = planner.segment_id(ID, START + 2 * HOUR)
        self.assertEqual(0.75, self.recorder.rows[target][START + 2 * HOUR].sum)
        self.assertNotIn(renamed, self.recorder.rows)

    async def test_disabled_old_counter_cannot_expand_active_bootstrap_window(self):
        old_epoch = START - manager.timedelta(days=400)
        await self.adopt(source(start=old_epoch))
        active_id = "beestat:active_fan_runtime_hours_hourly_v2"
        active = source(statistic_id=active_id)
        bound = identity(active_id)
        bound["resources"][active_id]["thermostat_id"] = 2
        args = {
            "epoch_start": START,
            "statistic_ids": (active_id,),
            "expected_revision": self.writer.status()["revision"],
        }
        preview = await self.writer.async_select((active,), bound, **args)
        await self.writer.async_select(
            (active,), bound, **args, preview_digest=preview["preview_digest"]
        )
        self.assertEqual(old_epoch, self.writer.bootstrap_start())
        self.assertEqual(
            START,
            self.writer.bootstrap_start(eligible_resources=bound["resources"]),
        )
        self.assertIsNone(
            self.writer.bootstrap_start(
                thermostat_id=1, eligible_resources=bound["resources"]
            )
        )
        self.assertIsNone(self.writer.bootstrap_start(eligible_resources={}))
        partition = await self.writer.async_writer_partition(bound)
        self.assertEqual({active_id}, partition.hourly_statistic_ids)
        self.assertIn(
            ID.removesuffix("_hourly_v2"), partition.frozen_legacy_statistic_ids
        )
        self.assertIsNone(self.store.value["series"][ID]["checkpoint"])

    async def test_exact_selection_retry_restores_missing_marker_idempotently(self):
        preview = await self.adopt()
        marker = self.entry.data.pop(manager.MARKER)
        before = deepcopy(self.store.value)
        replacement = self.fresh()
        with self.assertRaises(manager.HourlyImportError):
            await replacement.async_mode()
        await replacement.async_select(
            (source(),),
            identity(),
            epoch_start=START,
            statistic_ids=(ID,),
            expected_revision=0,
            preview_digest=preview["preview_digest"],
        )
        self.assertEqual(marker, self.entry.data[manager.MARKER])
        self.assertEqual("hourly", await replacement.async_mode())
        self.assertEqual(before, self.store.value)
        self.assertFalse(self.recorder.submissions)

    async def test_interrupted_selection_before_marker_retries_exact_saved_selection(
        self,
    ):
        args = {"epoch_start": START, "statistic_ids": (ID,), "expected_revision": 0}
        preview = await self.writer.async_select((source(),), identity(), **args)

        def fail_marker_update(entry, data):
            raise OSError("synthetic entry marker failure")

        self.hass.config_entries.async_update_entry = fail_marker_update
        with self.assertRaises(OSError):
            await self.writer.async_select(
                (source(),),
                identity(),
                **args,
                preview_digest=preview["preview_digest"],
            )
        self.assertNotIn(manager.MARKER, self.entry.data)
        self.assertIsNotNone(self.store.value["pending_selection"])
        self.hass.config_entries.async_update_entry = lambda entry, data: setattr(
            entry, "data", data
        )
        replacement = self.fresh()
        with self.assertRaises(manager.HourlyImportError):
            await replacement.async_mode()
        bound = identity()
        other_id = "beestat:other_fan_runtime_hours_hourly_v2"
        bound["resources"][other_id] = {
            "thermostat_id": 2,
            "sensor_id": None,
            "quantity": "fan_runtime_hours",
        }
        partition = await replacement.async_writer_partition(bound)
        self.assertEqual("selection_pending", partition.hourly_blocked_reason)
        self.assertFalse(partition.hourly_ready)
        self.assertEqual({ID}, partition.hourly_statistic_ids)
        self.assertEqual(
            {other_id.removesuffix("_hourly_v2")}, partition.legacy_statistic_ids
        )
        await replacement.async_select(
            (source(),), identity(), **args, preview_digest=preview["preview_digest"]
        )
        self.assertEqual("hourly", await replacement.async_mode())
        self.assertEqual(1, replacement.status()["revision"])
        self.assertFalse(self.recorder.submissions)

    async def test_pending_metadata_conflict_stops_without_retry(self):
        await self.pending_extension()
        self.recorder.metadata[ID]["unit_of_measurement"] = "s"
        replacement = self.fresh()
        with self.assertRaisesRegex(manager.HourlyImportError, "metadata"):
            await replacement.async_reconcile()
        self.assertEqual(2, len(self.recorder.submissions))
        self.assertIsNotNone(self.store.value["pending"])
        self.assert_coverage_suppressed(replacement)

    async def test_pending_predecessor_conflict_stops_without_retry(self):
        await self.pending_extension()
        self.recorder.rows[ID][START + HOUR] = planner.HourlyStatisticRow(
            START + HOUR, state=7, sum=7
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "third state"):
            await self.fresh().async_reconcile()
        self.assertEqual(2, len(self.recorder.submissions))
        self.assertIsNotNone(self.store.value["pending"])

    async def test_pending_unexpected_retained_tail_stops_without_retry(self):
        await self.pending_extension()
        self.recorder.rows[ID][START + 6 * HOUR] = planner.HourlyStatisticRow(
            START + 6 * HOUR, state=9, sum=9
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "third state"):
            await self.fresh().async_reconcile()
        self.assertEqual(2, len(self.recorder.submissions))
        self.assertIsNotNone(self.store.value["pending"])

    async def test_fully_checksummed_future_store_version_is_blocked(self):
        await self.adopt()
        self.store.value["version"] += 1
        self.store.value["integrity"] = manager._digest(
            {
                key: value
                for key, value in self.store.value.items()
                if key != "integrity"
            }
        )
        with self.assertRaises(manager.HourlyImportError):
            await self.fresh().async_mode()
        self.assertFalse(self.recorder.submissions)

    async def test_internally_invalid_coverage_is_not_trusted_despite_valid_checksum(
        self,
    ):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        record = self.store.value["series"][ID]
        record["coverage"][START.isoformat()]["row"] = None
        self.store.value["integrity"] = manager._digest(
            {
                key: value
                for key, value in self.store.value.items()
                if key != "integrity"
            }
        )
        replacement = self.fresh()
        with self.assertRaises(manager.HourlyImportError):
            await replacement.async_mode()
        self.assertEqual(1, len(self.recorder.submissions))

    async def test_future_and_open_hours_cannot_be_selected_as_observed_epochs(self):
        current_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        for epoch in (current_hour, current_hour + HOUR):
            with (
                self.subTest(epoch=epoch),
                self.assertRaises(manager.HourlyImportError),
            ):
                await self.writer.async_select(
                    (source((0.25,), start=epoch),),
                    identity(),
                    epoch_start=epoch,
                    statistic_ids=(ID,),
                    expected_revision=0,
                )
        self.assertIsNone(self.store.value)
        self.assertFalse(self.recorder.submissions)

    async def test_out_of_order_or_duplicate_source_hours_cannot_submit(self):
        item = source()
        await self.adopt(item)
        for hours in ((item.hours[1], item.hours[0]), (item.hours[0], item.hours[0])):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                await self.writer.async_import(
                    (replace(item, hours=hours),), identity()
                )
        self.assertFalse(self.recorder.submissions)
        self.assertIsNone(self.store.value["pending"])

    async def test_oversized_source_batch_cannot_save_intent_or_submit(self):
        old = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        old -= (manager._MAX_HOURS + 2) * HOUR
        item = source((0.25,) * (manager._MAX_HOURS + 1), start=old)
        await self.adopt(item)
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((item,), identity())
        self.assertFalse(self.recorder.submissions)
        self.assertIsNone(self.store.value["pending"])

    async def test_coverage_bounds_and_order_are_explicit(self):
        await self.adopt()
        for start, end in (
            (START, START),
            (START + HOUR, START),
            (START, START + (manager._MAX_HOURS + 1) * HOUR),
            (START.replace(tzinfo=None), START + HOUR),
            (START.replace(minute=1), START + HOUR),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.writer.coverage(start=start, end=end)

    async def test_unchanged_short_replay_preserves_later_verified_checkpoint(self):
        await self.adopt(source((0.25, 0.5, 0.75)))
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        submissions = len(self.recorder.submissions)
        await self.writer.async_import((source((0.25,)),), identity())
        self.assertEqual(submissions, len(self.recorder.submissions))
        self.assertEqual(
            (START + 2 * HOUR).isoformat(),
            self.store.value["series"][ID]["checkpoint"]["start"],
        )
        await self.writer.async_import(
            (source((0.1,), start=START + 3 * HOUR),), identity()
        )
        self.assertEqual(1.6, self.recorder.rows[ID][START + 3 * HOUR].sum)

    async def test_restored_older_journal_cannot_adopt_unexplained_native_effects(self):
        await self.adopt()
        await self.writer.async_import((source((0.25,)),), identity())
        old = deepcopy(self.store.value)
        await self.writer.async_import((source(),), identity())
        self.store.value = old
        replacement = self.fresh()
        with self.assertRaisesRegex(manager.HourlyImportError, "not explained"):
            await replacement.async_import((source(),), identity())
        self.assertEqual(
            0,
            replacement.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                "complete_observed_hours"
            ],
        )

    async def test_interrupted_selection_cannot_rebind_quantity(self):
        args = {"epoch_start": START, "statistic_ids": (ID,), "expected_revision": 0}
        preview = await self.writer.async_select((source(),), identity(), **args)
        self.store.fail = 2
        with self.assertRaises(OSError):
            await self.writer.async_select(
                (source(),),
                identity(),
                **args,
                preview_digest=preview["preview_digest"],
            )
        self.store.fail = None
        with self.assertRaisesRegex(
            manager.HourlyImportError, "resource or quantity changed"
        ):
            await self.fresh().async_select(
                (source(cumulative=False),),
                identity(),
                **args,
                preview_digest=preview["preview_digest"],
            )
        self.assertFalse(self.recorder.submissions)

    async def test_real_builder_misrouted_resource_blocks_and_suppresses(self):
        await self.adopt()
        await self.writer.async_import((source(),), identity())
        model = sys.modules[f"{PACKAGE}.config_model"]
        raw = [
            {
                "thermostat_id": 2,
                "timestamp": (START + manager.timedelta(minutes=5 * index)).isoformat(),
                "fan": 75,
            }
            for index in range(12)
        ]
        built = builder.build_hourly_statistics(
            {1: raw},
            {},
            model.BeestatConfig(
                (model.ConfiguredThermostat(1, "zone", "Fixture"),), ()
            ),
            start=START,
            end=START + HOUR,
            evaluated_at=START + 2 * HOUR,
            source_end_by_thermostat={1: START + manager.timedelta(minutes=55)},
        )
        broken = next(item for item in built if item.statistic_id == ID)
        self.assertFalse(broken.hours)
        self.assertEqual("resource_identity_mismatch", broken.blocked_reason)
        submissions = len(self.recorder.submissions)
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_import((broken,), identity())
        self.assertEqual(submissions, len(self.recorder.submissions))
        result = self.writer.coverage(start=START, end=START + 2 * HOUR)["series"][ID]
        self.assertEqual(0, result["complete_observed_hours"])
        self.assertFalse(result["complete"])

    async def test_final_checkpoint_save_keeps_pending_coverage_until_verified(self):
        await self.adopt()
        async with self.gated_import(gate_call=self.store.calls + 2) as importing:
            self.assertIsNotNone(self.store.value["pending"])
            self.assertEqual(0.75, self.recorder.rows[ID][START + HOUR].sum)
            self.assertTrue(self.writer.status()["pending"])
            self.assertEqual(
                0,
                self.writer.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                    "complete_observed_hours"
                ],
            )
            self.store.gate.set()
            await importing
            self.assertFalse(self.writer.status()["pending"])
            self.assertEqual(
                2,
                self.writer.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                    "complete_observed_hours"
                ],
            )

    async def test_cancelled_final_save_keeps_suppression_until_replacement_readback(
        self,
    ):
        await self.adopt()
        async with self.gated_import(gate_call=self.store.calls + 2) as importing:
            self.writer.close()
            importing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await importing
            self.assertTrue(self.writer.status()["pending"])
            self.assertEqual(
                0,
                self.writer.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                    "complete_observed_hours"
                ],
            )
            self.store.gate.set()
            replacement = self.fresh()
            recovery = self.create_operation_task(replacement.async_reconcile())
            await recovery
            self.assertFalse(replacement.status()["pending"])
            self.assertEqual(
                2,
                replacement.coverage(start=START, end=START + 2 * HOUR)["series"][ID][
                    "complete_observed_hours"
                ],
            )

    async def test_rolling_window_preserves_earliest_unresolved_boundary(self):
        await self.adopt(source((0.25, 0.5, 0.75)))
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        await self.writer.async_import((source((0.25, None, 0.75)),), identity())
        checkpoint = deepcopy(self.store.value["series"][ID]["checkpoint"])
        await self.writer.async_import(
            (source((0.5,), start=START + 24 * HOUR),), identity()
        )
        self.assertEqual(
            (START + HOUR).isoformat(), self.store.value["series"][ID]["blocked_from"]
        )
        self.assertEqual(checkpoint, self.store.value["series"][ID]["checkpoint"])

    async def test_short_replay_before_unresolved_gap_does_not_clear_boundary(self):
        await self.adopt(source((0.25, 0.5, 0.75)))
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        await self.writer.async_import((source((0.25, None, 0.75)),), identity())
        await self.writer.async_import((source((0.25,)),), identity())
        self.assertEqual(
            (START + HOUR).isoformat(), self.store.value["series"][ID]["blocked_from"]
        )
        await self.writer.async_import((source((0.25, 0.5, 0.75)),), identity())
        self.assertIsNone(self.store.value["series"][ID]["blocked_from"])

    async def test_provisional_tail_preserves_prefix_and_clears_complete_native_suffix(
        self,
    ):
        initial = source((0.25, 0.5, 0.75, 0.5))
        await self.adopt(initial)
        await self.writer.async_import((initial,), identity())
        limited = source((0.25, None))
        limited = replace(
            limited,
            hours=(limited.hours[0], replace(limited.hours[1], reason="provisional")),
        )
        await self.writer.async_import((limited,), identity())
        retained = self.recorder.rows[ID]
        self.assertEqual(0.25, retained[START].sum)
        self.assertTrue(
            all(row.cleared for instant, row in retained.items() if instant > START)
        )
        record = self.store.value["series"][ID]
        self.assertEqual(START.isoformat(), record["checkpoint"]["start"])
        self.assertEqual((START + HOUR).isoformat(), record["blocked_from"])

        advancing = source((0.25, 0.5, None))
        advancing = replace(
            advancing,
            hours=(
                *advancing.hours[:2],
                replace(advancing.hours[2], reason="provisional"),
            ),
        )
        await self.writer.async_import((advancing,), identity())
        self.assertEqual(0.75, self.recorder.rows[ID][START + HOUR].sum)
        self.assertIsNone(self.store.value["series"][ID]["blocked_from"])
        result = self.writer.coverage(start=START, end=START + 3 * HOUR)["series"][ID]
        self.assertEqual(
            ["verified", "verified", "provisional"],
            [hour["coverage"] for hour in result["hours"]],
        )

    async def test_corrected_prefix_cannot_keep_native_totals_in_provisional_tail(self):
        initial = source((0.25, 0.5, 0.75))
        await self.adopt(initial)
        await self.writer.async_import((initial,), identity())
        corrected = source((0.5, None))
        corrected = replace(
            corrected,
            hours=(
                corrected.hours[0],
                replace(corrected.hours[1], reason="provisional"),
            ),
        )
        await self.writer.async_import((corrected,), identity())
        self.assertTrue(all(row.cleared for row in self.recorder.rows[ID].values()))
        self.assertEqual(
            START.isoformat(), self.store.value["series"][ID]["blocked_from"]
        )
        self.assertIsNone(self.store.value["series"][ID]["checkpoint"])
        result = self.writer.coverage(start=START, end=START + 3 * HOUR)["series"][ID]
        self.assertEqual(0, result["complete_observed_hours"])

    async def test_epoch_without_checkpoint_remains_first_unverified_boundary(self):
        await self.adopt()
        later = source((0.5,), start=START + 24 * HOUR)
        await self.writer.async_import((later,), identity())
        self.assertEqual(
            START.isoformat(), self.store.value["series"][ID]["blocked_from"]
        )
        self.assertIsNone(self.store.value["series"][ID]["checkpoint"])
        args = {
            "epoch_start": START + 24 * HOUR,
            "statistic_ids": (ID,),
            "expected_revision": self.writer.status()["revision"],
        }
        preview = await self.writer.async_select((later,), identity(), **args)
        await self.writer.async_select(
            (later,), identity(), **args, preview_digest=preview["preview_digest"]
        )
        await self.writer.async_import((later,), identity())
        target = planner.segment_id(ID, START + 24 * HOUR)
        self.assertEqual(0.5, self.recorder.rows[target][START + 24 * HOUR].sum)
        self.assertNotIn(ID, self.recorder.metadata)

    async def test_exact_checkpoint_survives_coverage_eviction_during_hold(self):
        await self.adopt(source((0.25, 0.5)))
        await self.writer.async_import((source((0.25, 0.5)),), identity())
        await self.writer.async_import((source((0.25, None)),), identity())
        checkpoint = deepcopy(self.store.value["series"][ID]["checkpoint"])
        self.store.value["series"][ID]["coverage"].pop(START.isoformat())
        payload = {
            key: value for key, value in self.store.value.items() if key != "integrity"
        }
        self.store.value["integrity"] = manager._digest(payload)
        replacement = self.fresh()
        await replacement.async_import(
            (source((0.5,), start=START + 24 * HOUR),), identity()
        )
        self.assertEqual(checkpoint, self.store.value["series"][ID]["checkpoint"])
        self.assertEqual(
            (START + HOUR).isoformat(), self.store.value["series"][ID]["blocked_from"]
        )


if __name__ == "__main__":
    unittest.main()
