"""Tests for the exact Home Assistant lane dependency verifier."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scripts import check_ha_test_dependencies


class HomeAssistantTestDependenciesTest(unittest.TestCase):
    """Validate the narrow upstream patch-version exception."""

    @staticmethod
    def _version(package: str) -> str:
        return {
            "homeassistant": check_ha_test_dependencies.CORE_VERSION,
            "pytest-homeassistant-custom-component": (
                check_ha_test_dependencies.HARNESS_VERSION
            ),
        }[package]

    def test_accepts_only_verified_harness_patch_skew(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=f"{check_ha_test_dependencies.EXPECTED_PATCH_SKEW}\n",
            stderr="",
        )

        with (
            patch.object(
                check_ha_test_dependencies.importlib.metadata,
                "version",
                side_effect=self._version,
            ),
            patch.object(
                check_ha_test_dependencies.subprocess,
                "run",
                return_value=result,
            ),
        ):
            self.assertEqual(check_ha_test_dependencies.main(), 0)

    def test_rejects_an_additional_dependency_conflict(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                f"{check_ha_test_dependencies.EXPECTED_PATCH_SKEW}\n"
                "another-package has requirement other==1, but you have other 2.\n"
            ),
            stderr="",
        )

        with (
            patch.object(
                check_ha_test_dependencies.importlib.metadata,
                "version",
                side_effect=self._version,
            ),
            patch.object(
                check_ha_test_dependencies.subprocess,
                "run",
                return_value=result,
            ),
        ):
            self.assertEqual(check_ha_test_dependencies.main(), 1)
