"""Regression tests for source-coded thermostat alert classification."""

from __future__ import annotations

import unittest
from itertools import permutations
from pathlib import Path

if __package__:
    from ._module_loader import load_module
else:
    from _module_loader import load_module

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
alerts = load_module(ROOT, "beestat_statistics_alert_test", "alerts")


class AlertClassificationTest(unittest.TestCase):
    """Keep documented reminders distinct from faults and unknown alerts."""

    def test_documented_reminder_codes_override_equipment_words(self) -> None:
        for code in (3130, 3131, 3132, 3133, 3134, 3135, 3136, 3137, 3138, 3140):
            for value in (code, str(code)):
                with self.subTest(code=value):
                    alert = {"code": value, "text": "Furnace cooling maintenance"}
                    self.assertEqual(
                        alerts.classify_active_alerts((alert,)), "maintenance"
                    )
                    self.assertEqual(
                        alerts.active_alert_examples((alert,))[0]["category"],
                        "maintenance",
                    )

    def test_reminder_code_works_without_text(self) -> None:
        self.assertEqual(
            alerts.classify_active_alerts(({"code": " 3130 "},)), "maintenance"
        )

    def test_unknown_numeric_code_is_not_assumed_to_be_maintenance(self) -> None:
        for code in (3139, "9999"):
            with self.subTest(code=code):
                self.assertEqual(
                    alerts.classify_active_alerts(
                        ({"code": code, "text": "Service or replace component"},)
                    ),
                    "unknown",
                )

    def test_fault_with_maintenance_words_stays_equipment(self) -> None:
        self.assertEqual(
            alerts.classify_active_alerts(
                ({"code": 1006, "text": "Cooling problem; service required"},)
            ),
            "equipment",
        )

    def test_separate_fault_and_unknown_alerts_outrank_reminders(self) -> None:
        maintenance = {"code": 3130, "text": "Furnace maintenance reminder"}
        unknown = {"code": 3139, "text": "Maintenance message"}
        equipment = {"code": 1003, "text": "Furnace fault"}
        for combined in permutations((maintenance, unknown)):
            self.assertEqual(alerts.classify_active_alerts(combined), "unknown")
        for combined in permutations((maintenance, unknown, equipment)):
            self.assertEqual(alerts.classify_active_alerts(combined), "equipment")

    def test_legacy_text_and_empty_alert_contracts_remain(self) -> None:
        self.assertEqual(alerts.classify_active_alerts(()), "none")
        self.assertEqual(
            alerts.classify_active_alerts(({"text": "Replace filter"},)),
            "maintenance",
        )
        self.assertEqual(alerts.classify_active_alerts(({},)), "unknown")


if __name__ == "__main__":
    unittest.main()
