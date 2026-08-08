"""Bound repeated background-work requests to one pending follow-up."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

type TaskFactory = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]


class CoalescingTaskScheduler:
    """Run at most one task while retaining one request made during that run."""

    def __init__(
        self,
        run: Callable[[], Coroutine[Any, Any, None]],
        create_task: TaskFactory,
    ) -> None:
        self._run = run
        self._create_task = create_task
        self._pending = False
        self._task: asyncio.Task[None] | None = None

    def schedule(self) -> asyncio.Task[None]:
        """Schedule work or coalesce it into the current task's next pass."""

        self._pending = True
        if self._task is None or self._task.done():
            self._task = self._create_task(self._async_drain())
        return self._task

    async def _async_drain(self) -> None:
        try:
            while self._pending:
                self._pending = False
                await self._run()
        finally:
            self._task = None
