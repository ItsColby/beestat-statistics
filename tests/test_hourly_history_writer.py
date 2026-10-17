"""V3 adoption, bounded effects and exact-intent recovery on the shared writer."""

from __future__ import annotations

import asyncio
import json
import types
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from custom_components.beestat_statistics import config_model as model
from custom_components.beestat_statistics import hourly_history_delta as delta_builder
from custom_components.beestat_statistics import (
    hourly_history_values as representation,
)
from custom_components.beestat_statistics import hourly_history_writer as writer
from custom_components.beestat_statistics import hourly_import as manager
from custom_components.beestat_statistics import hourly_sources as sources
from custom_components.beestat_statistics.hourly_rows import RecorderSnapshot
from tests.history_support import Recorder as FixtureRecorder
from tests.history_support import Store as FixtureStore
from tests.history_support import identity, source

HOUR = timedelta(hours=1)
START = datetime(2026, 1, 31, 23, tzinfo=UTC)
KEY = "thermostat:1:fan_runtime_hours"
NATIVE = "beestat:zone_fan_runtime_rate_hourly_v3"


class Store(FixtureStore):
    def __init__(self):
        super().__init__()
        self.objects = {}
        self.fail_object_kind = None
        self.fail_read_kind = None

    async def async_write_object(self, kind, content):
        if kind == self.fail_object_kind:
            raise OSError("object persistence failed")
        reference = sha256(content).hexdigest()
        self.objects[kind, reference] = content
        return reference

    async def async_read_object(self, kind, reference):
        if kind == self.fail_read_kind:
            raise OSError("object readback failed")
        try:
            return self.objects[kind, reference]
        except KeyError as err:
            raise FileNotFoundError(reference) from err

    async def async_process_source_job(self, function, *args):
        return await asyncio.to_thread(function, *args)


class Recorder(FixtureRecorder):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.on_read = None

    async def async_snapshot_range(self, statistic_id, start, end):
        self.reads.append((statistic_id, start, end))
        if self.on_read:
            self.on_read()
        self.assert_bounded(start, end)
        return RecorderSnapshot(
            tuple(
                row
                for stamp, row in sorted(self.rows.get(statistic_id, {}).items())
                if start <= stamp < end
            ),
            deepcopy(self.metadata.get(statistic_id)),
            True,
        )

    def assert_bounded(self, start, end):
        if not 0 < (end - start) / HOUR <= 744:
            raise AssertionError("unbounded native read")


class HistoryWriterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.recorder = Store(), Recorder()
        self.entry = types.SimpleNamespace(entry_id="test-entry", data={})
        self.tasks = []
        self.hass = types.SimpleNamespace(
            data={},
            async_create_task=self.create_task,
            config_entries=types.SimpleNamespace(
                async_update_entry=lambda entry, data: setattr(entry, "data", data)
            ),
        )
        self.writer = self.fresh()
        self.current = True
        descriptors = representation.build_history_series((source(),), identity())
        self.context = {
            "identity": identity(),
            "config_revision": "c" * 64,
            "timezone": "America/New_York",
            "timezone_revision": 1,
            "evaluated_at": (START + 4 * HOUR).isoformat(),
            "check_current": self.guard,
            "config": model.BeestatConfig(
                thermostats=(
                    model.ConfiguredThermostat(
                        thermostat_id=1, slug="zone", name="Zone"
                    ),
                ),
                sensors=(),
            ),
            "descriptors": [item.descriptor for item in descriptors],
        }

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def fresh(self):
        return manager.HourlyImportManager(
            self.hass, self.entry, store=self.store, recorder=self.recorder
        )

    def guard(self):
        if not self.current:
            raise manager.HourlyImportError("context changed")

    async def asyncTearDown(self):
        if self.store.gate is not None:
            self.store.gate.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def stage(
        self,
        *,
        start=START,
        hours=2,
        fan=150,
        acquisition="read-one",
        empty=False,
        horizon=None,
    ):
        rows = (
            []
            if empty
            else [
                {
                    "thermostat_id": 1,
                    "timestamp": (start + timedelta(minutes=index * 5)).isoformat(),
                    "fan": fan,
                }
                for index in range(hours * 12)
            ]
        )
        if horizon is not None:
            rows = [
                row
                for row in rows
                if datetime.fromisoformat(row["timestamp"]) <= horizon
            ]
        content = json.dumps(rows).encode()
        declaration = {
            "contract_version": 3,
            "config_entry_id": self.entry.entry_id,
            "api_base": identity()["api_base"],
            "account_anchors": identity()["account_anchors"],
            "resource": "runtime_thermostat",
            "resource_id": 1,
            "thermostat_id": 1,
            "source_kind": "provider",
            "acquisition_id": acquisition,
            "chunk_index": 0,
            "chunk_count": 1,
            "format": "json",
            "start": start.isoformat(),
            "end": min(
                start + hours * HOUR,
                datetime.fromisoformat(self.context["evaluated_at"]),
            ).isoformat(),
            "acquired_at": self.context["evaluated_at"],
            "source_end": (
                horizon or start + hours * HOUR - timedelta(minutes=5)
            ).isoformat(),
            "unit_contract": "beestat_points_v1",
            "original_sha256": sha256(content).hexdigest(),
            "original_byte_count": len(content),
            "chunk_byte_offset": 0,
        }
        receipt = await sources.async_stage_source(
            self.store, content, declaration, sha256(content).hexdigest(), identity()
        )
        return receipt["source_id"]

    async def request(self, **changes):
        return {
            "config_entry_id": self.entry.entry_id,
            "source_ids": [await self.stage()],
            "quantity_ids": [KEY],
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
            "archive_policy": "reject",
            "daily_policy": writer.DAILY_POLICY,
            "operation_id": "initial-adoption",
            "expected_revision": 0,
            "consumer_contract": {
                "contract_version": 3,
                "consumers": [
                    {
                        "consumer_id": "fixture",
                        "version": "1",
                        "history_contract": 3,
                        "daily_policy": writer.DAILY_POLICY,
                    }
                ],
            },
            "recovery_reference": "consistent-recovery-fixture",
            **changes,
        }

    async def accept(self, request=None):
        plan = await self.writer.async_plan_history(
            request or await self.request(), context=self.context
        )
        self.assertEqual(plan["status"], "planned", plan)
        apply = {**plan["request"], "plan_digest": plan["plan_digest"]}
        result = await self.writer.async_accept_history(apply, context=self.context)
        return result, apply

    async def complete(self):
        while self.writer.has_pending_history:
            await self.writer.async_advance_history(context=self.context)

    async def delta_request(
        self, *, fan=300, acquisition="routine-delta", offsetless=False
    ):
        request = {"start": START.isoformat(), "end": (START + 2 * HOUR).isoformat()}
        baseline = await self.writer.async_history_source_points(
            request, context=self.context
        )
        original_id = await self.stage(fan=fan, acquisition=acquisition)
        original = (
            await sources.async_load_source_bundle(
                self.store, [original_id], identity()
            )
        )[0]
        raw = await self.store.async_read_object("source", original["sha256"])
        manifest = original["manifest"]
        if offsetless:
            rows = json.loads(raw)
            for row in rows:
                row["timestamp"] = row["timestamp"].removesuffix("+00:00")
            raw = json.dumps(rows).encode()
            manifest = {
                **manifest,
                "original_sha256": sha256(raw).hexdigest(),
                "original_byte_count": len(raw),
            }
        prepared = delta_builder.prepare_history_delta(
            [{"manifest": manifest, "original_bytes": raw}],
            baseline,
            identity=identity(),
            start=request["start"],
            end=request["end"],
            evaluated_at=self.context["evaluated_at"],
            acquisition_id=acquisition,
        )
        if prepared is None:
            return {**request, "source_ids": []}
        ids = []
        for chunk in prepared["chunks"]:
            receipt = await sources.async_stage_source(
                self.store,
                chunk["content"],
                chunk["manifest"],
                chunk["sha256"],
                identity(),
            )
            ids.append(receipt["source_id"])
        return {**request, "source_ids": ids}

    async def test_plan_is_readonly_and_detail_paging_keeps_digest(self):
        request = await self.request()
        plan = await self.writer.async_plan_history(request, context=self.context)
        second = await self.writer.async_plan_history(
            {**plan["request"], "detail_offset": 1, "detail_limit": 1},
            context=self.context,
        )
        self.assertEqual(plan["plan_digest"], second["plan_digest"])
        self.assertEqual(len(plan["batches"]), 2)
        self.assertEqual(second["request"]["config_entry_id"], self.entry.entry_id)
        self.assertEqual(self.store.calls, 0)
        self.assertEqual(self.entry.data, {})
        self.assertEqual(self.recorder.submissions, [])
        self.assertEqual(
            {kind for kind, _ in self.store.objects}, {"source", "manifest"}
        )

    async def test_accept_returns_before_effect_and_commits_months_independently(self):
        accepted, _ = await self.accept()
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.recorder.submissions, [])
        self.assertEqual(self.entry.data[manager.MARKER]["version"], 3)
        first = await self.writer.async_advance_history(context=self.context)
        self.assertEqual((first["status"], first["cursor"]), ("in_progress", 1))
        await self.complete()
        self.assertEqual(self.writer.history_status()["status"], "completed")
        self.assertEqual(
            [row.mean for row in self.recorder.rows[NATIVE].values()], [50, 50]
        )
        self.assertFalse(self.recorder.metadata[NATIVE]["has_sum"])
        self.assertIsNone(self.store.value["pending"])
        self.assertEqual(
            len(self.store.value["history"]["selections"][KEY]["proofs"]), 2
        )

    async def test_replay_observes_progress_and_conflicting_digest_is_rejected(self):
        _, apply = await self.accept()
        await self.writer.async_advance_history(context=self.context)
        before = len(self.recorder.submissions)
        self.assertEqual(
            (await self.writer.async_accept_history(apply, context=self.context))[
                "cursor"
            ],
            1,
        )
        self.assertEqual(len(self.recorder.submissions), before)
        with self.assertRaisesRegex(manager.HourlyImportError, "operation_id_conflict"):
            await self.writer.async_accept_history(
                {**apply, "plan_digest": "a" * 64}, context=self.context
            )

    async def test_pending_mixture_restarts_exact_saved_intent(self):
        request = await self.request(
            start=(START - HOUR).isoformat(), end=START.isoformat()
        )
        request["source_ids"] = [
            await self.stage(start=START - HOUR, acquisition="same-month")
        ]
        request["end"] = (START + HOUR).isoformat()
        _, apply = await self.accept(request)
        self.recorder.partial = 1
        with self.assertRaisesRegex(RuntimeError, "partial"):
            await self.writer.async_advance_history(context=self.context)
        self.assertEqual(self.store.value["operation"]["status"], "blocked")
        self.assertIsNotNone(self.store.value["pending"])
        self.writer = self.fresh()
        await self.writer.async_accept_history(apply, context=self.context)
        await self.complete()
        self.assertEqual(self.writer.history_status()["status"], "completed")
        self.assertEqual(len(self.recorder.rows[NATIVE]), 2)

    async def test_third_native_state_holds_without_replacing_intent(self):
        _, apply = await self.accept()
        self.store.fail_read_kind = "proof"
        with self.assertRaises(OSError):
            await self.writer.async_advance_history(context=self.context)
        pending = deepcopy(self.store.value["pending"])
        self.store.fail_read_kind = None
        self.recorder.rows[NATIVE][START] = manager.HourlyStatisticRow(START, mean=99)
        before = len(self.recorder.submissions)
        await self.writer.async_accept_history(apply, context=self.context)
        with self.assertRaisesRegex(manager.HourlyReconciliationError, "third_state"):
            await self.writer.async_advance_history(context=self.context)
        self.assertEqual(len(self.recorder.submissions), before)
        self.assertEqual(self.store.value["pending"], pending)

    async def test_cancellation_before_durable_intent_has_no_native_submission(self):
        await self.accept()
        self.store.gate = asyncio.Event()
        self.store.gate_call = self.store.calls + 1
        task = self.create_task(self.writer.async_advance_history(context=self.context))
        await asyncio.wait_for(self.store.gate_entered.wait(), 3)
        self.assertIsNotNone(self.writer._state["pending"])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.recorder.submissions, [])
        self.writer.close()
        replacement = self.fresh()
        recovery = self.create_task(replacement._load())
        await asyncio.sleep(0)
        self.assertFalse(recovery.done())
        self.store.gate.set()
        await asyncio.gather(*(task for task in self.tasks if not task.cancelled()))
        self.writer = replacement
        await recovery
        await self.complete()
        self.assertEqual(len(self.recorder.rows[NATIVE]), 2)

    async def test_context_change_after_await_blocks_plan_and_effects(self):
        request = await self.request()
        self.recorder.on_read = lambda: setattr(self, "current", False)
        result = await self.writer.async_plan_history(request, context=self.context)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.store.calls, 0)
        self.assertEqual(self.recorder.submissions, [])

    async def test_cold_adopted_marker_never_reports_unselected_ownership(self):
        self.assertEqual(self.writer.history_status()["status"], "unselected")
        await self.accept()
        self.store.value = None
        other = self.fresh()
        status = other.history_status()
        self.assertEqual(
            (status["status"], status["root_revision"], status["has_pending"]),
            ("blocked", 0, False),
        )
        self.assertEqual(other.history_configuration(self.context)["quantities"], [])
        with self.assertRaises(manager.HourlyImportError):
            await other.async_writer_partition(identity())
        self.assertEqual(other.history_status()["status"], "blocked")
        self.assertEqual(self.recorder.submissions, [])

    async def test_query_is_readonly_and_pending_suppresses_exact_window(self):
        await self.accept()
        self.store.fail_read_kind = "proof"
        with self.assertRaises(OSError):
            await self.writer.async_advance_history(context=self.context)
        before = (self.store.calls, len(self.recorder.submissions))
        material = await self.writer.async_history_material(
            {
                "quantity_ids": [KEY],
                "start": START.isoformat(),
                "end": (START + 2 * HOUR).isoformat(),
            },
            context=self.context,
        )
        self.assertEqual(
            material["pending_affected"][KEY],
            [[START.isoformat(), (START + HOUR).isoformat()]],
        )
        self.assertEqual(material["hours"][KEY], [])
        self.assertEqual((self.store.calls, len(self.recorder.submissions)), before)

    async def test_only_daily_queries_read_the_legacy_source(self):
        await self.accept()
        await self.complete()
        for period in (None, "hour", "day"):
            with self.subTest(period=period):
                self.recorder.reads.clear()
                request = {
                    "quantity_ids": [KEY],
                    "start": (START - timedelta(days=2)).isoformat(),
                    "end": (START + 2 * HOUR).isoformat(),
                }
                if period is not None:
                    request["period"] = period
                await self.writer.async_history_material(request, context=self.context)
                read_ids = {item[0] for item in self.recorder.reads}
                self.assertIn(NATIVE, read_ids)
                self.assertEqual(
                    "beestat:zone_fan_runtime_hours" in read_ids, period == "day"
                )

    async def test_query_guard_accepts_cold_cache_and_rejects_durable_change(self):
        await self.accept()
        cold = self.fresh()
        material = await cold.async_history_material(
            {
                "quantity_ids": [KEY],
                "start": START.isoformat(),
                "end": (START + 2 * HOUR).isoformat(),
            },
            context=self.context,
        )
        self.assertIsNone(cold._state)
        self.assertGreater(material["root_revision"], 0)
        await cold.async_check_history_material(material, context=self.context)
        await self.writer.async_advance_history(context=self.context)
        with self.assertRaisesRegex(
            manager.HourlyImportError, "history_query_root_changed"
        ):
            await cold.async_check_history_material(material, context=self.context)

    async def test_query_guard_rejects_writer_intent_during_root_fence_read(self):
        await self.accept()
        cold = self.fresh()
        material = await cold.async_history_material(
            {
                "quantity_ids": [KEY],
                "start": START.isoformat(),
                "end": (START + 2 * HOUR).isoformat(),
            },
            context=self.context,
        )
        reached, release = asyncio.Event(), asyncio.Event()
        check_fence = cold._check_history_root_fence

        async def pause_first_fence(state):
            if not reached.is_set():
                reached.set()
                await release.wait()
            await check_fence(state)

        cold._check_history_root_fence = pause_first_fence
        task = self.create_task(
            cold.async_check_history_material(material, context=self.context)
        )
        try:
            await reached.wait()
            await cold.async_advance_history(context=self.context)
        finally:
            release.set()
        with self.assertRaisesRegex(
            manager.HourlyImportError, "history_query_root_changed"
        ):
            await task

    async def test_query_guard_rejects_completed_fence_during_negative_read(self):
        await self.accept()
        await self.complete()
        cold = self.fresh()
        material = await cold.async_history_material(
            {
                "quantity_ids": [KEY],
                "start": START.isoformat(),
                "end": (START + HOUR).isoformat(),
            },
            context=self.context,
        )
        root_before, entry_before = (
            deepcopy(self.store.value),
            deepcopy(self.entry.data),
        )
        reached, release = asyncio.Event(), asyncio.Event()
        read = self.store.async_read_object

        async def pause_negative_read(kind, reference):
            try:
                return await read(kind, reference)
            except FileNotFoundError:
                if kind == "operation" and not reached.is_set():
                    reached.set()
                    await release.wait()
                raise

        self.store.async_read_object = pause_negative_read
        task = self.create_task(
            cold.async_check_history_material(material, context=self.context)
        )
        try:
            await asyncio.wait_for(reached.wait(), 3)
            await self.writer._invalidate_history_root(root_before["token"])
            self.assertFalse(self.writer.has_pending_store_save())
            self.assertEqual(self.store.value, root_before)
            self.assertEqual(self.entry.data, entry_before)
            self.assertIsNone(cold._state)
        finally:
            release.set()
        with self.assertRaisesRegex(manager.HourlyImportError, "root_changed"):
            await asyncio.wait_for(task, 3)

    async def test_initial_native_identity_collision_blocks_without_saves(self):
        self.recorder.metadata[NATIVE] = {"statistic_id": NATIVE}
        plan = await self.writer.async_plan_history(
            await self.request(), context=self.context
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(self.store.calls, 0)

    async def test_empty_routine_window_preserves_admitted_values_and_source_ids(self):
        await self.accept()
        await self.complete()
        old_sources = set(self.store.value["history"]["source_ids"])
        empty = await self.stage(acquisition="empty-later", empty=True)
        result = await self.writer.async_refresh_history(
            {
                "source_ids": [empty],
                "start": START.isoformat(),
                "end": (START + 2 * HOUR).isoformat(),
            },
            context=self.context,
        )
        # Nothing to rewrite: the source is admitted without a native batch.
        self.assertEqual((result["status"], result["batch_count"]), ("completed", 0))
        self.assertEqual(
            [row.mean for row in self.recorder.rows[NATIVE].values()], [50, 50]
        )
        self.assertTrue(old_sources < set(self.store.value["history"]["source_ids"]))

    async def test_interrupted_batchless_accept_resumes_and_keeps_adoption(self):
        await self.accept()
        await self.complete()
        empty = await self.stage(acquisition="empty-later", empty=True)
        original = self.store.async_save

        async def interrupted(value):
            if value["phase"] == "hourly" and value["operation"]["batch_count"] == 0:
                raise OSError("synthetic restart before the marker step")
            await original(value)

        self.store.async_save = interrupted
        with self.assertRaises(OSError):
            await self.writer.async_refresh_history(
                {
                    "source_ids": [empty],
                    "start": START.isoformat(),
                    "end": (START + 2 * HOUR).isoformat(),
                },
                context=self.context,
            )
        self.store.async_save = original
        saved = self.store.value
        self.assertEqual(
            (saved["phase"], saved["operation"]["status"]), ("prepared", "completed")
        )
        # The entry worker starts on has_pending and owns the marker step.
        self.assertTrue(writer.status(saved)["has_pending"])
        self.writer = self.fresh()
        result = await self.writer.async_advance_history(context=self.context)
        self.assertEqual(result["status"], "completed")
        saved = self.store.value
        self.assertEqual(saved["phase"], "hourly")
        self.assertFalse(self.writer.has_pending_history)
        self.assertEqual(
            saved["history"]["selections"][KEY]["descriptor"]["writer_status"],
            "adopted",
        )

    async def test_plan_conflict_is_readonly_but_routine_conflict_durably_holds(self):
        await self.accept()
        await self.complete()
        incoming = await self.stage(acquisition="disputed", fan=300)
        before = (self.store.calls, len(self.recorder.submissions))
        request = await self.request(
            source_ids=[incoming],
            expected_revision=self.store.value["revision"],
            operation_id="inspect-dispute",
        )
        plan = await self.writer.async_plan_history(request, context=self.context)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual((self.store.calls, len(self.recorder.submissions)), before)
        self.assertNotIn("source_holds", self.store.value["history"])
        coverage = self.store.value["history"]["coverage_revision"]
        result = await self.writer.async_refresh_history(
            {
                "source_ids": [incoming],
                "start": request["start"],
                "end": request["end"],
            },
            context=self.context,
        )
        self.assertEqual(result["error"], "source_conflict")
        self.assertEqual(self.store.value["history"]["coverage_revision"], coverage)
        self.assertIsNone(self.store.value["pending"])
        self.assertEqual(len(self.recorder.submissions), before[1])
        self.writer = self.fresh()
        material = await self.writer.async_history_material(
            request, context=self.context
        )
        self.assertEqual(len(material["source_holds"][KEY]), 2)
        self.assertEqual(material["source_holds"][KEY][0]["source_ids"], [incoming])
        # A reviewed quarantining operation clears only its committed hold batch.
        request["expected_revision"] = self.store.value["revision"]
        await self.accept(request)
        await self.writer.async_advance_history(context=self.context)
        material = await self.writer.async_history_material(
            request, context=self.context
        )
        self.assertEqual(len(material["source_holds"][KEY]), 1)
        self.assertIsNone(self.recorder.rows[NATIVE][START].mean)
        await self.complete()
        self.assertFalse(self.store.value["history"]["source_holds"])

    async def test_failed_hold_save_blocks_old_verified_view_after_restart(self):
        await self.accept()
        await self.complete()
        incoming = await self.stage(acquisition="disputed", fan=300)
        request = {
            "source_ids": [incoming],
            "quantity_ids": [KEY],
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
        }
        old_token = self.store.value["token"]
        old_entry = deepcopy(self.entry.data)
        self.store.fail = self.store.calls + 1
        with self.assertRaises(OSError):
            await self.writer.async_refresh_history(request, context=self.context)
        self.assertEqual(self.store.value["token"], old_token)
        self.assertNotEqual(self.entry.data[manager.MARKER]["token"], old_token)
        # A Core crash may lose the delayed config-entry marker update.
        self.entry.data = old_entry
        self.writer = self.fresh()
        with self.assertRaisesRegex(manager.HourlyImportError, "root_invalidated"):
            await self.writer.async_history_material(request, context=self.context)
        with self.assertRaises(manager.HourlyImportError):
            await self.writer.async_writer_partition(identity())

    async def test_cancelled_fence_write_blocks_concurrent_admission_until_same_write_settles(
        self,
    ):
        await self.accept()
        await self.complete()
        original = self.store.async_write_object
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(kind, content):
            if (
                kind == "operation"
                and json.loads(content).get("kind") == "history_invalidated_root"
            ):
                entered.set()
                await release.wait()
            return await original(kind, content)

        self.store.async_write_object = gated
        token = self.store.value["token"]
        task = self.create_task(self.writer._invalidate_history_root(token))
        await asyncio.wait_for(entered.wait(), 3)
        self.assertEqual(self.writer.history_status()["status"], "blocked")
        request = {
            "quantity_ids": [KEY],
            "start": START.isoformat(),
            "end": (START + HOUR).isoformat(),
        }
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.writer.has_pending_store_save())
        other = self.fresh()
        with self.assertRaisesRegex(manager.HourlyImportError, "write_pending"):
            await other.async_history_material(request, context=self.context)
        takeover = self.create_task(other.async_writer_partition(identity()))
        await asyncio.sleep(0)
        self.assertFalse(takeover.done())
        release.set()
        await asyncio.gather(
            *(
                pending
                for pending in self.tasks
                if not pending.cancelled() and pending is not takeover
            )
        )
        with self.assertRaises(manager.HourlyImportError):
            await takeover
        with self.assertRaisesRegex(manager.HourlyImportError, "root_invalidated"):
            await other.async_history_material(request, context=self.context)

    async def test_fence_is_idempotent_and_corrupt_or_unreadable_is_never_absence(self):
        await self.accept()
        await self.complete()
        token = self.store.value["token"]
        reference = await self.writer._invalidate_history_root(token)
        before = len(self.store.objects)
        self.assertEqual(await self.writer._invalidate_history_root(token), reference)
        self.assertEqual(len(self.store.objects), before)
        self.hass.data.clear()
        self.store.objects["operation", reference] = b"corrupt"
        other = self.fresh()
        request = {
            "quantity_ids": [KEY],
            "start": START.isoformat(),
            "end": (START + HOUR).isoformat(),
        }
        with self.assertRaisesRegex(manager.HourlyImportError, "fence_corrupt"):
            await other.async_history_material(request, context=self.context)
        original = self.store.async_read_object

        async def unreadable(kind, digest):
            if kind == "operation" and digest == reference:
                raise PermissionError("fence unreadable")
            return await original(kind, digest)

        self.store.async_read_object = unreadable
        with self.assertRaisesRegex(manager.HourlyImportError, "fence_unreadable"):
            await self.fresh().async_history_material(request, context=self.context)

    async def test_root_only_rollback_remains_fenced_but_consistent_restore_matches(
        self,
    ):
        await self.accept()
        await self.complete()
        old_root, old_entry, old_objects = (
            deepcopy(self.store.value),
            deepcopy(self.entry.data),
            deepcopy(self.store.objects),
        )
        incoming = await self.stage(acquisition="disputed", fan=300)
        request = {
            "source_ids": [incoming],
            "quantity_ids": [KEY],
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
        }
        await self.writer.async_refresh_history(request, context=self.context)
        successor = await self.writer.async_history_material(
            request, context=self.context
        )
        self.assertEqual(len(successor["source_holds"][KEY]), 2)
        self.assertTrue(self.store.value["history"]["invalidation_fences"])
        self.store.value, self.entry.data = old_root, old_entry
        self.hass.data.clear()
        with self.assertRaisesRegex(manager.HourlyImportError, "root_invalidated"):
            await self.fresh().async_history_material(request, context=self.context)
        self.store.objects = old_objects
        restored = await self.fresh().async_history_material(
            request, context=self.context
        )
        self.assertFalse(restored["source_holds"][KEY])
        self.assertEqual(len(restored["hours"][KEY]), 2)

    async def test_failed_fence_write_has_no_success_claim_or_native_effect(self):
        await self.accept()
        await self.complete()
        before = (
            deepcopy(self.store.value),
            deepcopy(self.entry.data),
            len(self.recorder.submissions),
        )
        self.store.fail_object_kind = "operation"
        with self.assertRaises(OSError):
            await self.writer._invalidate_history_root(self.store.value["token"])
        self.assertEqual(self.writer.history_status()["status"], "blocked")
        self.assertEqual(
            (self.store.value, self.entry.data, len(self.recorder.submissions)), before
        )

    async def test_failed_seal_leaves_the_root_and_recorder_untouched(self):
        plan = await self.writer.async_plan_history(
            await self.request(), context=self.context
        )
        apply = {**plan["request"], "plan_digest": plan["plan_digest"]}
        before = deepcopy(self.store.value)
        # A batch names a proof only once it is written; an orphan proof is inert.
        for kind in ("proof", "operation"):
            self.store.fail_object_kind = kind
            with self.assertRaises(OSError):
                await self.writer.async_accept_history(apply, context=self.context)
            self.assertEqual(self.store.value, before)
            self.assertFalse(
                [key for key in self.store.objects if key[0] == "operation"]
            )
        self.assertTrue([key for key in self.store.objects if key[0] == "proof"])
        self.assertEqual(self.recorder.submissions, [])
        self.store.fail_object_kind = None
        await self.writer.async_accept_history(apply, context=self.context)
        await self.complete()
        self.assertEqual(self.writer.history_status()["status"], "completed")

    async def test_baseline_is_window_bounded_and_empty_refresh_has_no_effect(self):
        await self.accept()
        await self.complete()
        request = {"start": START.isoformat(), "end": (START + HOUR).isoformat()}
        baseline = await self.writer.async_history_source_points(
            request, context=self.context
        )
        resource = baseline["resources"]["runtime_thermostat:1"]
        self.assertEqual(len(resource["rows"]), 12)
        self.assertEqual(len(resource["slots"]), 12)
        self.assertEqual(
            baseline["baseline_digest"],
            writer.digest(
                {
                    key: value
                    for key, value in baseline.items()
                    if key != "baseline_digest"
                }
            ),
        )
        before = (
            self.store.calls,
            len(self.store.objects),
            len(self.recorder.submissions),
        )
        await self.writer.async_refresh_history(
            {**request, "source_ids": []}, context=self.context
        )
        self.assertEqual(
            (self.store.calls, len(self.store.objects), len(self.recorder.submissions)),
            before,
        )

    async def test_oversized_month_catalog_selects_bounded_window_without_public_cap_relaxation(
        self,
    ):
        # This isolated catalog check needs the real size limit and parsers,
        # but not thousands of thread handoffs from the in-memory store.
        # Native service/runtime tests separately verify executor placement.
        async def process_source_job(function, *args):
            return function(*args)

        self.store.async_process_source_job = process_source_job
        await self.accept()
        await self.complete()
        root = deepcopy(self.store.value)
        history = root["history"]
        month = START.strftime("%Y-%m")
        catalog = json.loads(
            await self.store.async_read_object(
                "operation", history["source_catalog"][month]
            )
        )
        old_start = START - timedelta(days=10)
        older_ids = [
            await self.stage(
                start=old_start, hours=1, acquisition=f"catalog-retained-{index}"
            )
            for index in range(writer.MAX_SOURCE_CHUNKS)
        ]
        catalog["source_ids"] = sorted(set(catalog["source_ids"]) | set(older_ids))
        self.assertEqual(len(catalog["source_ids"]), writer.MAX_SOURCE_CHUNKS + 1)
        history["source_catalog"][month] = await self.store.async_write_object(
            "operation", writer.canonical_json(catalog)
        )
        history["first_provider"]["runtime_thermostat:1"] = old_start.isoformat()
        history["source_revision"] = writer.digest(
            {
                "catalog": history["source_catalog"],
                "first_provider": history["first_provider"],
                "provider_order": history["provider_order"],
                "provider_supersedes": history["provider_supersedes"],
            }
        )
        root["revision"] += 1
        self.store.value = writer.sealed(root)
        self.writer = self.fresh()
        reads = []
        original_read = self.store.async_read_object

        async def traced(kind, reference):
            if kind == "source":
                reads.append(reference)
            return await original_read(kind, reference)

        self.store.async_read_object = traced
        old_seal = json.loads(await original_read("manifest", older_ids[0]))
        request = {
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
            "source_ids": [],
        }
        baseline = await self.writer.async_history_source_points(
            request, context=self.context
        )
        self.assertEqual(len(baseline["resources"]["runtime_thermostat:1"]["rows"]), 24)
        self.assertEqual(
            baseline["first_provider"]["runtime_thermostat:1"], old_start.isoformat()
        )
        self.assertNotIn(old_seal["sha256"], reads)
        saved, object_count = deepcopy(self.store.value), len(self.store.objects)
        self.assertEqual(
            await self.writer.async_refresh_history(request, context=self.context),
            {"status": "unchanged", "changed_rows": 0},
        )
        self.assertEqual(self.store.value, saved)
        self.assertEqual(len(self.store.objects), object_count)
        operation = json.loads(
            await original_read("operation", root["operation"]["object"])
        )
        with self.assertRaisesRegex(manager.HourlyImportError, "sources_invalid"):
            self.writer._history_writer().request(
                {**operation["request"], "source_ids": catalog["source_ids"]},
                self.context,
            )

    async def test_delta_corrections_keep_lineage_after_benign_configuration_revision(
        self,
    ):
        await self.accept()
        await self.complete()
        self.context["config_revision"] = "d" * 64
        request = await self.delta_request()
        result = await self.writer.async_refresh_history(request, context=self.context)
        self.assertEqual(result["status"], "accepted")
        await self.complete()
        self.assertEqual(
            [row.mean for row in self.recorder.rows[NATIVE].values()], [100, 100]
        )
        self.assertEqual(
            self.store.value["history"]["provider_supersedes"]["runtime_thermostat:1"][
                "routine-delta"
            ],
            ["read-one"],
        )
        invalid = await self.delta_request(fan=None, acquisition="invalid-later")
        await self.writer.async_refresh_history(invalid, context=self.context)
        await self.complete()
        self.assertTrue(
            all(row.mean is None for row in self.recorder.rows[NATIVE].values())
        )
        material = await self.writer.async_history_material(
            {**invalid, "quantity_ids": [KEY]}, context=self.context
        )
        self.assertTrue(
            all(row["status"] == "invalid_slots" for row in material["hours"][KEY])
        )

    async def test_stale_delta_baseline_cannot_create_a_new_operation(self):
        await self.accept()
        await self.complete()
        stale = await self.delta_request(acquisition="stale")
        current = await self.delta_request(fan=75, acquisition="accepted-first")
        await self.writer.async_refresh_history(current, context=self.context)
        await self.complete()
        before = deepcopy(self.store.value)
        with self.assertRaisesRegex(
            manager.HourlyImportError, "delta_baseline_changed"
        ):
            await self.writer.async_refresh_history(stale, context=self.context)
        self.assertEqual(self.store.value, before)

    async def test_offsetless_semantically_equal_provider_rows_do_not_make_delta(self):
        await self.accept()
        await self.complete()
        request = await self.delta_request(fan=150, offsetless=True)
        self.assertEqual(request["source_ids"], [])

    async def test_retained_complete_provisional_hour_closes_only_through_journal(self):
        self.context["evaluated_at"] = (
            START + HOUR + timedelta(minutes=57)
        ).isoformat()
        await self.accept()
        await self.complete()
        self.assertNotIn(START + HOUR, self.recorder.rows[NATIVE])
        sources_before = {
            key for key in self.store.objects if key[0] in {"source", "manifest"}
        }
        self.context["evaluated_at"] = (START + 3 * HOUR).isoformat()
        request = {
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
            "source_ids": [],
        }
        before = self.store.value["history"]["coverage_revision"]
        accepted = await self.writer.async_refresh_history(
            request, context=self.context
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.store.value["history"]["coverage_revision"], before)
        self.assertNotIn(START + HOUR, self.recorder.rows[NATIVE])
        await self.complete()
        self.assertEqual(self.recorder.rows[NATIVE][START + HOUR].mean, 50)
        self.assertEqual(
            {key for key in self.store.objects if key[0] in {"source", "manifest"}},
            sources_before,
        )
        saved = deepcopy(self.store.value)
        objects = len(self.store.objects)
        self.assertEqual(
            await self.writer.async_refresh_history(request, context=self.context),
            {"status": "unchanged", "changed_rows": 0},
        )
        self.assertEqual(self.store.value, saved)
        self.assertEqual(len(self.store.objects), objects)

    async def test_retained_rows_without_adequate_source_horizon_never_gain_credit(
        self,
    ):
        source_id = await self.stage(horizon=START + HOUR + timedelta(minutes=50))
        request = await self.request(source_ids=[source_id])
        await self.accept(request)
        await self.complete()
        saved = deepcopy(self.store.value)
        self.context["evaluated_at"] = (START + 5 * HOUR).isoformat()
        result = await self.writer.async_refresh_history(
            {"source_ids": [], "start": request["start"], "end": request["end"]},
            context=self.context,
        )
        self.assertEqual(result, {"status": "unchanged", "changed_rows": 0})
        self.assertEqual(self.store.value, saved)
        self.assertNotIn(START + HOUR, self.recorder.rows[NATIVE])

    async def test_adopted_v3_status_uses_its_root_and_rejects_retired_writers(self):
        await self.accept()
        await self.complete()
        self.assertEqual(self.writer.status()["mode"], "history")
        self.assertEqual(self.writer.status()["revision"], self.store.value["revision"])
        self.assertFalse(self.writer.status()["pending"])
        saved = deepcopy(self.store.value)
        for change in (
            {"version": 1},
            {"legacy_v2": {"series": {}}},
            {"pending": {"generation": 2, "intent": {}}},
        ):
            with self.subTest(change=change):
                self.store.value = writer.sealed({**saved, **change})
                with self.assertRaises(manager.HourlyImportError):
                    await self.fresh().async_writer_partition(identity())
        self.store.value = saved
        self.assertEqual(
            (
                await self.fresh().async_writer_partition(identity())
            ).legacy_statistic_ids,
            frozenset(),
        )

    async def test_unselected_entry_keeps_daily_writer_but_missing_adopted_root_blocks(
        self,
    ):
        partition = await self.writer.async_writer_partition(identity())
        self.assertFalse(partition.has_hourly)
        self.assertEqual(
            partition.legacy_statistic_ids, {"beestat:zone_fan_runtime_hours"}
        )
        self.entry.data[manager.MARKER] = {"version": 3, "token": "a" * 64}
        with self.assertRaises(manager.HourlyImportError):
            await self.fresh().async_writer_partition(identity())
        self.assertFalse(self.recorder.submissions)

    async def test_legacy_partition_freezes_only_adopted_physical_quantity(self):
        await self.accept()
        current = identity()
        current["resources"]["beestat:zone_cool_runtime_hours_hourly_v2"] = {
            "thermostat_id": 1,
            "sensor_id": None,
            "quantity": "cool_runtime_hours",
        }
        partition = await self.writer.async_writer_partition(current)
        self.assertEqual(
            partition.legacy_statistic_ids, {"beestat:zone_cool_runtime_hours"}
        )
        self.assertTrue(partition.has_hourly)

    async def test_cold_partition_preserves_daily_voc_and_unmapped_sensors_after_rename_or_disable(
        self,
    ):
        await self.accept()
        await self.complete()
        bound = identity()
        original = next(iter(bound["resources"]))
        voc = "beestat:room_voc_concentration_hourly_v2"
        orphan = "beestat:unmapped_temperature_hourly_v2"
        bound["resources"][voc] = {
            "thermostat_id": 1,
            "sensor_id": 10,
            "quantity": "voc_concentration",
        }
        bound["resources"][orphan] = {
            "thermostat_id": None,
            "sensor_id": 11,
            "quantity": "temperature",
        }
        renamed = deepcopy(bound)
        renamed["resources"]["beestat:renamed_fan_runtime_hours_hourly_v2"] = renamed[
            "resources"
        ].pop(original)
        disabled = deepcopy(bound)
        disabled["resources"].pop(original)
        empty = {**bound, "resources": {}}
        saved, saves, reads = (
            deepcopy(self.store.value),
            self.store.calls,
            list(self.recorder.reads),
        )
        for current in (bound, renamed, disabled, empty):
            with self.subTest(resources=current["resources"]):
                result = await self.fresh().async_writer_partition(current)
                self.assertTrue(result.has_hourly)
                expected = (
                    {voc.removesuffix("_hourly_v2"), orphan.removesuffix("_hourly_v2")}
                    if current["resources"]
                    else set()
                )
                self.assertEqual(result.legacy_statistic_ids, expected)
        self.assertEqual(self.store.value, saved)
        self.assertEqual(self.store.calls, saves)
        self.assertEqual(self.recorder.reads, reads)

    async def test_partition_rejects_rebound_alias_account_and_marker_without_effects(
        self,
    ):
        await self.accept()
        await self.complete()
        saved = deepcopy(self.store.value)
        submissions = deepcopy(self.recorder.submissions)
        original_marker = deepcopy(self.entry.data[manager.MARKER])
        rebound = identity()
        next(iter(rebound["resources"].values()))["thermostat_id"] = 2
        other_account = {**identity(), "account_anchors": ["different-account"]}
        for current in (rebound, other_account):
            with (
                self.subTest(identity=current),
                self.assertRaises(manager.HourlyImportError),
            ):
                await self.fresh().async_writer_partition(current)
        for marker in (None, {"version": 3, "token": "a" * 64}):
            self.entry.data[manager.MARKER] = marker
            with (
                self.subTest(marker=marker),
                self.assertRaises(manager.HourlyImportError),
            ):
                await self.fresh().async_writer_partition(identity())
        self.entry.data[manager.MARKER] = original_marker
        self.assertEqual(self.store.value, saved)
        self.assertEqual(self.recorder.submissions, submissions)

    async def test_blocked_history_preserves_unrelated_daily_writer(
        self,
    ):
        await self.accept()
        self.store.fail_read_kind = "proof"
        with self.assertRaises(OSError):
            await self.writer.async_advance_history(context=self.context)
        current = identity()
        current["resources"]["beestat:zone_cool_runtime_hours_hourly_v2"] = {
            "thermostat_id": 1,
            "sensor_id": None,
            "quantity": "cool_runtime_hours",
        }
        partition = await self.writer.async_writer_partition(current)
        self.assertEqual(partition.hourly_blocked_reason, "history_operation_pending")
        self.assertEqual(
            partition.legacy_statistic_ids, {"beestat:zone_cool_runtime_hours"}
        )


class SupersessionContractionTest(unittest.TestCase):
    def test_retired_acquisitions_leave_the_closure_among_the_rest(self):
        resource = "runtime_thermostat:1"
        relation = {
            resource: {"b": ["a"], "c": ["b"], "d": ["c"], "e": ["b"]},
            "runtime_sensor:2": {"y": ["x"]},
        }
        retired = {resource: ["b", "c"], "runtime_sensor:2": ["x"]}
        result = writer._contract(relation, retired)
        self.assertEqual(result, {resource: {"d": ["a"], "e": ["a"]}})
        self.assertEqual(relation[resource]["c"], ["b"])
        roots = {resource: {"a", "d", "e"}}
        kept = {
            name: older - {"b", "c"}
            for name, older in sources._provider_precedence(
                {}, relation, {resource: {"a", "b", "c", "d", "e"}}
            )[resource].items()
            if name in roots[resource]
        }
        self.assertEqual(
            sources._provider_precedence({}, result, roots)[resource], kept
        )
