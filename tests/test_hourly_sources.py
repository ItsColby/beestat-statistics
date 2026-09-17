"""Source admission, ordering and qualified archive resolution without HA."""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_sources_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
sources = importlib.import_module(f"{PACKAGE}.hourly_sources")


def identity():
    return {
        "entry_id": "fixture-entry",
        "api_base": "https://api.test/",
        "account_anchors": ["account-one"],
        "resources": {
            "sensor-temperature": {"thermostat_id": 1, "sensor_id": 10},
            "thermostat-temperature": {"thermostat_id": 1, "sensor_id": None},
        },
    }


def manifest(**changes):
    return {
        "contract_version": 3,
        "config_entry_id": "fixture-entry",
        "api_base": "https://api.test/",
        "account_anchors": ["account-one"],
        "resource": "runtime_sensor",
        "resource_id": 10,
        "thermostat_id": 1,
        "source_kind": "provider",
        "acquisition_id": "read-one",
        "chunk_index": 0,
        "chunk_count": 1,
        "format": "json",
        "start": "2026-09-01T00:00:00+00:00",
        "end": "2026-09-02T00:00:00+00:00",
        "acquired_at": "2026-09-03T00:00:00+00:00",
        "source_end": "2026-09-02T00:00:00+00:00",
        "unit_contract": "beestat_points_v1",
        **changes,
    }


def row(minute=0, **changes):
    return {
        "sensor_id": 10,
        "timestamp": f"2026-09-01T00:{minute:02}:00+00:00",
        "temperature": 72,
        **changes,
    }


def original_manifest(content, **changes):
    return manifest(
        original_sha256=sha256(content).hexdigest(),
        original_byte_count=len(content),
        chunk_byte_offset=0,
        **changes,
    )


def raw_export():
    declaration = manifest()
    return {
        "schema_version": 1,
        "status": "success",
        "identity": {
            key: declaration[key]
            for key in (
                "config_entry_id",
                "resource",
                "resource_id",
                "thermostat_id",
            )
        },
        "request": {
            **{
                key: declaration[key]
                for key in ("resource", "resource_id", "start", "end")
            },
            "method": "read",
            "timestamp_operator": "between",
            "boundary": "inclusive",
        },
        "completeness": {
            "transport_complete": True,
            "truncated": False,
            "pagination_indicated": False,
            "provider_complete": None,
        },
        "data": [row(), row(timestamp=declaration["end"])],
    }


class ObjectStore:
    def __init__(self):
        self.objects = {}
        self.reads = []

    def write_object(self, kind, content):
        content_digest = sha256(content).hexdigest()
        self.objects[kind, content_digest] = content
        return content_digest

    async def async_write_object(self, kind, content):
        return self.write_object(kind, content)

    async def async_read_object(self, kind, content_digest):
        self.reads.append((kind, content_digest))
        content = self.objects[kind, content_digest]
        if sha256(content).hexdigest() != content_digest:
            raise ValueError("corrupt fixture object")
        return content

    async def async_process_source_job(self, function, *args):
        return function(*args)


class HourlySourcesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ObjectStore()

    async def stage(self, rows, declaration=None):
        content = json.dumps(rows).encode()
        declaration = {**original_manifest(content), **(declaration or {})}
        return await sources.async_stage_source(
            self.store, content, declaration, sha256(content).hexdigest(), identity()
        )

    async def merged(
        self, receipts, *, archive=False, provider_order=None, provider_supersedes=None
    ):
        loaded = await sources.async_load_source_bundle(
            self.store, [item["source_id"] for item in receipts], identity()
        )
        return sources.merge_source_bundle(
            loaded,
            archive_policy=archive,
            provider_order=provider_order,
            provider_supersedes=provider_supersedes,
        )

    async def original_chunks(self, chunks, *, original=None, changes=None):
        original = b"".join(chunks) if original is None else original
        receipts = []
        offset = 0
        for index, chunk in enumerate(chunks):
            declaration = original_manifest(
                original,
                format="jsonl",
                source_kind="archive",
                chunk_index=index,
                chunk_count=len(chunks),
            )
            declaration["chunk_byte_offset"] = offset
            declaration.update((changes or {}).get(index, {}))
            receipts.append(
                await sources.async_stage_source(
                    self.store,
                    chunk,
                    declaration,
                    sha256(chunk).hexdigest(),
                    identity(),
                )
            )
            offset += len(chunk)
        return receipts

    async def test_staging_retains_original_bytes_and_replay_identity(self):
        content = b' [ {"sensor_id":10, "timestamp":"2026-09-01T00:00:00Z"} ]\n'
        expected = sha256(content).hexdigest()
        first = sources.stage_source(
            self.store, content, original_manifest(content), expected, identity()
        )
        second = await sources.async_stage_source(
            self.store, content, original_manifest(content), expected, identity()
        )
        self.assertEqual(first, second)
        self.assertEqual(self.store.objects["source", expected], content)
        self.assertEqual({key[0] for key in self.store.objects}, {"source", "manifest"})
        self.assertEqual(first["confidence"], "provider_ordered")

    async def test_identity_digest_units_and_unbounded_fields_fail_before_write(self):
        cases = [
            manifest(account_anchors=["different"]),
            manifest(thermostat_id=2),
            manifest(resource_id=11),
            manifest(api_base="https://other.test/"),
            manifest(unit_contract="degrees_celsius"),
            manifest(unreviewed=True),
            manifest(chunk_count=2049),
            manifest(end="2026-09-04T00:00:00+00:00"),
        ]
        for declaration in cases:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                await self.stage([row()], declaration)
        with self.assertRaisesRegex(ValueError, "digest_mismatch"):
            await sources.async_stage_source(
                self.store, b"[]", manifest(), "0" * 64, identity()
            )
        self.assertFalse(self.store.objects)

    async def test_parser_bounds_and_nonfinite_or_duplicate_json_are_rejected(self):
        contents = [
            b"[" * 18 + b"0" + b"]" * 18,
            b'[{"temperature":NaN}]',
            b'[{"temperature":1e999}]',
            b'[{"timestamp":"one","timestamp":"two"}]',
            b"\xff",
            json.dumps([row()] * 10001).encode(),
            b" " * (8 * 1024 * 1024 + 1),
        ]
        for content in contents:
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                await sources.async_stage_source(
                    self.store,
                    content,
                    original_manifest(content),
                    sha256(content).hexdigest(),
                    identity(),
                )
        self.assertFalse(self.store.objects)

    async def test_raw_success_export_matches_identity_and_inclusive_bounds(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                envelope = raw_export()

                def transport(value, wrapped=wrapped):
                    return (
                        {"changed_states": [], "service_response": value}
                        if wrapped
                        else value
                    )

                receipt = await self.stage(transport(envelope))
                self.assertEqual(receipt["row_count"], 2)
                cases = [
                    (("status",), "error", "incomplete_export"),
                    (
                        ("completeness", "transport_complete"),
                        False,
                        "incomplete_export",
                    ),
                    (("completeness", "truncated"), True, "incomplete_export"),
                    (
                        ("completeness", "pagination_indicated"),
                        True,
                        "incomplete_export",
                    ),
                    (("request", "boundary"), "exclusive", "export_identity"),
                    (("identity", "config_entry_id"), "other-entry", "export_identity"),
                    (("request", "end"), "2026-09-01T01:00:00Z", "export_window"),
                ]
                for path, value, reason in cases:
                    with self.subTest(path=path):
                        changed = deepcopy(envelope)
                        target = changed
                        for key in path[:-1]:
                            target = target[key]
                        target[path[-1]] = value
                        with self.assertRaisesRegex(ValueError, reason):
                            await self.stage(transport(changed))

    async def test_native_rest_export_retains_outer_bytes_and_resolves_points(self):
        envelope = raw_export()
        content = (
            json.dumps({"changed_states": [], "service_response": envelope}, indent=2)
            + "\n"
        ).encode()
        receipt = sources.stage_source(
            self.store,
            content,
            original_manifest(content),
            sha256(content).hexdigest(),
            identity(),
        )
        self.assertEqual(receipt["row_count"], 2)
        self.assertEqual(self.store.objects["source", receipt["sha256"]], content)
        self.assertEqual(
            receipt["manifest"]["original_sha256"], sha256(content).hexdigest()
        )
        self.assertEqual(receipt["manifest"]["original_byte_count"], len(content))
        merged = await self.merged([receipt])
        resource = merged["resources"]["runtime_sensor:10"]
        self.assertEqual(resource["rows"], envelope["data"])
        self.assertEqual(resource["blocked_windows"], [])
        self.assertEqual(resource["conflict_hours"], [])

    async def test_native_rest_empty_read_preserves_earlier_points(self):
        first = await self.stage([row()])
        export = {**raw_export(), "data": []}
        empty = await self.stage(
            {"changed_states": [], "service_response": export},
            manifest(acquisition_id="read-two"),
        )
        self.assertEqual(empty["row_count"], 0)
        merged = await self.merged([first, empty])
        resource = merged["resources"]["runtime_sensor:10"]
        self.assertEqual(resource["rows"], [row()])
        self.assertEqual(resource["blocked_windows"], [])

    async def test_malformed_native_rest_wrappers_fail_before_retention(self):
        envelope = raw_export()
        cases = [
            {"service_response": envelope},
            {"changed_states": []},
            {"changed_states": None, "service_response": envelope},
            {"changed_states": {}, "service_response": envelope},
            {"changed_states": [row()], "service_response": envelope},
            {"changed_states": [], "service_response": []},
            {"changed_states": [], "service_response": None},
            {"changed_states": [], "service_response": envelope, "data": []},
            {
                "changed_states": [],
                "service_response": {
                    "changed_states": [],
                    "service_response": envelope,
                },
            },
        ]
        for index, value in enumerate(cases):
            with (
                self.subTest(index=index),
                self.assertRaisesRegex(
                    ValueError, "rest_export_shape|incomplete_export"
                ),
            ):
                await self.stage(value)
        self.assertFalse(self.store.objects)

    async def test_complete_ordered_chunks_preserve_last_provider_correction(self):
        first = await self.stage([row(temperature=72)], manifest(chunk_count=2))
        with self.assertRaisesRegex(ValueError, "chunks_incomplete"):
            await self.merged([first])
        second = await self.stage(
            [row(temperature="invalid")], manifest(chunk_count=2, chunk_index=1)
        )
        merged = await self.merged([second, first])
        self.assertEqual(
            merged["resources"]["runtime_sensor:10"]["rows"],
            [row(temperature="invalid")],
        )

    async def test_later_provider_correction_and_tombstone_never_fall_back(self):
        old = await self.stage([row()])
        new = await self.stage(
            [row(deleted=True)],
            manifest(acquisition_id="read-two", acquired_at="2026-09-04T00:00:00Z"),
        )
        archive = await self.stage(
            [row()], manifest(source_kind="archive", acquisition_id="old-cache")
        )
        held = await self.merged([old, archive, new], archive=True)
        self.assertTrue(held["resources"]["runtime_sensor:10"]["conflict_hours"])
        merged = await self.merged(
            [old, archive, new],
            archive=True,
            provider_order={"runtime_sensor:10": ["read-one", "read-two"]},
        )
        resource = merged["resources"]["runtime_sensor:10"]
        self.assertTrue(resource["rows"][0]["deleted"])
        self.assertEqual(resource["conflict_hours"], [])

    async def test_empty_later_response_preserves_earlier_observations(self):
        old = await self.stage([row()])
        empty = await self.stage(
            [], manifest(acquisition_id="read-two", acquired_at="2026-09-04T00:00:00Z")
        )
        merged = await self.merged([old, empty])
        self.assertEqual(merged["resources"]["runtime_sensor:10"]["rows"], [row()])

    async def test_archive_only_before_first_fresh_point_requires_explicit_policy(self):
        archive = await self.stage(
            [row(0), row(5), row(15)],
            manifest(source_kind="archive", acquisition_id="cache"),
        )
        provider = await self.stage([row(10)])
        denied = await self.merged([archive, provider])
        admitted = await self.merged([archive, provider], archive=True)
        alone = await self.merged([archive], archive=True)
        self.assertEqual(len(denied["resources"]["runtime_sensor:10"]["rows"]), 1)
        result = admitted["resources"]["runtime_sensor:10"]
        self.assertEqual(result["rows"], [row(0), row(5), row(10)])
        self.assertEqual(
            result["slots"][row()["timestamp"]]["confidence"], ["archive_qualified"]
        )
        self.assertEqual(alone["resources"]["runtime_sensor:10"]["rows"], [])

    async def test_equal_cross_source_observations_coalesce_provenance(self):
        provider = await self.stage([row(runtime_sensor_id=123)])
        archive = await self.stage(
            [row(runtime_sensor_id=987)],
            manifest(source_kind="archive", acquisition_id="cache"),
        )
        merged = await self.merged([provider, archive], archive=True)
        slot = merged["resources"]["runtime_sensor:10"]["slots"][row()["timestamp"]]
        self.assertEqual(
            set(slot["source_ids"]), {provider["source_id"], archive["source_id"]}
        )
        self.assertEqual(slot["confidence"], ["archive_qualified", "provider_ordered"])

    async def test_cross_source_and_unordered_archive_conflicts_quarantine_hour(self):
        provider = await self.stage([row(10)])
        archive = await self.stage(
            [row(temperature=70), row(temperature=71)],
            manifest(source_kind="archive", acquisition_id="cache"),
        )
        merged = await self.merged([provider, archive], archive=True)
        self.assertEqual(
            merged["resources"]["runtime_sensor:10"]["conflict_hours"],
            [row()["timestamp"]],
        )
        conflict = await self.stage(
            [row(10, temperature=73)], manifest(acquisition_id="equal-rank")
        )
        merged = await self.merged([provider, conflict])
        self.assertEqual(merged["resources"]["runtime_sensor:10"]["rows"], [])
        self.assertEqual(
            merged["resources"]["runtime_sensor:10"]["conflict_hours"],
            [row()["timestamp"]],
        )

    async def test_unplaceable_timestamp_blocks_only_its_resource_window(self):
        bad = await self.stage([row(timestamp="unplaceable")])
        good = await self.stage(
            [{"thermostat_id": 1, "timestamp": row()["timestamp"], "fan": 0}],
            manifest(resource="runtime_thermostat", resource_id=1),
        )
        merged = await self.merged([bad, good])
        sensor = merged["resources"]["runtime_sensor:10"]
        thermostat = merged["resources"]["runtime_thermostat:1"]
        self.assertEqual(
            sensor["blocked_windows"],
            [
                {
                    "start": manifest()["start"],
                    "end": manifest()["end"],
                    "reason": "unplaceable_timestamp",
                }
            ],
        )
        self.assertEqual(thermostat["blocked_windows"], [])
        self.assertEqual(len(thermostat["rows"]), 1)

    async def test_single_row_missing_timestamp_is_retained_and_quarantined(self):
        receipt = await self.stage({"sensor_id": 10, "deleted": True})
        self.assertEqual(receipt["row_count"], 1)
        merged = await self.merged([receipt])
        self.assertEqual(
            merged["resources"]["runtime_sensor:10"]["blocked_windows"][0]["reason"],
            "unplaceable_timestamp",
        )

    async def test_jsonl_keeps_original_chunk_bytes_and_null_blocks_window(self):
        content = json.dumps(row()).encode() + b"\nnull\n"
        receipt = await sources.async_stage_source(
            self.store,
            content,
            original_manifest(content, format="jsonl"),
            sha256(content).hexdigest(),
            identity(),
        )
        merged = await self.merged([receipt])
        self.assertTrue(merged["resources"]["runtime_sensor:10"]["blocked_windows"])
        self.assertEqual(self.store.objects["source", receipt["sha256"]], content)

    async def test_aggregate_limit_rejects_bundle_without_mutating_retained_sources(
        self,
    ):
        receipt = await self.stage([row()])
        before = dict(self.store.objects)
        with (
            patch.object(sources, "MAX_BUNDLE_BYTES", 1),
            self.assertRaisesRegex(ValueError, "bundle_limit"),
        ):
            await self.merged([receipt])
        self.assertEqual(before, self.store.objects)

    async def test_exact_original_reassembly_preserves_multibyte_crlf_and_whitespace(
        self,
    ):
        chunks = [
            b"  "
            + json.dumps(row(label="\u00e9\U0001f9ea"), ensure_ascii=False).encode()
            + b"\r\n",
            json.dumps(row(5), separators=(",", ":")).encode() + b"\r\n\r\n",
            json.dumps(row(10)).encode(),
        ]
        receipts = await self.original_chunks(chunks)
        loaded = await sources.async_load_source_bundle(
            self.store, [item["source_id"] for item in reversed(receipts)], identity()
        )
        self.assertEqual(sum(item["row_count"] for item in loaded), 3)
        reconstructed = b"".join(
            self.store.objects["source", item["sha256"]]
            for item in sorted(
                loaded, key=lambda value: value["manifest"]["chunk_index"]
            )
        )
        self.assertEqual(reconstructed, b"".join(chunks))
        self.assertEqual(
            sha256(reconstructed).hexdigest(),
            receipts[0]["manifest"]["original_sha256"],
        )

    async def test_changed_serialization_cannot_match_declared_original(self):
        chunks = [
            json.dumps(row()).encode() + b"\n",
            json.dumps(row(5)).encode() + b"\n",
        ]
        original = b"".join(chunks)
        # Move one space without changing any parsed value or total byte count.
        changed = [b" " + chunks[0].replace(b": ", b":", 1), chunks[1]]
        receipts = await self.original_chunks(changed, original=original)
        with self.assertRaisesRegex(ValueError, "original_digest_mismatch"):
            await self.merged(receipts, archive=True)

    async def test_original_offset_gap_overlap_and_missing_piece_fail_closed(self):
        chunks = [
            json.dumps(row()).encode() + b"\n",
            json.dumps(row(5)).encode() + b"\n",
            json.dumps(row(10)).encode() + b"\n",
        ]
        for difference in (-1, 1):
            with self.subTest(offset_delta=difference):
                receipts = await self.original_chunks(
                    chunks,
                    changes={1: {"chunk_byte_offset": len(chunks[0]) + difference}},
                )
                with self.assertRaisesRegex(ValueError, "original_chunk_sequence"):
                    await self.merged(receipts, archive=True)
        receipts = await self.original_chunks(chunks)
        with self.assertRaisesRegex(ValueError, "chunks_incomplete"):
            await self.merged(receipts[:2], archive=True)

    async def test_chunk_order_cannot_reorder_original_bytes(self):
        chunks = [
            json.dumps(row()).encode() + b"\n",
            json.dumps(row(5)).encode() + b"\n",
        ]
        receipts = await self.original_chunks(
            chunks, changes={0: {"chunk_index": 1}, 1: {"chunk_index": 0}}
        )
        with self.assertRaisesRegex(ValueError, "original_chunk_sequence"):
            await self.merged(receipts, archive=True)

    async def test_partial_line_or_split_json_original_is_rejected_during_staging(self):
        line = json.dumps(row(label="\u00e9"), ensure_ascii=False).encode() + b"\n"
        split = line.index("\u00e9".encode()) + 1
        with self.assertRaisesRegex(ValueError, "complete_lines"):
            await self.original_chunks([line[:split], line[split:]])
        content = json.dumps([row()]).encode()
        declaration = original_manifest(content + b" ")
        with self.assertRaisesRegex(ValueError, "complete_original"):
            await sources.async_stage_source(
                self.store,
                content,
                declaration,
                sha256(content).hexdigest(),
                identity(),
            )

    async def test_empty_jsonl_original_is_exact_and_nonempty_original_rejects_empty_chunk(
        self,
    ):
        receipt = (await self.original_chunks([b""]))[0]
        loaded = await sources.async_load_source_bundle(
            self.store, [receipt["source_id"]], identity()
        )
        self.assertEqual(loaded[0]["row_count"], 0)
        self.assertEqual(
            loaded[0]["manifest"]["original_sha256"], sha256(b"").hexdigest()
        )
        with self.assertRaisesRegex(ValueError, "original_bounds"):
            await self.original_chunks([b"", json.dumps(row()).encode() + b"\n"])

    async def test_original_object_limit_is_independent_of_transport_chunk_limit(self):
        declaration = original_manifest(b"[]")
        declaration["original_byte_count"] = 128 * 1024 * 1024 + 1
        with self.assertRaisesRegex(ValueError, "original_bounds"):
            await sources.async_stage_source(
                self.store, b"[]", declaration, sha256(b"[]").hexdigest(), identity()
            )

    async def test_window_reads_archive_payload_but_preserves_later_provider_boundary(
        self,
    ):
        archive = await self.stage(
            [row(timestamp="2026-04-01T00:00:00Z")],
            manifest(
                source_kind="archive",
                acquisition_id="archive",
                start="2026-04-01T00:00:00Z",
                end="2026-04-02T00:00:00Z",
            ),
        )
        provider = await self.stage(
            [row(timestamp="2026-06-01T00:00:00Z")],
            manifest(start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z"),
        )
        source_ids = [archive["source_id"], provider["source_id"]]
        declarations = await sources.async_load_source_manifests(
            self.store, source_ids, identity()
        )
        anchors = sources.first_provider_points(declarations)
        self.assertEqual(anchors, {"runtime_sensor:10": "2026-06-01T00:00:00+00:00"})
        loaded = await sources.async_load_source_bundle(
            self.store,
            source_ids,
            identity(),
            window=("2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"),
        )
        self.assertEqual([item["source_id"] for item in loaded], [archive["source_id"]])
        self.assertNotIn(("source", provider["sha256"]), self.store.reads)
        merged = sources.merge_source_bundle(
            loaded, archive_policy=True, first_provider=anchors
        )
        self.assertEqual(len(merged["resources"]["runtime_sensor:10"]["rows"]), 1)
        self.assertEqual(merged["first_provider"], anchors)

    async def test_window_reads_all_chunks_of_selected_original(self):
        chunks = [
            json.dumps(row()).encode() + b"\n",
            json.dumps(row(timestamp="2026-09-02T00:00:00Z")).encode() + b"\n",
        ]
        receipts = await self.original_chunks(
            chunks,
            changes={
                0: {"end": "2026-09-01T01:00:00Z"},
                1: {"start": "2026-09-02T00:00:00Z", "end": "2026-09-02T01:00:00Z"},
            },
        )
        loaded = await sources.async_load_source_bundle(
            self.store,
            [item["source_id"] for item in receipts],
            identity(),
            window=("2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z"),
        )
        self.assertEqual(len(loaded), 2)
        for receipt in receipts:
            self.assertIn(("source", receipt["sha256"]), self.store.reads)

    async def test_nonoverlap_metadata_identity_and_bad_request_window_still_fail(self):
        receipt = await self.stage([row()])
        for window in (
            ("2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
            ("2026-09-01T00:00:00", "2026-09-02T00:00:00Z"),
        ):
            with self.subTest(window=window), self.assertRaises(ValueError):
                await sources.async_load_source_bundle(
                    self.store, [receipt["source_id"]], identity(), window=window
                )
        changed = identity()
        changed["account_anchors"] = ["another-account"]
        with self.assertRaisesRegex(ValueError, "account_mismatch"):
            await sources.async_load_source_bundle(
                self.store,
                [receipt["source_id"]],
                changed,
                window=("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
            )
        self.assertFalse(any(kind == "source" for kind, _ in self.store.reads))

    async def test_selected_originals_alone_count_toward_bundle_bytes(self):
        old = await self.stage(
            [row(timestamp="2026-08-01T00:00:00Z")],
            manifest(
                acquisition_id="old",
                start="2026-08-01T00:00:00Z",
                end="2026-08-02T00:00:00Z",
            ),
        )
        new = await self.stage([row()])
        with patch.object(sources, "MAX_BUNDLE_BYTES", new["byte_count"]):
            loaded = await sources.async_load_source_bundle(
                self.store,
                [old["source_id"], new["source_id"]],
                identity(),
                window=("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
            )
        self.assertEqual([item["source_id"] for item in loaded], [new["source_id"]])

    async def test_catalog_scan_filters_before_cap_and_preserves_cross_page_acquisition(
        self,
    ):
        old = []
        first = datetime(2026, 8, 1, tzinfo=UTC)
        for index in range(2047):
            stamp = (first + timedelta(minutes=index * 5)).isoformat()
            old.append(
                await self.stage(
                    [row(timestamp=stamp)],
                    manifest(
                        acquisition_id=f"old-{index}",
                        start="2026-08-01T00:00:00Z",
                        end="2026-08-10T00:00:00Z",
                    ),
                )
            )
        selected = await self.original_chunks(
            [json.dumps(row()).encode() + b"\n", json.dumps(row(5)).encode() + b"\n"]
        )
        ids = [item["source_id"] for item in [*old, *selected]]
        window = ("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "bundle_ids"):
            await sources.async_load_source_bundle(
                self.store, ids, identity(), window=window
            )
        declarations, loaded = await sources.async_load_catalog_source_bundle(
            self.store, ids, identity(), window=window
        )
        self.assertEqual(len(declarations), 2049)
        self.assertEqual(
            [item["source_id"] for item in loaded],
            [item["source_id"] for item in selected],
        )
        self.assertEqual(
            {
                content_digest
                for kind, content_digest in self.store.reads
                if kind == "source"
            },
            {item["sha256"] for item in selected},
        )
        self.store.reads.clear()
        with self.assertRaisesRegex(ValueError, "bundle_limit"):
            await sources.async_load_catalog_source_bundle(
                self.store,
                ids,
                identity(),
                window=("2026-08-01T00:00:00Z", "2026-09-02T00:00:00Z"),
            )
        self.assertFalse(any(kind == "source" for kind, _ in self.store.reads))
        self.store.reads.clear()
        with self.assertRaisesRegex(ValueError, "chunks_incomplete"):
            await sources.async_load_catalog_source_bundle(
                self.store, ids[:-1], identity(), window=window
            )
        self.assertFalse(any(kind == "source" for kind, _ in self.store.reads))

    def delta(self):
        incoming = json.dumps([row(deleted=True)]).encode()
        return {
            "format": "integration_delta_v1",
            "baseline_source_revision": "a" * 64,
            "baseline_digest": "b" * 64,
            "evaluation_version": "beestat_points_delta_v1",
            "acquisition": {
                "acquisition_id": "read-one",
                "bounds": {"start": manifest()["start"], "end": manifest()["end"]},
                "evaluated_at": manifest()["acquired_at"],
                "original_chunks": [
                    {
                        "sha256": sha256(incoming).hexdigest(),
                        "byte_count": len(incoming),
                        "row_count": 1,
                        "manifest": original_manifest(incoming),
                    }
                ],
                "original_bytes_kind": "integration_json_export",
                "original_bytes_retained": False,
            },
            "resource": {
                "resource": "runtime_sensor",
                "resource_id": 10,
                "thermostat_id": 1,
            },
            "rows": [row(deleted=True)],
        }

    async def test_delta_seals_baseline_qualification_without_claiming_input_retention(
        self,
    ):
        value = self.delta()
        receipt = await self.stage(value)
        self.assertEqual(receipt["confidence"], "integration_delta_v1")
        loaded = await sources.async_load_source_bundle(
            self.store, [receipt["source_id"]], identity()
        )
        self.assertEqual(
            loaded[0]["delta"],
            {key: item for key, item in value.items() if key != "rows"},
        )
        self.assertFalse(loaded[0]["delta"]["acquisition"]["original_bytes_retained"])
        self.assertNotIn(
            ("source", value["acquisition"]["original_chunks"][0]["sha256"]),
            self.store.objects,
        )
        merged = sources.merge_source_bundle(loaded)
        slot = merged["resources"]["runtime_sensor:10"]["slots"][row()["timestamp"]]
        self.assertEqual(slot["confidence"], ["integration_delta_v1"])
        self.assertTrue(merged["resources"]["runtime_sensor:10"]["rows"][0]["deleted"])

    async def test_superseding_correction_does_not_order_untouched_conflicts(self):
        later = "2026-09-01T01:00:00+00:00"
        old_a = await self.stage(
            [row(temperature=70), row(timestamp=later, temperature=70)],
            manifest(acquisition_id="old-a"),
        )
        old_b = await self.stage(
            [row(temperature=71), row(timestamp=later, temperature=71)],
            manifest(acquisition_id="old-b"),
        )
        correction = await self.stage(
            [row(deleted=True)], manifest(acquisition_id="correction")
        )
        merged = await self.merged(
            [old_a, old_b, correction],
            provider_supersedes={
                "runtime_sensor:10": {"correction": ["old-a", "old-b"]}
            },
        )
        result = merged["resources"]["runtime_sensor:10"]
        self.assertEqual(result["rows"], [row(deleted=True)])
        self.assertEqual(result["conflict_hours"], [later])

    async def test_supersession_must_dominate_every_differing_candidate(self):
        old_a = await self.stage(
            [row(temperature=70)], manifest(acquisition_id="old-a")
        )
        old_b = await self.stage(
            [row(temperature=71)], manifest(acquisition_id="old-b")
        )
        correction = await self.stage(
            [row(temperature="invalid")], manifest(acquisition_id="correction")
        )
        merged = await self.merged(
            [old_a, old_b, correction],
            provider_supersedes={"runtime_sensor:10": {"correction": ["old-a"]}},
        )
        result = merged["resources"]["runtime_sensor:10"]
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["conflict_hours"], [row()["timestamp"]])

    async def test_transitive_supersession_preserves_invalid_correction(self):
        old = await self.stage([row()], manifest(acquisition_id="original"))
        correction = await self.stage(
            [row(temperature="invalid")], manifest(acquisition_id="latest")
        )
        merged = await self.merged(
            [old, correction],
            provider_supersedes={
                "runtime_sensor:10": {"latest": ["middle"], "middle": ["original"]}
            },
        )
        result = merged["resources"]["runtime_sensor:10"]
        self.assertEqual(result["rows"], [row(temperature="invalid")])
        self.assertEqual(result["conflict_hours"], [])

    async def test_precedence_cycles_and_self_supersession_fail_closed(self):
        receipt = await self.stage([row()], manifest(acquisition_id="original"))
        for relation in (
            {"original": ["original"]},
            {"latest": ["original"], "original": ["latest"]},
        ):
            with self.subTest(relation=relation), self.assertRaises(ValueError):
                await self.merged(
                    [receipt], provider_supersedes={"runtime_sensor:10": relation}
                )
        with self.assertRaisesRegex(ValueError, "supersedes_cycle"):
            await self.merged(
                [receipt],
                provider_order={"runtime_sensor:10": ["original", "latest"]},
                provider_supersedes={"runtime_sensor:10": {"original": ["latest"]}},
            )

    async def test_supersession_declarations_change_revision_without_inventing_rows(
        self,
    ):
        receipt = await self.stage([row()], manifest(acquisition_id="original"))
        first = await self.merged(
            [receipt],
            provider_supersedes={"runtime_sensor:10": {"later": ["original"]}},
        )
        second = await self.merged(
            [receipt],
            provider_supersedes={"runtime_sensor:10": {"later": ["original", "other"]}},
        )
        self.assertNotEqual(first["source_revision"], second["source_revision"])
        self.assertEqual(first["resources"], second["resources"])

    async def test_delta_rejects_unbound_baseline_and_false_retention_claim(self):
        for key, change in (
            ("baseline_digest", "not-a-digest"),
            ("evaluation_version", "unknown"),
        ):
            value = self.delta()
            value[key] = change
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.stage(value)
        value = self.delta()
        value["acquisition"]["original_bytes_retained"] = True
        with self.assertRaisesRegex(ValueError, "delta_acquisition"):
            await self.stage(value)
        value = self.delta()
        value["acquisition"]["original_chunks"][0]["manifest"]["resource_id"] = 11
        with self.assertRaisesRegex(ValueError, "delta_commitment"):
            await self.stage(value)
        self.assertFalse(self.store.objects)


if __name__ == "__main__":
    unittest.main()
