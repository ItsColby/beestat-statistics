"""Regression tests for source-coded thermostat alert classification."""

from __future__ import annotations

import unittest
from itertools import permutations

from custom_components.beestat_statistics import alerts


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

    def test_source_aliases_share_the_canonical_classification(self) -> None:
        for field in ("code", "alertNumber"):
            for type_field in ("type", "notificationType", "source"):
                for code, expected in (
                    (0, "unknown"),
                    (9999, "unknown"),
                    (3137, "maintenance"),
                ):
                    with self.subTest(field=field, type_field=type_field, code=code):
                        alert = {field: code, type_field: "Filter sensor fault"}
                        self.assertEqual(
                            alerts.classify_active_alerts((alert,)), expected
                        )
                        example = alerts.active_alert_examples((alert,))[0]
                        self.assertEqual(example["category"], expected)
                        self.assertEqual(example["code"], str(code))
                        self.assertEqual(example["type"], "Filter sensor fault")

    def test_fault_with_maintenance_words_stays_equipment(self) -> None:
        self.assertEqual(
            alerts.classify_active_alerts(
                ({"code": 1006, "text": "Cooling problem; service required"},)
            ),
            "equipment",
        )

    def test_only_an_absent_or_empty_code_uses_the_source_alias(self) -> None:
        for code, expected in (
            (0, "unknown"),
            (None, "maintenance"),
            ("", "maintenance"),
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    alerts.classify_active_alerts(
                        ({"code": code, "alertNumber": 3137, "text": "Replace filter"},)
                    ),
                    expected,
                )

    def test_separate_fault_and_unknown_alerts_outrank_reminders(self) -> None:
        maintenance = {"code": 3130, "text": "Furnace maintenance reminder"}
        unknown = {"code": 3139, "text": "Maintenance message"}
        equipment = {"code": 1003, "text": "Furnace fault"}
        for combined in permutations((maintenance, unknown)):
            self.assertEqual(alerts.classify_active_alerts(combined), "unknown")
        for combined in permutations((maintenance, unknown, equipment)):
            self.assertEqual(alerts.classify_active_alerts(combined), "equipment")

    def test_remote_sensor_codes_are_not_equipment_alerts(self) -> None:
        for code, expected in ((1028, "sensor"), (1027, "info"), (1029, "info")):
            for field in ("code", "alertNumber"):
                for value in (code, f" {code} "):
                    with self.subTest(code=value, field=field):
                        alert = {field: value, "text": "Sensor cooling heating"}
                        self.assertEqual(
                            alerts.classify_active_alerts((alert,)), expected
                        )
                        self.assertEqual(
                            alerts.active_alert_examples((alert,))[0]["category"],
                            expected,
                        )

    def test_neighbouring_sensor_reading_codes_stay_unknown(self) -> None:
        for code in (1026, 1030, 1031, 1032, 1033):
            with self.subTest(code=code):
                self.assertEqual(
                    alerts.classify_active_alerts(({"code": code},)), "unknown"
                )

    def test_category_rank_keeps_faults_above_remote_sensor_alerts(self) -> None:
        equipment = {"code": 1006, "text": "Problem with cooling"}
        unknown = {"code": 3139}
        sensor = {"code": 1028, "text": "Remote sensor not communicating"}
        maintenance = {"code": 3130}
        info = {"code": 1029, "text": "Remote sensor re-established"}
        ranked = (equipment, unknown, sensor, maintenance, info)
        expected = ("equipment", "unknown", "sensor", "maintenance", "info")
        for index, category in enumerate(expected):
            for combined in permutations(ranked[index:]):
                with self.subTest(category=category, combined=combined):
                    self.assertEqual(alerts.classify_active_alerts(combined), category)

    def test_legacy_text_and_empty_alert_contracts_remain(self) -> None:
        self.assertEqual(alerts.classify_active_alerts(()), "none")
        self.assertEqual(
            alerts.classify_active_alerts(({"text": "Replace filter"},)),
            "maintenance",
        )
        self.assertEqual(alerts.classify_active_alerts(({},)), "unknown")
