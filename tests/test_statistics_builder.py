"""Tests for pure Beestat statistics construction."""

from __future__ import annotations

import importlib.util
import math
import sys
import types
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_module("const")
config_model = _load_module("config_model")
statistics_builder = _load_module("statistics_builder")


class StatisticsBuilderTest(unittest.TestCase):
    """Validate daily external statistic shape and accumulation."""

    def setUp(self) -> None:
        self.local_tz = ZoneInfo("America/New_York")
        self.config = config_model.BeestatConfig(
            thermostats=(
                config_model.ConfiguredThermostat(
                    thermostat_id=1,
                    slug="zone_a",
                    name="Zone A",
                ),
            ),
            sensors=(
                config_model.ConfiguredSensor(
                    sensor_id=10,
                    slug="room_sensor_a",
                    name="Room Sensor A",
                    thermostat_id=1,
                    thermostat_slug="zone_a",
                    include_temperature=True,
                    include_air_quality=False,
                    include_co2=False,
                    include_voc=False,
                    occupancy_entity_id="binary_sensor.room_sensor_a_occupancy",
                ),
            ),
        )

    def test_runtime_statistics_are_cumulative_by_local_day(self) -> None:
        rows = [
            {
                "thermostat_id": True,
                "date": "2026-07-03",
                "sum_compressor_cool_1": 9999,
            },
            {
                "thermostat_id": 1.5,
                "date": "2026-07-04",
                "sum_compressor_cool_1": 9999,
            },
            {
                "thermostat_id": float("inf"),
                "date": "2026-07-01",
                "sum_compressor_cool_1": 9999,
            },
            {
                "thermostat_id": 1,
                "date": "2026-07-01",
                "sum_compressor_cool_1": 3600,
                "sum_compressor_cool_2": 0,
                "sum_fan": 1800,
            },
            {
                "thermostat_id": 1,
                "date": "2026-07-02",
                "sum_compressor_cool_1": 1800,
                "sum_compressor_cool_2": 1800,
                "sum_fan": 3600,
            },
            {
                "thermostat_id": 1,
                "date": "not-a-date",
                "sum_compressor_cool_1": 9999,
                "sum_compressor_cool_2": 9999,
                "sum_fan": 9999,
            },
        ]

        series = statistics_builder.build_runtime_statistics(
            rows,
            self.local_tz,
            self.config,
        )

        cool = _series(series, "beestat:zone_a_cool_runtime_hours")
        self.assertEqual(cool.metadata["name"], "Beestat Zone A Cool Runtime")
        self.assertNotIn("has_mean", cool.metadata)
        self.assertEqual(cool.metadata["mean_type"], 0)
        self.assertEqual(cool.metadata["unit_class"], "duration")
        self.assertEqual(
            [item["state"] for item in cool.statistics],
            [1.0, 2.0],
        )
        self.assertEqual(
            [item["start"] for item in cool.statistics],
            [
                datetime(2026, 7, 1, tzinfo=self.local_tz),
                datetime(2026, 7, 2, tzinfo=self.local_tz),
            ],
        )

    def test_summary_statistics_use_last_daily_row_and_reject_nonfinite_values(
        self,
    ) -> None:
        """Duplicate starts and non-finite numbers must not reach Recorder."""

        rows = [
            {
                "thermostat_id": 1,
                "date": "2026-07-01",
                "sum_compressor_cool_1": 3600,
                "sum_heating_degree_days": 1,
                "avg_indoor_humidity": 45,
            },
            {
                "thermostat_id": 1,
                "date": "2026-07-01",
                "sum_compressor_cool_1": 7200,
                "sum_heating_degree_days": "Infinity",
                "avg_indoor_humidity": "NaN",
            },
            {
                "thermostat_id": 1,
                "date": "2026-07-02",
                "sum_compressor_cool_1": 3600,
            },
            {
                "thermostat_id": 1,
                "date": "2026-07-02",
                "deleted": True,
            },
        ]

        runtime = statistics_builder.build_runtime_statistics(
            rows,
            self.local_tz,
            self.config,
        )
        summary_sum = statistics_builder.build_summary_sum_statistics(
            rows,
            self.local_tz,
            self.config,
        )
        summary_mean = statistics_builder.build_summary_mean_statistics(
            rows,
            self.local_tz,
            self.config,
        )

        cool = _series(runtime, "beestat:zone_a_cool_runtime_hours")
        heating_degree_days = _series(
            summary_sum,
            "beestat:zone_a_heating_degree_days",
        )
        self.assertEqual(len(cool.statistics), 1)
        self.assertEqual(cool.statistics[0]["state"], 2.0)
        self.assertEqual(heating_degree_days.statistics, [])
        self.assertFalse(
            any(
                item.statistic_id == "beestat:zone_a_indoor_humidity"
                for item in summary_mean
            )
        )

    def test_sensor_statistics_group_points_by_local_day(self) -> None:
        rows_by_id = {
            10: [
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-01T04:30:00Z",
                    "temperature": 70,
                    "occupancy": True,
                },
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-01T05:30:00Z",
                    "temperature": 74,
                    "occupancy": False,
                },
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-02T04:30:00Z",
                    "temperature": 68,
                    "occupancy": 1,
                },
                {"sensor_id": 10, "timestamp": "not-a-timestamp", "temperature": 120},
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-01T06:30:00Z",
                    "temperature": True,
                },
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-02T05:30:00Z",
                    "temperature": "NaN",
                },
                {
                    "sensor_id": 10,
                    "timestamp": "2026-07-02T06:30:00Z",
                    "temperature": "Infinity",
                },
            ]
        }

        series = statistics_builder.build_sensor_statistics(
            rows_by_id,
            self.local_tz,
            self.config,
        )

        temperature = _series(series, "beestat:room_sensor_a_temperature")
        self.assertEqual(
            temperature.metadata["name"], "Beestat Room Sensor A Temperature"
        )
        self.assertNotIn("has_mean", temperature.metadata)
        self.assertEqual(temperature.metadata["mean_type"], 1)
        self.assertEqual(temperature.metadata["unit_class"], "temperature")
        self.assertEqual(
            temperature.metadata["unit_of_measurement"], "\N{DEGREE SIGN}F"
        )
        self.assertEqual(
            temperature.statistics,
            [
                {
                    "start": datetime(2026, 7, 1, tzinfo=self.local_tz),
                    "mean": 72.0,
                    "min": 70.0,
                    "max": 74.0,
                },
                {
                    "start": datetime(2026, 7, 2, tzinfo=self.local_tz),
                    "mean": 68.0,
                    "min": 68.0,
                    "max": 68.0,
                },
            ],
        )
        occupancy = _series(series, "beestat:room_sensor_a_occupancy")
        self.assertEqual(occupancy.metadata["unit_of_measurement"], "%")
        self.assertEqual(
            occupancy.statistics,
            [
                {
                    "start": datetime(2026, 7, 1, tzinfo=self.local_tz),
                    "mean": 50.0,
                    "min": 0.0,
                    "max": 100.0,
                },
                {
                    "start": datetime(2026, 7, 2, tzinfo=self.local_tz),
                    "mean": 100.0,
                    "min": 100.0,
                    "max": 100.0,
                },
            ],
        )

    def test_unrepresentable_local_timestamps_do_not_abort_valid_points(self) -> None:
        """UTC-to-local underflow must reject only the malformed source point."""

        rows = [
            {
                "timestamp": "0001-01-01T00:00:00Z",
                "temperature": 999,
                "setpoint_heat": 999,
            },
            {
                "timestamp": "2026-07-01T12:00:00Z",
                "temperature": 72,
                "setpoint_heat": 68,
            },
        ]
        room = statistics_builder.build_sensor_statistics(
            {10: rows}, self.local_tz, self.config
        )
        thermostat = statistics_builder.build_thermostat_point_statistics(
            {1: rows}, self.local_tz, self.config
        )
        self.assertEqual(
            _series(room, "beestat:room_sensor_a_temperature").statistics[0]["mean"], 72
        )
        self.assertEqual(
            _series(thermostat, "beestat:zone_a_heat_setpoint").statistics[0]["mean"],
            68,
        )
        self.assertTrue(all(item.source_rows == 1 for item in [*room, *thermostat]))

    def test_negative_cumulative_contribution_stops_only_affected_series(self) -> None:
        """Invalid negative durations or degree days cannot reverse Recorder sums."""

        rows = [
            {
                "thermostat_id": 1,
                "date": f"2026-07-0{day}",
                "sum_compressor_cool_1": -3600 if day == 2 else 3600,
                "sum_compressor_cool_2": -1,
                "sum_fan": 3600,
                "sum_heating_degree_days": -1 if day == 2 else 1,
            }
            for day in (1, 2, 3)
        ]
        runtime = statistics_builder.build_runtime_statistics(
            rows, self.local_tz, self.config
        )
        sums = statistics_builder.build_summary_sum_statistics(
            rows, self.local_tz, self.config
        )
        self.assertEqual(
            _series(runtime, "beestat:zone_a_cool_runtime_hours").statistics, []
        )
        for statistic_id in (
            "beestat:zone_a_cool_stage_1_runtime_hours",
            "beestat:zone_a_heating_degree_days",
        ):
            self.assertEqual(
                [
                    row["sum"]
                    for row in _series([*runtime, *sums], statistic_id).statistics
                ],
                [1],
            )
        self.assertEqual(
            [
                row["sum"]
                for row in _series(
                    runtime, "beestat:zone_a_fan_runtime_hours"
                ).statistics
            ],
            [1, 2, 3],
        )
        ids = statistics_builder.cumulative_statistic_ids(self.config, rows)
        self.assertNotIn("beestat:zone_a_cool_stage_2_runtime_hours", ids)

    def test_invalid_cumulative_contribution_stops_only_affected_series(self) -> None:
        """Explicit bad increments cannot be silently imported as zero activity."""

        for invalid in (
            None,
            "",
            "unknown",
            "unavailable",
            "malformed",
            "NaN",
            "Infinity",
            float("-inf"),
            True,
            False,
            10**1000,
        ):
            with self.subTest(invalid=invalid):
                rows = [
                    {
                        "thermostat_id": 1,
                        "date": f"2026-07-0{day}",
                        "sum_compressor_cool_1": invalid if day == 2 else 3600,
                        "sum_fan": 3600,
                        "sum_heating_degree_days": invalid if day == 2 else 1,
                        "sum_cooling_degree_days": 2,
                    }
                    for day in (1, 2, 3)
                ]
                series = statistics_builder.build_statistics(
                    rows, {}, {}, self.local_tz, self.config
                )
                for suffix in (
                    "cool_runtime_hours",
                    "cool_stage_1_runtime_hours",
                    "heating_degree_days",
                ):
                    self.assertEqual(
                        [
                            row["sum"]
                            for row in _series(
                                series, f"beestat:zone_a_{suffix}"
                            ).statistics
                        ],
                        [1],
                    )
                for suffix, expected in (
                    ("fan_runtime_hours", [1, 2, 3]),
                    ("cooling_degree_days", [2, 4, 6]),
                ):
                    self.assertEqual(
                        [
                            row["sum"]
                            for row in _series(
                                series, f"beestat:zone_a_{suffix}"
                            ).statistics
                        ],
                        expected,
                    )

    def test_existing_detailed_runtime_survives_an_all_zero_correction(self) -> None:
        """Only previously imported exact IDs survive without positive source rows."""

        existing_ids = {
            "beestat:zone_a_cool_stage_2_runtime_hours",
            "beestat:zone_a_humidifier_runtime_hours",
            "beestat:other_zone_ventilator_runtime_hours",
        }
        rows = [
            {
                "thermostat_id": 1,
                "date": "2026-07-01",
                "sum_compressor_cool_2": 0,
                "sum_humidifier": 0,
            },
            {"thermostat_id": 1, "date": "2026-07-02"},
        ]
        series = statistics_builder.build_statistics(
            rows,
            {},
            {},
            self.local_tz,
            self.config,
            existing_statistic_ids=existing_ids,
        )
        expected_ids = {
            "beestat:zone_a_cool_stage_2_runtime_hours",
            "beestat:zone_a_humidifier_runtime_hours",
        }
        detailed_ids = set(
            statistics_builder.detailed_runtime_statistic_ids(self.config)
        )
        self.assertEqual(len(detailed_ids), 10)
        self.assertEqual(
            {item.statistic_id for item in series} & detailed_ids, expected_ids
        )
        self.assertEqual(
            set(
                statistics_builder.cumulative_statistic_ids(
                    self.config, rows, existing_statistic_ids=existing_ids
                )
            )
            & detailed_ids,
            expected_ids,
        )
        for statistic_id in expected_ids:
            self.assertEqual(
                [row["sum"] for row in _series(series, statistic_id).statistics],
                [0, 0],
            )
        self.assertFalse(
            any(item.statistic_id.startswith("beestat:other_zone_") for item in series)
        )
        self.assertFalse(
            set(statistics_builder.cumulative_statistic_ids(self.config, rows))
            & detailed_ids
        )

    def test_retained_detailed_runtime_still_stops_at_explicit_invalid_input(
        self,
    ) -> None:
        """Recorder presence selects a series but never makes a bad counter zero."""

        for slug, _label, field in statistics_builder.DETAILED_RUNTIME_FIELDS:
            with self.subTest(slug=slug):
                statistic_id = f"beestat:zone_a_{slug}_runtime_hours"
                rows = [
                    {"thermostat_id": 1, "date": "2026-07-01", field: 0},
                    {"thermostat_id": 1, "date": "2026-07-02", field: None},
                    {"thermostat_id": 1, "date": "2026-07-03", field: 0},
                ]
                series = statistics_builder.build_runtime_statistics(
                    rows,
                    self.local_tz,
                    self.config,
                    existing_statistic_ids={statistic_id},
                )
                self.assertEqual(
                    [row["sum"] for row in _series(series, statistic_id).statistics],
                    [0],
                )

    def test_omitted_optional_cumulative_fields_preserve_zero_activity(self) -> None:
        """Omitted counters and explicit zero retain the supported sparse shape."""

        rows = [
            {
                "thermostat_id": 1,
                "date": "2026-07-01",
                "sum_compressor_cool_1": 3600,
                "sum_heating_degree_days": 1,
            },
            {"thermostat_id": 1, "date": "2026-07-02"},
            {
                "thermostat_id": 1,
                "date": "2026-07-03",
                "sum_compressor_cool_1": 0,
                "sum_heating_degree_days": 0,
            },
        ]
        series = statistics_builder.build_statistics(
            rows, {}, {}, self.local_tz, self.config
        )
        for suffix in (
            "cool_runtime_hours",
            "cool_stage_1_runtime_hours",
            "heating_degree_days",
        ):
            self.assertEqual(
                [
                    row["sum"]
                    for row in _series(series, f"beestat:zone_a_{suffix}").statistics
                ],
                [1, 1, 1],
            )

    def test_runtime_breakdowns_are_emitted_only_for_observed_hardware(self) -> None:
        series = statistics_builder.build_runtime_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": "2026-07-01",
                    "sum_compressor_cool_1": 3600,
                    "sum_compressor_cool_2": 0,
                    "sum_humidifier": 900,
                    "sum_dehumidifier": 0,
                },
                {
                    "thermostat_id": 1,
                    "date": "2026-07-02",
                    "sum_compressor_cool_1": 1800,
                    "sum_compressor_cool_2": 0,
                    "sum_humidifier": 0,
                    "sum_dehumidifier": 0,
                },
            ],
            self.local_tz,
            self.config,
        )

        cool_stage_1 = _series(series, "beestat:zone_a_cool_stage_1_runtime_hours")
        humidifier = _series(series, "beestat:zone_a_humidifier_runtime_hours")
        self.assertEqual([row["state"] for row in cool_stage_1.statistics], [1.0, 1.5])
        self.assertEqual([row["state"] for row in humidifier.statistics], [0.25, 0.25])
        self.assertFalse(
            any(
                item.statistic_id
                in {
                    "beestat:zone_a_cool_stage_2_runtime_hours",
                    "beestat:zone_a_dehumidifier_runtime_hours",
                }
                for item in series
            )
        )

    def test_summary_and_setpoint_statistics_use_current_recorder_metadata(
        self,
    ) -> None:
        summary_series = statistics_builder.build_summary_mean_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": "2026-07-01",
                    "avg_indoor_humidity": 50,
                    "avg_outdoor_temperature": 80,
                    "min_outdoor_temperature": 72,
                    "max_outdoor_temperature": 88,
                    "avg_outdoor_humidity": 60,
                }
            ],
            self.local_tz,
            self.config,
        )
        point_series = statistics_builder.build_thermostat_point_statistics(
            {
                1: [
                    {
                        "thermostat_id": 1,
                        "timestamp": "2026-07-01T12:00:00Z",
                        "setpoint_heat": 68,
                        "setpoint_cool": 74,
                    }
                ]
            },
            self.local_tz,
            self.config,
        )

        outdoor = _series(summary_series, "beestat:zone_a_outdoor_temperature")
        humidity = _series(summary_series, "beestat:zone_a_indoor_humidity")
        heat_setpoint = _series(point_series, "beestat:zone_a_heat_setpoint")

        for item in (outdoor, humidity, heat_setpoint):
            self.assertNotIn("has_mean", item.metadata)

        self.assertEqual(outdoor.metadata["unit_class"], "temperature")
        self.assertEqual(outdoor.metadata["unit_of_measurement"], "\N{DEGREE SIGN}F")
        self.assertEqual(humidity.metadata["unit_class"], "unitless")
        self.assertEqual(humidity.metadata["unit_of_measurement"], "%")
        self.assertEqual(heat_setpoint.metadata["unit_class"], "temperature")
        self.assertEqual(
            heat_setpoint.metadata["unit_of_measurement"],
            "\N{DEGREE SIGN}F",
        )

    def test_impossible_temperatures_do_not_contaminate_daily_point_statistics(
        self,
    ) -> None:
        values = (-460, -500, -459.7, -40, 80, None, float("inf"), float("nan"))
        rows = [
            {
                "timestamp": f"2026-07-01T12:{index:02d}:00Z",
                "setpoint_heat": value,
                "setpoint_cool": value,
                "temperature": value,
                "occupancy": index % 2 == 0,
            }
            for index, value in enumerate(values)
        ]
        thermostat = statistics_builder.build_thermostat_point_statistics(
            {1: rows}, self.local_tz, self.config
        )
        sensor = statistics_builder.build_sensor_statistics(
            {10: rows}, self.local_tz, self.config
        )
        for item in (
            _series(thermostat, "beestat:zone_a_heat_setpoint"),
            _series(thermostat, "beestat:zone_a_cool_setpoint"),
            _series(sensor, "beestat:room_sensor_a_temperature"),
        ):
            self.assertEqual(item.source_rows, 3)
            self.assertEqual(
                item.statistics,
                [
                    {
                        "start": datetime(2026, 7, 1, tzinfo=self.local_tz),
                        "mean": -139.9,
                        "min": -459.7,
                        "max": 80,
                    }
                ],
            )
            self.assertEqual(item.metadata["unit_of_measurement"], "°F")
            self.assertEqual(item.metadata["unit_class"], "temperature")
        occupancy = _series(sensor, "beestat:room_sensor_a_occupancy")
        self.assertEqual(occupancy.source_rows, len(rows))
        self.assertEqual(occupancy.statistics[0]["mean"], 50)
        self.assertEqual(occupancy.statistics[0]["min"], 0)
        self.assertEqual(occupancy.statistics[0]["max"], 100)
        self.assertEqual(
            statistics_builder.build_thermostat_point_statistics(
                {1: [rows[0]]}, self.local_tz, self.config
            ),
            [],
        )

    def test_temperature_validation_follows_sensor_unit_scaling(self) -> None:
        spec = statistics_builder.build_sensor_specs(self.config)[0]
        with patch.object(
            statistics_builder,
            "build_sensor_specs",
            return_value=(replace(spec, scale=0.1),),
        ):
            series = statistics_builder.build_sensor_statistics(
                {
                    10: [
                        {"timestamp": "2026-07-01T12:00:00Z", "temperature": -4000},
                        {"timestamp": "2026-07-01T12:05:00Z", "temperature": -4600},
                    ]
                },
                self.local_tz,
                self.config,
            )
        self.assertEqual(series[0].source_rows, 1)
        self.assertEqual(series[0].statistics[0]["mean"], -400)

    def test_summary_temperature_mean_and_extrema_are_validated_independently(
        self,
    ) -> None:
        observations = (
            (-460, -40, 80),
            (-40, -460, -20),
            (-40, -50, -460),
            (-459.7, -459.7, -459.7),
            (None, None, None),
        )
        series = statistics_builder.build_summary_mean_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": f"2026-07-{day:02d}",
                    "avg_outdoor_temperature": mean,
                    "min_outdoor_temperature": minimum,
                    "max_outdoor_temperature": maximum,
                    "avg_indoor_humidity": 40,
                }
                for day, (mean, minimum, maximum) in enumerate(observations, 1)
            ],
            self.local_tz,
            self.config,
        )
        temperature = _series(series, "beestat:zone_a_outdoor_temperature")
        self.assertEqual(temperature.source_rows, 3)
        self.assertEqual(
            temperature.statistics,
            [
                {
                    "start": datetime(2026, 7, 2, tzinfo=self.local_tz),
                    "mean": -40,
                    "max": -20,
                },
                {
                    "start": datetime(2026, 7, 3, tzinfo=self.local_tz),
                    "mean": -40,
                    "min": -50,
                },
                {
                    "start": datetime(2026, 7, 4, tzinfo=self.local_tz),
                    "mean": -459.7,
                    "min": -459.7,
                    "max": -459.7,
                },
            ],
        )
        humidity = _series(series, "beestat:zone_a_indoor_humidity")
        self.assertEqual(humidity.source_rows, len(observations))
        self.assertTrue(all(row["mean"] == 40 for row in humidity.statistics))

    def test_historical_temperature_representation_allowance_is_bounded(self) -> None:
        for value in (-459.67, -459.7, -459.72, -459.72 - 5e-10):
            with self.subTest(valid=value):
                self.assertEqual(
                    statistics_builder._measurement_value(value, "temperature", "°F"),
                    value,
                )
        for value in (-459.72 - 1e-8, -460, -500):
            with self.subTest(invalid=value):
                self.assertIsNone(
                    statistics_builder._measurement_value(value, "temperature", "°F")
                )

    def test_cumulative_statistic_ids_include_runtime_and_summary_sums(self) -> None:
        self.assertEqual(
            statistics_builder.cumulative_statistic_ids(self.config),
            (
                "beestat:zone_a_cool_runtime_hours",
                "beestat:zone_a_heat_runtime_hours",
                "beestat:zone_a_fan_runtime_hours",
                "beestat:zone_a_heating_degree_days",
                "beestat:zone_a_cooling_degree_days",
            ),
        )
        self.assertEqual(
            statistics_builder.cumulative_statistic_ids(
                self.config,
                [
                    {
                        "thermostat_id": 1,
                        "date": "2026-07-01",
                        "sum_compressor_cool_1": 3600,
                        "sum_humidifier": 300,
                    }
                ],
            ),
            (
                "beestat:zone_a_cool_runtime_hours",
                "beestat:zone_a_heat_runtime_hours",
                "beestat:zone_a_fan_runtime_hours",
                "beestat:zone_a_cool_stage_1_runtime_hours",
                "beestat:zone_a_humidifier_runtime_hours",
                "beestat:zone_a_heating_degree_days",
                "beestat:zone_a_cooling_degree_days",
            ),
        )

    def test_complete_output_has_unique_series(self) -> None:
        """Every Recorder write identity is unique after normalized construction."""

        series = statistics_builder.build_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": "2026-07-01",
                    "sum_compressor_cool_1": 3600,
                    "sum_compressor_heat_1": 1800,
                    "sum_fan": 3600,
                    "sum_heating_degree_days": 1,
                    "sum_cooling_degree_days": 2,
                    "avg_indoor_humidity": 45,
                    "avg_outdoor_temperature": 80,
                    "avg_outdoor_humidity": 55,
                }
            ],
            {
                1: [
                    {
                        "thermostat_id": 1,
                        "timestamp": "2026-07-01T12:00:00Z",
                        "setpoint_heat": 68,
                        "setpoint_cool": 74,
                    }
                ]
            },
            {
                10: [
                    {
                        "sensor_id": 10,
                        "timestamp": "2026-07-01T12:00:00Z",
                        "temperature": 72,
                    }
                ]
            },
            self.local_tz,
            self.config,
        )

        statistic_ids = [item.statistic_id for item in series]
        self.assertEqual(len(statistic_ids), len(set(statistic_ids)))

    def test_derived_arithmetic_never_emits_nonfinite_values(self) -> None:
        """Finite source values cannot overflow into invalid Recorder rows."""

        series = statistics_builder.build_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": "2026-07-01",
                    "sum_compressor_cool_1": 1e308,
                    "sum_compressor_cool_2": 1e308,
                    "sum_heating_degree_days": 1e308,
                },
                {
                    "thermostat_id": 1,
                    "date": "2026-07-02",
                    "sum_compressor_cool_1": 3600,
                    "sum_heating_degree_days": 1e308,
                },
            ],
            {
                1: [
                    {
                        "thermostat_id": 1,
                        "timestamp": "2026-07-01T12:00:00Z",
                        "setpoint_heat": 1e308,
                    },
                    {
                        "thermostat_id": 1,
                        "timestamp": "2026-07-01T12:05:00Z",
                        "setpoint_heat": 1e308,
                    },
                ]
            },
            {
                10: [
                    {
                        "sensor_id": 10,
                        "timestamp": "2026-07-01T12:00:00Z",
                        "temperature": 1e308,
                    },
                    {
                        "sensor_id": 10,
                        "timestamp": "2026-07-01T12:05:00Z",
                        "temperature": 1e308,
                    },
                ]
            },
            self.local_tz,
            self.config,
        )

        cool_runtime = _series(series, "beestat:zone_a_cool_runtime_hours")
        heat_setpoint = _series(series, "beestat:zone_a_heat_setpoint")
        room_temperature = _series(series, "beestat:room_sensor_a_temperature")
        self.assertEqual(len(cool_runtime.statistics), 2)
        self.assertEqual(heat_setpoint.statistics[0]["mean"], 1e308)
        self.assertEqual(room_temperature.statistics[0]["mean"], 1e308)
        for item in series:
            for row in item.statistics:
                for key in ("state", "sum", "mean", "min", "max"):
                    if key in row:
                        self.assertTrue(math.isfinite(row[key]))

    def test_cumulative_seed_overflow_truncates_the_invalid_tail(self) -> None:
        """A seed offset must stop before an unrepresentable Recorder value."""

        start = datetime(2026, 7, 1, tzinfo=self.local_tz)
        series = [
            statistics_builder.StatisticsSeries(
                metadata={"statistic_id": "beestat:test", "has_sum": True},
                statistics=[
                    {"start": start, "state": 1e307, "sum": 1e307},
                    {"start": start.replace(day=2), "state": 1.7e308, "sum": 1.7e308},
                ],
                source_rows=2,
            )
        ]
        seeds = {
            "beestat:test": statistics_builder.CumulativeStatisticSeed(
                start=start.replace(day=30),
                state=1e307,
                sum=1e307,
            )
        }

        adjusted = statistics_builder.apply_cumulative_seeds(series, seeds)

        self.assertEqual(
            adjusted[0].statistics,
            [{"start": start, "state": 2e307, "sum": 2e307}],
        )

    def test_apply_cumulative_seeds_offsets_partial_window_series(self) -> None:
        series = statistics_builder.build_runtime_statistics(
            [
                {
                    "thermostat_id": 1,
                    "date": "2026-07-02",
                    "sum_compressor_cool_1": 3600,
                    "sum_compressor_cool_2": 0,
                },
                {
                    "thermostat_id": 1,
                    "date": "2026-07-03",
                    "sum_compressor_cool_1": 1800,
                    "sum_compressor_cool_2": 1800,
                },
            ],
            self.local_tz,
            self.config,
        )
        seeds = {
            "beestat:zone_a_cool_runtime_hours": (
                statistics_builder.CumulativeStatisticSeed(
                    start=datetime(2026, 7, 1, tzinfo=self.local_tz),
                    state=42.5,
                    sum=42.5,
                )
            )
        }

        adjusted = statistics_builder.apply_cumulative_seeds(series, seeds)

        cool = _series(adjusted, "beestat:zone_a_cool_runtime_hours")
        fan = _series(adjusted, "beestat:zone_a_fan_runtime_hours")
        self.assertEqual(
            [item["state"] for item in cool.statistics],
            [43.5, 44.5],
        )
        self.assertEqual(
            [item["sum"] for item in cool.statistics],
            [43.5, 44.5],
        )
        self.assertEqual(
            [item["state"] for item in fan.statistics],
            [0.0, 0.0],
        )


def _series(series, statistic_id: str):
    for item in series:
        if item.statistic_id == statistic_id:
            return item
    raise AssertionError(f"Missing statistic series {statistic_id}")


if __name__ == "__main__":
    unittest.main()
