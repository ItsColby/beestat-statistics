"""Detached native Recorder rows and explicitly complete snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class HourlyStatisticRow:
    """A detached normalized row; timestamps are aware whole UTC hours."""

    start: datetime
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    state: float | None = None
    sum: float | None = None

    @property
    def cleared(self) -> bool:
        """Identify native start-only invalidation, never a numeric observation."""

        return all(
            value is None
            for value in (self.mean, self.min, self.max, self.state, self.sum)
        )


@dataclass(frozen=True, slots=True)
class RecorderSnapshot:
    """Native metadata and rows with explicit bounded acquisition coverage.

    Complete means every row in the requested range was acquired, including an
    authoritative empty result for a new ID. All metadata-supported numeric fields
    must be acquired, with unsupported fields represented as None. A missing
    required projection cannot be interpreted as a cleared row. The native adapter
    establishes completeness through its serialized read; callers fence changes
    to the snapshot before authorizing effects.
    """

    rows: tuple[HourlyStatisticRow, ...] = ()
    metadata: Mapping[str, Any] | None = None
    complete: bool = False
