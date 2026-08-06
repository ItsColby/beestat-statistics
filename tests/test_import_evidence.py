"""Tests for bounded partial-import evidence."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "beestat_statistics"
    / "import_evidence.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "beestat_import_evidence_test", MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load import_evidence")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SkippedWindowEvidenceTest(unittest.TestCase):
    """Validate bounded counts and identifier-free source examples."""

    def setUp(self) -> None:
        self.module = _load_module()

    def test_counts_every_window_but_retains_only_three_safe_examples(self) -> None:
        evidence = self.module.SkippedWindowEvidence()

        for index in range(5):
            evidence.record(
                "runtime_sensor" if index < 4 else "runtime_thermostat",
                start=f"2026-08-0{index + 1} 00:00:00",
                end=f"2026-08-0{index + 2} 00:00:00",
            )

        self.assertEqual(evidence.total_count, 5)
        self.assertEqual(evidence.runtime_sensor_count, 4)
        self.assertEqual(evidence.runtime_thermostat_count, 1)
        self.assertEqual(len(evidence.examples), 3)
        self.assertEqual(
            set(evidence.examples[0]),
            {"resource", "start", "end"},
        )
        self.assertNotIn("sensor_id", repr(evidence.examples))
        self.assertNotIn("thermostat_id", repr(evidence.examples))

    def test_duplicate_resource_windows_count_but_do_not_hide_other_examples(
        self,
    ) -> None:
        evidence = self.module.SkippedWindowEvidence()

        for _index in range(4):
            evidence.record(
                "runtime_sensor",
                start="2026-08-01 00:00:00",
                end="2026-08-02 00:00:00",
            )
        evidence.record(
            "runtime_thermostat",
            start="2026-08-02 00:00:00",
            end="2026-08-03 00:00:00",
        )

        self.assertEqual(evidence.total_count, 5)
        self.assertEqual(len(evidence.examples), 2)
        self.assertEqual(
            evidence.examples[-1]["resource"],
            "runtime_thermostat",
        )


if __name__ == "__main__":
    unittest.main()
