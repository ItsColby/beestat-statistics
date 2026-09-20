"""Tests for Beestat API response normalization."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
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
        aiohttp.ClientError = type("ClientError", (Exception,), {})
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
            client.redact_error(
                self.api.aiohttp.ClientError("network-response-secret")
            ),
            "Beestat network request failed",
        )
        self.assertEqual(
            client.redact_error(KeyError("unexpected-response-secret")),
            "Unexpected integration error (KeyError)",
        )

    def test_client_rejects_insecure_api_base_before_transport_setup(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.api.BeestatClient(
                object(),
                "secret-token",
                "http://api.test/",
            )

    def test_exception_fingerprint_is_useful_without_exception_content(self) -> None:
        err = RuntimeError("private-response-secret")
        frame = traceback.FrameSummary(str(ROOT / "synthetic.py"), 17, "fail")
        with patch.object(self.api.traceback, "extract_tb", return_value=[frame]):
            fingerprint = self.api.exception_fingerprint(err)

        self.assertEqual("RuntimeError@synthetic:fail:17", fingerprint)

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

    async def test_failed_sync_distinguishes_envelope_and_boolean_false(self) -> None:
        cases = (
            ({"success": False, "data": False}, "unsuccessful_envelope"),
            ({"success": 0, "data": False}, "unsuccessful_envelope"),
            ({"success": True, "data": False}, "sync_false"),
            (False, "sync_false"),
        )
        for payload, failure in cases:
            with self.subTest(payload=payload):
                session = _FakeSession([payload, payload, payload])
                client = self.api.BeestatClient(
                    session, "secret-token", "https://api.test/"
                )
                sleep = AsyncMock()
                with (
                    patch.object(self.api.asyncio, "sleep", new=sleep),
                    self.assertRaises(self.api.BeestatApiError) as raised,
                ):
                    await client.async_sync_runtime()
                self.assertEqual(
                    client.redact_error(raised.exception),
                    "Failed Beestat call runtime.sync: "
                    "runtime.sync returned an unsuccessful response "
                    f"[failure={failure}] [attempts=3; final_http_status=200]",
                )
                self.assertEqual(type(raised.exception), self.api.BeestatApiError)
                self.assertEqual(session.call_count, 3)
                self.assertEqual(
                    [call.args for call in sleep.await_args_list], [(2,), (4,)]
                )

    async def test_failed_envelope_reports_only_allowlisted_integer_codes(self) -> None:
        secret = "remote-response-secret"
        for success, code in ((False, 1000), (0, 1003), (False, 1005), (0, 1505)):
            with self.subTest(success=success, code=code):
                session = _FakeSession(
                    [
                        {
                            "success": success,
                            "data": {
                                "error_code": code,
                                "error_message": f"Invalid API key {secret}",
                                "error_detail": f"https://private.test/{secret}",
                            },
                        }
                    ]
                )
                client = self.api.BeestatClient(
                    session, "secret-token", "https://api.test/", retries=1
                )
                with self.assertRaises(self.api.BeestatApiError) as raised:
                    await client.async_sync_runtime()
                self.assertEqual(type(raised.exception), self.api.BeestatApiError)
                self.assertEqual(
                    client.redact_error(raised.exception),
                    "Failed Beestat call runtime.sync: "
                    "runtime.sync returned an unsuccessful response "
                    "[failure=unsuccessful_envelope; "
                    f"provider_error_code={code}] "
                    "[attempts=1; final_http_status=200]",
                )
                self.assertNotIn(secret, client.redact_error(raised.exception))

    async def test_failed_envelope_omits_unknown_or_malformed_code_fields(self) -> None:
        secret = "remote-response-secret"
        codes = (9999, True, False, 1000.0, "1000", secret, None, [], {"code": 1000})
        payloads = [
            {
                "success": False,
                "data": {
                    "error_code": code,
                    "error_message": secret,
                    "error_detail": f"https://private.test/{secret}",
                },
            }
            for code in codes
        ]
        payloads.extend(
            (
                {"success": False, "data": {"code": 1000, "error_detail": secret}},
                {"success": False, "error_code": 1000, "message": secret},
                {"success": False, "data": [{"error_code": 1000, "value": secret}]},
                {"success": False, "data": secret},
                {"success": False, "data": None},
            )
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                client = self.api.BeestatClient(
                    _FakeSession([payload]),
                    "secret-token",
                    "https://api.test/",
                    retries=1,
                )
                with self.assertRaises(self.api.BeestatApiError) as raised:
                    await client.async_sync_runtime()
                self.assertEqual(
                    client.redact_error(raised.exception),
                    "Failed Beestat call runtime.sync: "
                    "runtime.sync returned an unsuccessful response "
                    "[failure=unsuccessful_envelope] "
                    "[attempts=1; final_http_status=200]",
                )
                self.assertNotIn(secret, client.redact_error(raised.exception))

    async def test_sync_success_null_and_true_remain_successful(self) -> None:
        for payload in (None, True, {"success": True, "data": None}, {"data": True}):
            with self.subTest(payload=payload):
                session = _FakeSession([payload])
                client = self.api.BeestatClient(
                    session, "secret-token", "https://api.test/"
                )
                sleep = AsyncMock()
                with patch.object(self.api.asyncio, "sleep", new=sleep):
                    self.assertEqual(await client.async_sync_runtime(), [])
                self.assertEqual(session.call_count, 1)
                sleep.assert_not_awaited()

    async def test_successful_data_is_not_reclassified_by_error_code(self) -> None:
        data = {"error_code": 1000, "error_message": "Invalid API key"}
        session = _FakeSession([{"success": True, "data": data}, {"data": data}])
        client = self.api.BeestatClient(session, "secret-token", "https://api.test/")
        self.assertEqual(await client.async_read_id("thermostat"), [data])
        self.assertEqual(await client.async_read_id("thermostat"), [data])
        self.assertEqual(session.call_count, 2)

    async def test_failed_envelope_can_recover_without_changing_retry_policy(
        self,
    ) -> None:
        session = _FakeSession(
            [
                {"success": False, "data": {"error_code": 1003}},
                {"success": True, "data": None},
            ]
        )
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=4
        )
        sleep = AsyncMock()
        with patch.object(self.api.asyncio, "sleep", new=sleep):
            self.assertEqual(await client.async_sync_runtime(), [])
        self.assertEqual(session.call_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_exhaustion_reports_final_http_status_after_mixed_responses(
        self,
    ) -> None:
        for final_status, final_payload, final_detail in (
            (
                200,
                False,
                "runtime.sync returned an unsuccessful response [failure=sync_false]",
            ),
            (503, {}, "runtime.sync returned HTTP 503"),
        ):
            with self.subTest(final_status=final_status):
                session = _FakeSession(
                    [
                        _FakeResponse({}, status=429),
                        _FakeResponse(final_payload, status=final_status),
                    ]
                )
                client = self.api.BeestatClient(
                    session, "secret-token", "https://api.test/", retries=2
                )
                sleep = AsyncMock()
                with (
                    patch.object(self.api.asyncio, "sleep", new=sleep),
                    self.assertRaises(self.api.BeestatApiError) as raised,
                ):
                    await client.async_sync_runtime()
                self.assertEqual(
                    client.redact_error(raised.exception),
                    f"Failed Beestat call runtime.sync: {final_detail} "
                    f"[attempts=2; final_http_status={final_status}]",
                )
                self.assertEqual(session.call_count, 2)
                sleep.assert_awaited_once_with(2)

    async def test_final_transport_failure_does_not_reuse_prior_http_status(
        self,
    ) -> None:
        secret = "network-response-secret"
        session = _FakeSession([])
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=2
        )
        sleep = AsyncMock()
        with (
            patch.object(
                session,
                "get",
                side_effect=[
                    _FakeResponse(False),
                    self.api.aiohttp.ClientError(secret),
                ],
            ) as get,
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaises(self.api.BeestatApiError) as raised,
        ):
            await client.async_sync_runtime()
        self.assertEqual(
            client.redact_error(raised.exception),
            "Failed Beestat call runtime.sync: Beestat network request failed "
            "[attempts=2; final_http_status=unavailable]",
        )
        self.assertEqual(get.call_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_auth_envelope_still_uses_original_nonretrying_exception(
        self,
    ) -> None:
        payload = {
            "success": False,
            "message": "Invalid API key remote-response-secret",
            "data": {"error_code": 1000},
        }
        session = _FakeSession([payload])
        client = self.api.BeestatClient(session, "secret-token", "https://api.test/")
        sleep = AsyncMock()
        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaises(self.api.BeestatAuthError) as raised,
        ):
            await client.async_sync_runtime()
        self.assertEqual(
            client.redact_error(raised.exception), "runtime.sync authentication failed"
        )
        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

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

    async def test_deterministic_http_error_is_not_retried(self) -> None:
        session = _FakeSession([_FakeResponse({}, status=400)])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=3,
        )
        sleep = AsyncMock()

        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaisesRegex(
                self.api.BeestatPermanentError,
                r"thermostat\.read_id returned HTTP 400",
            ),
        ):
            await client.async_read_id("thermostat")

        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

    async def test_redirect_is_rejected_without_forwarding_credentials(self) -> None:
        session = _FakeSession([_FakeResponse({}, status=302)])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=3,
        )
        sleep = AsyncMock()

        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaisesRegex(
                self.api.BeestatPermanentError,
                r"thermostat\.read_id refused HTTP redirect 302",
            ),
        ):
            await client.async_read_id("thermostat")

        self.assertEqual(session.call_count, 1)
        self.assertEqual(session.allow_redirects, [False])
        sleep.assert_not_awaited()

    async def test_transient_http_error_is_retried(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse({}, status=429),
                {"data": [{"id": 1001}]},
            ]
        )
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=2,
        )
        sleep = AsyncMock()

        with patch.object(self.api.asyncio, "sleep", new=sleep):
            result = await client.async_read_id("thermostat")

        self.assertEqual(result, [{"id": 1001}])
        self.assertEqual(session.call_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_http_auth_failure_is_not_retried(self) -> None:
        session = _FakeSession([_FakeResponse({}, status=401)])
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=3
        )
        sleep = AsyncMock()
        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaises(self.api.BeestatAuthError),
        ):
            await client.async_read_id("thermostat")
        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

    async def test_cancelled_transport_propagates_without_retry(self) -> None:
        session = _FakeSession([_FakeResponse({}, json_error=asyncio.CancelledError())])
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=3
        )
        sleep = AsyncMock()
        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await client.async_read_id("thermostat")
        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

    async def test_decode_runs_off_loop_and_cancelled_result_cannot_publish(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        started, finished = asyncio.Event(), asyncio.Event()
        proceed = threading.Event()
        loop_thread = threading.get_ident()
        original_decode = self.api._decode_response
        worker_threads = []

        def paused_decode(chunks):
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            try:
                if not proceed.wait(5):
                    raise AssertionError("decode worker was not released")
                return original_decode(chunks)
            finally:
                loop.call_soon_threadsafe(finished.set)

        session = _FakeSession([{"data": [{"fan": 30}]}])
        client = self.api.BeestatClient(session, "secret-token", "https://api.test/")
        trace = self.api._ReadTrace()
        sleep = AsyncMock()
        with (
            patch.object(self.api, "_decode_response", side_effect=paused_decode),
            patch.object(self.api.asyncio, "sleep", new=sleep),
        ):
            task = asyncio.create_task(
                client._async_call_raw("runtime_thermostat", "read", None, trace=trace)
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertNotEqual(worker_threads, [loop_thread])
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                proceed.set()
                await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(trace.attempts, [])
        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

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

    async def test_response_size_limit_is_not_retried(self) -> None:
        session = _FakeSession([{"data": [{"value": "x" * 64}]}])
        client = self.api.BeestatClient(
            session,
            "secret-token",
            "https://api.test/",
            retries=3,
            max_response_bytes=32,
        )
        sleep = AsyncMock()

        with (
            patch.object(self.api.asyncio, "sleep", new=sleep),
            self.assertRaisesRegex(
                self.api.BeestatApiError,
                "response exceeded the size limit",
            ),
        ):
            await client.async_read_id("thermostat")

        self.assertEqual(session.call_count, 1)
        sleep.assert_not_awaited()

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

    async def test_raw_point_receipts_preserve_mapping_order_and_retry_evidence(
        self,
    ) -> None:
        rows = {"second": {"deleted": True}, "first": {"temperature": 72}}
        session = _FakeSession(
            [_FakeResponse({}, status=429), {"data": rows, "has_more": True}]
        )
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=2
        )
        with patch.object(self.api.asyncio, "sleep", new=AsyncMock()):
            result = await client.async_read_runtime_sensor(
                10, "start", "end", raw_response=True
            )
        self.assertEqual(result.data, rows)
        self.assertEqual(list(result.data), ["second", "first"])
        self.assertNotIn("id", result.data["first"])
        self.assertEqual(
            [item.outcome for item in result.attempts], ["provider_failure", "success"]
        )
        self.assertEqual([item.http_status for item in result.attempts], [429, 200])
        self.assertTrue(result.pagination_indicated)
        self.assertGreater(result.response_bytes, 0)
        self.assertEqual(session.requests[0]["resource"], "runtime_sensor")
        self.assertEqual(session.requests[0]["method"], "read")

    async def test_per_call_raw_limit_does_not_change_shared_client_default(
        self,
    ) -> None:
        payload = {"data": [{"temperature": "x" * 60}]}
        session = _FakeSession([payload, payload])
        client = self.api.BeestatClient(
            session, "secret-token", "https://api.test/", retries=3
        )
        with self.assertRaises(self.api.BeestatRawReadError) as error:
            await client.async_read_runtime_thermostat(
                1, "start", "end", raw_response=True, max_response_bytes=32
            )
        self.assertEqual(len(error.exception.attempts), 1)
        self.assertEqual(error.exception.attempts[0].outcome, "non_retryable_failure")
        self.assertEqual(
            await client.async_read_runtime_thermostat(1, "start", "end"),
            payload["data"],
        )
        self.assertEqual(session.call_count, 2)

    async def test_raw_auth_failure_keeps_safe_receipt_without_remote_body(
        self,
    ) -> None:
        session = _FakeSession([_FakeResponse({"error": "secret-token"}, status=401)])
        client = self.api.BeestatClient(session, "secret-token", "https://api.test/")
        with self.assertRaises(self.api.BeestatRawReadError) as error:
            await client.async_read_runtime_thermostat(
                1, "start", "end", raw_response=True
            )
        self.assertEqual(error.exception.attempts[0].outcome, "authentication_failed")
        self.assertNotIn("secret-token", str(error.exception))
        self.assertNotIn("api.test", str(error.exception))

    async def test_raw_envelope_requires_data_without_changing_default_or_unwrapped_reads(
        self,
    ) -> None:
        payload = {"success": True, "message": "No data included"}
        point = {"timestamp": "2026-09-01T00:00:00Z", "fan": 30}
        keyed = {"second": {"fan": 20}, "first": {"deleted": True}}
        session = _FakeSession([payload, payload, point, keyed, [point]])
        client = self.api.BeestatClient(session, "secret-token", "https://api.test/")
        with self.assertRaisesRegex(self.api.BeestatRawReadError, "without data"):
            await client.async_read_runtime_thermostat(
                1, "start", "end", raw_response=True
            )
        self.assertEqual(
            await client.async_read_runtime_thermostat(1, "start", "end"), [payload]
        )
        for expected in (point, keyed, [point]):
            result = await client.async_read_runtime_thermostat(
                1, "start", "end", raw_response=True
            )
            self.assertEqual(result.data, expected)
        self.assertEqual(session.call_count, 5)


class _FakeResponse:
    def __init__(
        self,
        payload,
        *,
        status: int = 200,
        text: str | None = None,
        json_error: BaseException | None = None,
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
    def __init__(self, body: bytes, read_error: BaseException | None = None) -> None:
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
        self.allow_redirects: list[bool] = []
        self.requests: list[dict[str, str]] = []

    def get(self, _url, *, params, allow_redirects: bool):
        self.call_count += 1
        self.allow_redirects.append(allow_redirects)
        self.requests.append(dict(params))
        response = next(self._payloads)
        return (
            response if isinstance(response, _FakeResponse) else _FakeResponse(response)
        )


if __name__ == "__main__":
    unittest.main()
