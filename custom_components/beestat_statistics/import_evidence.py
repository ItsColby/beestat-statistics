"""Bounded evidence for partial Recorder import results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MAX_SKIPPED_WINDOW_EXAMPLES = 3

type SkippedWindowResource = Literal["runtime_sensor", "runtime_thermostat"]


@dataclass(slots=True)
class SkippedWindowEvidence:
    """Count skipped source windows while retaining bounded safe examples."""

    total_count: int = 0
    runtime_sensor_count: int = 0
    runtime_thermostat_count: int = 0
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
