"""Offline raw acquisition preserves evidence and fails visibly at public bounds."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tests.test_api_response import (
    PACKAGE,
    _FakeResponse,
    _FakeSession,
    _load_api_module,
)

START = datetime(2026, 9, 1, tzinfo=UTC)
END = START + timedelta(days=1)


class RawPointsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_aiohttp = sys.modules.get("aiohttp")
        stub = types.ModuleType("aiohttp")
        stub.ClientError = type("ClientError", (Exception,), {})
        stub.ClientSession = object
        sys.modules["aiohttp"] = stub
        self.api = _load_api_module()
        sys.modules.pop(f"{PACKAGE}.raw_points", None)
        self.raw = importlib.import_module(f"{PACKAGE}.raw_points")
        config = importlib.import_module(f"{PACKAGE}.config_model")
        self.config = config.BeestatConfig(
            (config.ConfiguredThermostat(1, "zone", "Zone"),),
            (
                config.ConfiguredSensor(
                    10, "room", "Room", 1, "zone", True, False, False, True
                ),
            ),
        )

    def tearDown(self):
        if self.old_aiohttp is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = self.old_aiohttp

    def identity(self, request, **changes):
        params = {"config_entry_id": "fixture-entry", "metadata_fetched_at": START}
        params.update(changes)
        return self.raw.validate_raw_point_identity(
            request,
            self.config,
            [{"id": 1}],
            [{"id": 10, "thermostat_id": 1}],
            **params,
        )

    async def test_both_resources_preserve_duplicates_deletions_and_mapping_keys(self):
        sources = (
            (
                "runtime_thermostat",
                1,
                [
                    {"timestamp": "later", "fan": 30},
                    {"timestamp": "later", "deleted": True},
                ],
            ),
            (
                "runtime_sensor",
                10,
                {"last": {"temperature": 72}, "first": {"deleted": True}},
            ),
        )
        for resource, resource_id, data in sources:
            with self.subTest(resource=resource):
                session = _FakeSession([{"data": data}])
                client = self.api.BeestatClient(
                    session, "fixture-secret", "https://api.test/"
                )
                request = self.raw.parse_raw_point_request(
                    resource, resource_id, START, END
                )
                result = await self.raw.async_read_raw_points(
                    client, request, self.identity(request)
                )
                self.assertEqual(result["data"], data)
                self.assertEqual(result["request"]["boundary"], "inclusive")
                self.assertEqual(result["identity"]["thermostat_id"], 1)
                self.assertTrue(result["completeness"]["transport_complete"])
                self.assertIsNone(result["completeness"]["provider_complete"])
                self.assertIsNone(result["completeness"]["sample_completeness"])
                self.assertIsNone(result["completeness"]["provider_settlement"])
                self.assertEqual(session.call_count, 1)

    def test_invalid_requests_and_ambiguous_identity_fail_before_transport(self):
        invalid = (
            ("thermostat", 1, START, END),
            ("runtime_thermostat", True, START, END),
            ("runtime_thermostat", 0, START, END),
            ("runtime_thermostat", 1, START.replace(tzinfo=None), END),
            ("runtime_thermostat", 1, START, START),
            ("runtime_thermostat", 1, START, START + timedelta(days=31, seconds=1)),
            ("runtime_thermostat", 1, START, END.replace(microsecond=1)),
        )
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.raw.parse_raw_point_request(*args)
        request = self.raw.parse_raw_point_request("runtime_sensor", 10, START, END)
        precise_metadata_time = START.replace(microsecond=123456)
        self.assertEqual(
            self.identity(
                request, metadata_fetched_at=precise_metadata_time
            ).metadata_fetched_at,
            precise_metadata_time.isoformat(),
        )
        for thermostat_rows, sensor_rows in (
            ([{"id": 1}], [{"id": 10, "thermostat_id": 2}]),
            ([{"id": 1}, {"id": "1"}], [{"id": 10, "thermostat_id": 1}]),
            ([{"id": 1}], [{"id": 10, "thermostat_id": 1}] * 2),
            ([], [{"id": 10, "thermostat_id": 1}]),
        ):
            with self.assertRaises(ValueError):
                self.raw.validate_raw_point_identity(
                    request,
                    self.config,
                    thermostat_rows,
                    sensor_rows,
                    config_entry_id="fixture",
                    metadata_fetched_at=START,
                )

    async def test_transport_failure_is_distinct_from_empty_success(self):
        request = self.raw.parse_raw_point_request("runtime_thermostat", 1, START, END)
        for payload, expected in (
            (_FakeResponse({}, status=500), "failed"),
            ({"data": []}, "success"),
        ):
            session = _FakeSession([payload])
            client = self.api.BeestatClient(
                session, "fixture-secret", "https://api.test/", retries=1
            )
            result = await self.raw.async_read_raw_points(
                client, request, self.identity(request)
            )
            self.assertEqual(result["status"], expected)
            if expected == "failed":
                self.assertNotIn("data", result)
                self.assertFalse(result["completeness"]["transport_complete"])
            else:
                self.assertEqual(result["data"], [])

    async def test_response_limits_return_failure_without_truncated_data(self):
        request = self.raw.parse_raw_point_request("runtime_sensor", 10, START, END)
        for data, limits, expected in (
            ([{}, {}, {}], {"RAW_POINT_MAX_ROWS": 2}, "row limit"),
            ([{"value": "é" * 200}], {"RAW_POINT_MAX_BYTES": 1500}, "size limit"),
            ([{"value": float("nan")}], {}, "finite JSON"),
            (True, {}, "data shape"),
        ):
            client = self.api.BeestatClient(
                _FakeSession([{"data": data}]),
                "fixture-secret",
                "https://api.test/",
                retries=1,
            )
            with patch.multiple(self.raw, **limits) if limits else patch.dict({}, {}):
                result = await self.raw.async_read_raw_points(
                    client, request, self.identity(request)
                )
            self.assertEqual(result["status"], "failed")
            self.assertIn(expected, result["error"])
            self.assertNotIn("data", result)
            self.assertFalse(result["completeness"]["truncated"])

    async def test_provider_incomplete_or_missing_data_cannot_be_successful_raw_evidence(
        self,
    ):
        request = self.raw.parse_raw_point_request("runtime_thermostat", 1, START, END)
        for hint in ("truncated", "has_more", "next", "next_page", "next_cursor"):
            with self.subTest(hint=hint):
                session = _FakeSession([{"data": [{"fan": 30}], hint: True}])
                client = self.api.BeestatClient(
                    session, "fixture-secret", "https://api.test/"
                )
                result = await self.raw.async_read_raw_points(
                    client, request, self.identity(request)
                )
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("data", result)
                self.assertTrue(result["completeness"]["pagination_indicated"])
                self.assertTrue(result["completeness"]["truncated"])
                self.assertFalse(result["completeness"]["provider_complete"])
                self.assertTrue(result["completeness"]["transport_complete"])
                self.assertEqual(session.call_count, 1)
        for payload in (
            {"success": True},
            {"success": True, "message": "No data included"},
        ):
            session = _FakeSession([payload])
            client = self.api.BeestatClient(
                session, "fixture-secret", "https://api.test/"
            )
            result = await self.raw.async_read_raw_points(
                client, request, self.identity(request)
            )
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("data", result)
            self.assertIn("without data", result["error"])
            self.assertEqual(result["attempts"][0]["outcome"], "non_retryable_failure")
            self.assertEqual(session.call_count, 1)

    async def test_cancellation_propagates_and_mismatched_identity_never_requests(self):
        session = _FakeSession([_FakeResponse({}, json_error=asyncio.CancelledError())])
        client = self.api.BeestatClient(session, "fixture-secret", "https://api.test/")
        request = self.raw.parse_raw_point_request("runtime_thermostat", 1, START, END)
        identity = self.identity(request)
        with self.assertRaises(ValueError):
            await self.raw.async_read_raw_points(
                client, request, replace(identity, resource_id=2)
            )
        self.assertEqual(session.call_count, 0)
        with self.assertRaises(asyncio.CancelledError):
            await self.raw.async_read_raw_points(client, request, identity)
        self.assertEqual(session.call_count, 1)


if __name__ == "__main__":
    unittest.main()
