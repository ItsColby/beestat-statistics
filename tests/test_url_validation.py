"""Tests for the Beestat credential-transport URL boundary."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_url_validation_test"


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


url_validation = _load_module("url_validation")


class UrlValidationTest(unittest.TestCase):
    """Require a bounded HTTPS API origin before credentials can be used."""

    def test_normalizes_valid_https_url(self) -> None:
        self.assertEqual(
            url_validation.normalize_api_base(" https://api.example.test/v1/ "),
            "https://api.example.test/v1/",
        )

    def test_rejects_insecure_or_ambiguous_urls(self) -> None:
        for value in (
            "http://api.example.test/",
            "https://user@example.test/",
            "https://api.example.test/#fragment",
            "https://api.example.test/#",
            "https://api.example.test/?mode=test",
            "https://api.example.test/?",
            "https:///missing-host",
            "https://api.example.test\\ambiguous",
            "https://api%2f.example.test/",
            "https://api.example.test:0/",
            "https://api.example.test/has space",
            "https://api.example.test/line\nbreak",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                url_validation.normalize_api_base(value)


if __name__ == "__main__":
    unittest.main()
