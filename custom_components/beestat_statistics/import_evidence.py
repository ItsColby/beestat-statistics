"""Bounded evidence for partial Recorder import results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MAX_SKIPPED_WINDOW_EXAMPLES = 3
# Consecutive non-size failures (no successful read in between) tolerated before
# a pass aborts. A single failing day inside a MAX_WINDOW_DAYS window fails once
# per bisection level (30, 15, 7.5, 3.75, 1.875 and 0.94 days), so the budget
# must exceed that depth for the day to reach skip-with-evidence.
MAX_TRANSIENT_WINDOW_FAILURES = 8

type SkippedWindowResource = Literal["runtime_sensor", "runtime_thermostat"]


@dataclass(slots=True)
class SkippedWindowEvidence:
    """Count skipped source windows while retaining bounded safe examples."""

    total_count: int = 0
    runtime_sensor_count: int = 0
    runtime_thermostat_count: int = 0
    transient_failure_count: int = 0  # consecutive; reset by a successful read
    _examples: list[dict[str, str]] = field(default_factory=list)

    @property
    def examples(self) -> tuple[dict[str, str], ...]:
        """Return private-identifier-free examples for state and diagnostics."""

        return tuple(dict(item) for item in self._examples)

    def record(
        self,
        resource: SkippedWindowResource,
        *,
        start: str,
        end: str,
    ) -> None:
        """Record one skipped source window without retaining its numeric ID."""

        self.total_count += 1
        if resource == "runtime_sensor":
            self.runtime_sensor_count += 1
        else:
            self.runtime_thermostat_count += 1
        example = {
            "resource": resource,
            "start": start,
            "end": end,
        }
        if (
            len(self._examples) < MAX_SKIPPED_WINDOW_EXAMPLES
            and example not in self._examples
        ):
            self._examples.append(example)
