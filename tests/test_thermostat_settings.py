"""Tests for the privacy-safe Beestat Ecobee settings projection."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_thermostat_settings_test"


def _load_module():
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.thermostat_settings",
        ROOT / "thermostat_settings.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load thermostat_settings")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ThermostatSettingsTest(unittest.TestCase):
    """Validate mapping, units, and confidentiality boundaries."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = _load_module()

    def test_projection_keeps_useful_scalars_and_rejects_private_objects(self) -> None:
        snapshots = self.settings.build_thermostat_settings_snapshots(
            (
                {
                    "thermostat_id": 1001,
                    "ecobee_thermostat_id": 2002,
                },
            ),
            [
                {
                    "ecobee_thermostat_id": 2002,
                    "identifier": "private-serial",
                    "location": {"address": "private address"},
                    "electricity": {"billing": "private billing"},
                    "notification_settings": {"email": "person@example.test"},
                    "settings": {
                        "autoAway": True,
                        "followMeComfort": False,
                        "compressorProtectionMinTemp": 350,
                        "compressorProtectionMinTime": 300,
                        "backlightOffTime": 10,
                        "randomStartDelayCool": 1,
                        "randomStartDelayHeat": 0,
                        "humidity": "36",
                        "userAccessCode": "1234",
                        "groupName": "private group",
                        "electricityBillingDayOfMonth": 17,
                        "futureField": "private future",
                    },
                    "audio": {
                        "microphoneEnabled": True,
                        "playbackVolume": 50,
                        "voiceEngines": [{"id": "private voice account"}],
                    },
                }
            ],
        )

        snapshot = snapshots[1001]
        self.assertTrue(self.settings.boolean_setting(snapshot, "autoAway"))
        self.assertFalse(self.settings.boolean_setting(snapshot, "followMeComfort"))
        self.assertEqual(
            self.settings.temperature_fahrenheit(
                snapshot,
                "compressorProtectionMinTemp",
            ),
            35.0,
        )
        self.assertEqual(
            snapshot.source_details["temperature_and_staging"][
                "compressorProtectionMinTime"
            ],
            {"value": 300, "unit": "s"},
        )
        self.assertEqual(
            snapshot.source_details["humidity_and_ventilation"]["humidity"],
            {"value": "36", "unit": "%"},
        )
        self.assertEqual(
            snapshot.source_details["display_and_access"]["backlightOffTime"],
            {"value": 10, "unit": "s"},
        )
        self.assertEqual(
            snapshot.source_details["temperature_and_staging"]["randomStartDelayCool"],
            1,
        )
        self.assertEqual(
            snapshot.source_details["temperature_and_staging"]["randomStartDelayHeat"],
            0,
        )
        serialized = repr(snapshot)
        for excluded in (
            "private-serial",
            "private address",
            "private billing",
            "person@example.test",
            "1234",
            "private group",
            "private future",
            "private voice account",
            "electricityBillingDayOfMonth",
        ):
            self.assertNotIn(excluded, serialized)

    def test_inactive_and_unmatched_rows_are_ignored(self) -> None:
        snapshots = self.settings.build_thermostat_settings_snapshots(
            ({"thermostat_id": 1, "ecobee_thermostat_id": 2},),
            [
                {
                    "ecobee_thermostat_id": 2,
                    "inactive": True,
                    "settings": {"autoAway": True},
                },
                {
                    "ecobee_thermostat_id": 3,
                    "settings": {"autoAway": True},
                },
            ],
        )

        self.assertEqual(snapshots, {})


if __name__ == "__main__":
    unittest.main()
