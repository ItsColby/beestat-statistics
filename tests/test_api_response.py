"""Tests for Beestat API response normalization."""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class ApiResponseTest(unittest.IsolatedAsyncioTestCase):
    """Validate Beestat response helpers without requiring aiohttp."""

    def setUp(self) -> None:
        self._old_modules = {key: sys.modules.get(key) for key in ("aiohttp",)}
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

    def test_non_auth_api_errors_do_not_expose_remote_payloads(self) -> None:
        secret = "remote-response-secret"
        payloads = (
            ({"error": {"detail": secret}}, "returned an error"),
            (
                {"success": False, "message": secret},
                "returned an unsuccessful response",
            ),
        )

        for payload, expected in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    self.api.BeestatApiError, expected
                ) as raised:
                    self.api._unwrap_response(payload, "thermostat", "read_id")
                self.assertNotIn(secret, str(raised.exception))

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

    def test_client_error_messages_are_bounded_for_ha_state(self) -> None:
        client = self.api.BeestatClient(
            object(),
            "secret-token",
            "https://api.test/",
        )

        self.assertEqual(
            client.redact_error(RuntimeError("network-response-secret")),
            "Beestat network request failed",
        )
        self.assertEqual(
            client.redact_error(KeyError("unexpected-response-secret")),
            "Unexpected integration error (KeyError)",
        )

    def test_exception_fingerprint_is_useful_without_exception_content(self) -> None:
        err = RuntimeError("private-response-secret")
        frame = traceback.FrameSummary(str(ROOT / "synthetic.py"), 17, "fail")
        with patch.object(self.api.traceback, "extract_tb", return_value=[frame]):
            fingerprint = self.api.exception_fingerprint(err)

        self.assertEqual("RuntimeError@synthetic:fail:17", fingerprint)
        self.assertNotIn("private-response-secret", fingerprint)

    def test_exception_fingerprint_is_sanitized_and_bounded(self) -> None:
        exception_type = type("Private!" + ("x" * 256), (RuntimeError,), {})
        err = exception_type("private-response-secret")
        frame = traceback.FrameSummary(
            str(ROOT / (("module!" + ("y" * 256)) + ".py")),
            17,
            "fail!" + ("z" * 256),
        )
        with patch.object(self.api.traceback, "extract_tb", return_value=[frame]):
            fingerprint = self.api.exception_fingerprint(err)

        self.assertLessEqual(len(fingerprint), 160)
        self.assertNotIn("!", fingerprint)
        self.assertNotIn("private-response-secret", fingerprint)

    def test_sync_true_response_is_success_without_rows(self) -> None:
        self.assertEqual(self.api._normalize_rows(True, allow_boolean=True), [])

    async def test_sync_false_response_is_retried_before_success(self) -> None:
        session = _FakeSession([False, True])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=2,
        )

        with patch.object(self.api.asyncio, "sleep", new=AsyncMock()):
            self.assertEqual(await client.async_sync_resource("runtime"), [])

        self.assertEqual(session.call_count, 2)

    async def test_http_error_does_not_expose_response_body(self) -> None:
        secret = "http-response-secret"
        session = _FakeSession(
            [_FakeResponse({}, status=500, text=f"failure: {secret}")]
        )
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=1,
        )

        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            r"Failed Beestat call thermostat\.read_id: "
            r"thermostat\.read_id returned HTTP 500",
        ) as raised:
            await client.async_read_id("thermostat")

        self.assertNotIn(secret, str(raised.exception))

    async def test_invalid_json_error_does_not_expose_parser_detail(self) -> None:
        secret = "parser-response-secret"
        session = _FakeSession([_FakeResponse({}, json_error=ValueError(secret))])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=1,
        )

        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            r"Failed Beestat call thermostat\.read_id: "
            r"Beestat returned invalid response data",
        ) as raised:
            await client.async_read_id("thermostat")

        self.assertNotIn(secret, str(raised.exception))

    async def test_response_body_is_rejected_at_configured_size_limit(self) -> None:
        session = _FakeSession([{"data": [{"value": "x" * 64}]}])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=1,
            max_response_bytes=32,
        )

        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            "response exceeded the size limit",
        ):
            await client.async_read_id("thermostat")

    async def test_streamed_body_limit_does_not_require_content_length(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse(
                    {"data": [{"value": "x" * 64}]},
                    include_content_length=False,
                )
            ]
        )
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=1,
            max_response_bytes=32,
        )

        with self.assertRaisesRegex(
            self.api.BeestatApiError,
            "response exceeded the size limit",
        ):
            await client.async_read_id("thermostat")

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


class _FakeResponse:
    def __init__(
        self,
        payload,
        *,
        status: int = 200,
        text: str | None = None,
        json_error: Exception | None = None,
        include_content_length: bool = True,
    ) -> None:
        self.status = status
        self._payload = payload
        self._text = text
        self._json_error = json_error
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.content = _FakeContent(body, json_error)
        self.content_length = len(body) if include_content_length else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    async def json(self, *, content_type=None):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def text(self) -> str:
        return self._text if self._text is not None else str(self._payload)


class _FakeContent:
    def __init__(self, body: bytes, read_error: Exception | None = None) -> None:
        self._body = body
        self._read_error = read_error

    async def iter_chunked(self, size: int):
        if self._read_error is not None:
            raise self._read_error
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _FakeSession:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = iter(payloads)
        self.call_count = 0

    def get(self, _url, *, params):
        self.call_count += 1
        response = next(self._payloads)
        return (
            response if isinstance(response, _FakeResponse) else _FakeResponse(response)
        )


if __name__ == "__main__":
    unittest.main()
