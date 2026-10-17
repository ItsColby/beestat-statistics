"""Synthetic source and persistence fixtures for qualified history tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from custom_components.beestat_statistics import hourly_statistics as builder

HOUR = timedelta(hours=1)
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
