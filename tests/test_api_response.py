"""Tests for Beestat API response normalization."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_api_test"


def _load_api_module():
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.api", ROOT / "api.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load api")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ApiResponseTest(unittest.TestCase):
    """Validate Beestat response helpers without requiring aiohttp."""

    def setUp(self) -> None:
        self._old_modules = {
            key: sys.modules.get(key)
            for key in (
                "aiohttp",
            )
        }
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientError = RuntimeError
        aiohttp.ClientSession = object
        sys.modules["aiohttp"] = aiohttp
        self.api = _load_api_module()

    def tearDown(self) -> None:
        for key, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_error_true_with_auth_message_starts_reauth_path(self) -> None:
        with self.assertRaises(self.api.BeestatAuthError):
            self.api._unwrap_response(
                {"success": False, "error": True, "message": "Invalid API key"},
                "thermostat",
                "read_id",
            )

    def test_error_dict_with_auth_detail_starts_reauth_path(self) -> None:
        with self.assertRaises(self.api.BeestatAuthError):
            self.api._unwrap_response(
                {
                    "error": {
                        "code": "forbidden",
                        "detail": "API key does not have permission",
                    }
                },
                "thermostat",
                "read_id",
            )

    def test_response_body_redacts_api_key_and_api_base(self) -> None:
        replacements = self.api._redaction_replacements(
            api_key="secret-token",
            api_base="https://api.test/",
        )

        self.assertEqual(
            self.api._redact_text(
                "request failed for https://api.test/?api_key=secret-token",
                replacements,
            ),
            "request failed for <redacted-url>/?api_key=<redacted>",
        )

    def test_client_error_redaction_is_safe_for_ha_state(self) -> None:
        client = self.api.BeestatClient(
            object(),
            "secret-token",
            "https://api.test/",
        )

        self.assertEqual(
            client.redact_error(
                "Cannot connect to https://api.test/?api_key=secret-token"
            ),
            "Cannot connect to <redacted-url>/?api_key=<redacted>",
        )

    def test_sync_boolean_response_is_success_without_rows(self) -> None:
        self.assertEqual(self.api._normalize_rows(True, allow_boolean=True), [])
        self.assertEqual(self.api._normalize_rows(False, allow_boolean=True), [])

    def test_read_boolean_response_is_not_silently_empty(self) -> None:
        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            "Unexpected response data shape: bool",
        ):
            self.api._normalize_rows(True)

    def test_read_list_rows_must_be_objects(self) -> None:
        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            "Unexpected response row shape: str",
        ):
            self.api._normalize_rows([{"id": 1}, "bad-row"])

    def test_read_id_mapping_preserves_id_keys_when_rows_omit_them(self) -> None:
        self.assertEqual(
            self.api._normalize_rows(
                {
                    "1001": {"name": "Zone A"},
                    "2002": {"id": 2002, "name": "Second Zone"},
                }
            ),
            [
                {"id": "1001", "name": "Zone A"},
                {"id": 2002, "name": "Second Zone"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
