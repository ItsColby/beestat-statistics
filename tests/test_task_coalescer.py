"""Tests for bounded background-work coalescing."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_task_coalescer_test"


def _load_task_coalescer_module():
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.task_coalescer", ROOT / "task_coalescer.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load task_coalescer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TaskCoalescerTest(unittest.IsolatedAsyncioTestCase):
    """Validate that bursts retain only one bounded follow-up run."""

    async def test_burst_is_coalesced_into_running_and_one_follow_up(self) -> None:
        task_coalescer = _load_task_coalescer_module()
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


if __name__ == "__main__":
    unittest.main()
