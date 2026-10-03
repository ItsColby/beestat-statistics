"""Tests for bounded background-work coalescing."""

from __future__ import annotations

import asyncio
import unittest

from custom_components.beestat_statistics import task_coalescer


class TaskCoalescerTest(unittest.IsolatedAsyncioTestCase):
    """Validate that bursts retain only one bounded follow-up run."""

    async def test_burst_is_coalesced_into_running_and_one_follow_up(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0
        tasks: list[asyncio.Task[None]] = []

        async def run() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()

        def create_task(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        scheduler = task_coalescer.CoalescingTaskScheduler(run, create_task)
        task = scheduler.schedule()
        scheduler.schedule()
        scheduler.schedule()
        await first_started.wait()
        scheduler.schedule()
        scheduler.schedule()
        release_first.set()
        await task

        self.assertEqual(calls, 2)
        self.assertEqual(len(tasks), 1)

    async def test_cancelled_work_does_not_run_pending_follow_up(self) -> None:
        started = asyncio.Event()
        calls = 0

        async def run() -> None:
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.Event().wait()

        scheduler = task_coalescer.CoalescingTaskScheduler(run, asyncio.create_task)
        task = scheduler.schedule()
        await started.wait()
        scheduler.schedule()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, 1)

    async def test_failed_work_propagates_and_a_new_request_can_recover(self) -> None:
        calls = 0

        async def run() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("synthetic failure")

        scheduler = task_coalescer.CoalescingTaskScheduler(run, asyncio.create_task)
        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            await scheduler.schedule()
        await scheduler.schedule()
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
