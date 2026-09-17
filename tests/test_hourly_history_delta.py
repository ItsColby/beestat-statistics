"""Exact routine delta comparison, provenance and immutable baseline bindings."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_hourly_history_delta_test"
package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
package.__path__ = [str(ROOT)]
module_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.hourly_history_delta", ROOT / "hourly_history_delta.py"
)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError("Unable to load history delta")
delta = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = delta
module_spec.loader.exec_module(delta)
sources = sys.modules[f"{PACKAGE}.hourly_sources"]
START = datetime(2026, 9, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


class HistoryDeltaTest(unittest.TestCase):
    def setUp(self):
        self.end = START + 2 * HOUR
        self.evaluated_at = START + 4 * HOUR
        self.identity = {
            "entry_id": "fixture",
            "api_base": "https://api.beestat.io/",
            "account_anchors": ["account-1"],
            "resources": {
                "thermostat": {"thermostat_id": 1, "sensor_id": None},
                "sensor": {"thermostat_id": 1, "sensor_id": 2},
            },
        }
        self.baseline = {
            "source_revision": "a" * 64,
            "evaluation_version": delta.EVALUATION_VERSION,
            "identity": {
                key: self.identity[key]
                for key in ("entry_id", "api_base", "account_anchors")
            },
            "start": START.isoformat(),
            "end": self.end.isoformat(),
            "provider_order": {},
            "first_provider": {},
            "routine_policy": {},
            "resources": {},
        }
        self.seal()

    def seal(self):
        self.baseline["baseline_digest"] = delta.digest(
            {
                key: value
                for key, value in self.baseline.items()
                if key != "baseline_digest"
            }
        )

    def old_rows(self, rows, *, resource="runtime_thermostat", resource_id=1):
        self.baseline["resources"][f"{resource}:{resource_id}"] = {
            "resource": resource,
            "resource_id": resource_id,
            "thermostat_id": 1,
            "rows": copy.deepcopy(rows),
            "slots": {
                sources._row_stamp(row).isoformat(): {
                    "row": copy.deepcopy(row),
                    "source_ids": ["old-source"],
                    "confidence": ["provider_ordered"],
                    "basis": "provider",
                }
                for row in rows
            },
            "conflict_hours": [],
            "blocked_windows": [],
        }
        self.seal()

    def row(self, value=100, *, start=START, **fields):
        return {
            "timestamp": start.isoformat(),
            "thermostat_id": 1,
            "fan": value,
            **fields,
        }

    def incoming(
        self,
        rows,
        *,
        index=0,
        count=1,
        acquisition_id="routine-1",
        resource="runtime_thermostat",
        resource_id=1,
        indent=None,
        start=None,
        end=None,
    ):
        content = json.dumps(rows, indent=indent).encode()
        manifest = {
            "contract_version": 3,
            "config_entry_id": "fixture",
            "api_base": self.identity["api_base"],
            "account_anchors": ["account-1"],
            "resource": resource,
            "resource_id": resource_id,
            "thermostat_id": 1,
            "source_kind": "provider",
            "acquisition_id": acquisition_id,
            "chunk_index": index,
            "chunk_count": count,
            "format": "json",
            "original_sha256": sha256(content).hexdigest(),
            "original_byte_count": len(content),
            "chunk_byte_offset": 0,
            "start": (start or START).isoformat(),
            "end": (end or self.end).isoformat(),
            "source_end": self.end.isoformat(),
            "acquired_at": self.evaluated_at.isoformat(),
            "unit_contract": "beestat_points_v1",
        }
        return {"manifest": manifest, "original_bytes": content}

    def prepare(self, incoming, **changes):
        return delta.prepare_history_delta(
            incoming,
            self.baseline,
            identity=self.identity,
            start=START,
            end=self.end,
            evaluated_at=self.evaluated_at,
            acquisition_id="routine-1",
            **changes,
        )

    def rows(self, result):
        return [
            row
            for chunk in result["chunks"]
            for row in json.loads(chunk["content"])["rows"]
        ]

    def test_changed_zero_and_late_correction_are_retained_exactly(self):
        self.old_rows([self.row(), self.row(200, start=START + HOUR)])
        changed = [self.row(0), self.row(175, start=START + HOUR)]
        result = self.prepare([self.incoming(changed)])
        self.assertEqual(self.rows(result), changed)
        self.assertEqual(result["changed_rows"], 2)
        self.assertEqual(
            result["baseline_source_revision"], self.baseline["source_revision"]
        )
        self.assertEqual(result["baseline_digest"], self.baseline["baseline_digest"])

    def test_last_row_wins_across_all_chunks_before_any_comparison(self):
        self.old_rows([self.row()])
        changed = self.incoming([self.row(200)], index=0, count=2)
        restored = self.incoming([self.row()], index=1, count=2)
        self.assertIsNone(self.prepare([restored, changed]))
        restored["original_bytes"] = json.dumps([self.row(0)]).encode()
        restored["manifest"]["original_byte_count"] = len(restored["original_bytes"])
        restored["manifest"]["original_sha256"] = sha256(
            restored["original_bytes"]
        ).hexdigest()
        self.assertEqual(self.rows(self.prepare([restored, changed])), [self.row(0)])

    def test_later_tombstone_or_invalid_value_overwrites_earlier_valid_row(self):
        self.old_rows([self.row()])
        for final in (self.row(None), self.row("invalid"), self.row(0, deleted=True)):
            with self.subTest(final=final):
                result = self.prepare(
                    [
                        self.incoming([self.row(150)], index=0, count=2),
                        self.incoming([final], index=1, count=2),
                    ]
                )
                self.assertEqual(self.rows(result), [final])
        self.old_rows([self.row(0, deleted=True)])
        self.assertIsNone(self.prepare([self.incoming([self.row(0, deleted=True)])]))

    def test_empty_or_shorter_retention_is_not_a_deletion(self):
        self.old_rows([self.row(), self.row(200, start=START + HOUR)])
        before = copy.deepcopy(self.baseline)
        self.assertIsNone(self.prepare([]))
        self.assertIsNone(self.prepare([self.incoming([])]))
        self.assertIsNone(
            self.prepare(
                [self.incoming([self.row(200, start=START + HOUR)], start=START + HOUR)]
            )
        )
        self.assertEqual(self.baseline, before)

    def test_canonical_comparison_ignores_key_order_offset_spelling_and_provider_row_ids(
        self,
    ):
        self.old_rows([self.row(100, id=1, runtime_thermostat_id=2)])
        row = {
            "runtime_thermostat_id": 99,
            "fan": 100,
            "thermostat_id": 1,
            "timestamp": "2026-09-01T00:00:00Z",
            "id": 333,
        }
        self.assertIsNone(self.prepare([self.incoming([row], indent=2)]))

    def test_boolean_and_integer_are_distinct_observations(self):
        self.old_rows([self.row(1)])
        self.assertEqual(
            self.rows(self.prepare([self.incoming([self.row(True)])])), [self.row(True)]
        )

    def test_endpoint_belongs_to_next_half_open_window(self):
        self.assertIsNone(self.prepare([self.incoming([self.row(start=self.end)])]))

    def test_provisional_operation_hour_uses_actual_acquisition_source_bounds(self):
        self.evaluated_at = START + HOUR + timedelta(minutes=31)
        actual_end = START + HOUR + timedelta(minutes=30)
        incoming = self.incoming([self.row(start=START + HOUR)], end=actual_end)
        incoming["manifest"]["source_end"] = actual_end.isoformat()
        result = self.prepare([incoming])
        self.assertEqual(result["end"], self.end.isoformat())
        chunk = result["chunks"][0]
        self.assertEqual(chunk["manifest"]["end"], actual_end.isoformat())
        self.assertEqual(
            json.loads(chunk["content"])["acquisition"]["bounds"]["end"],
            actual_end.isoformat(),
        )
        self.assertEqual(
            chunk["manifest"]["acquired_at"], self.evaluated_at.isoformat()
        )

    def test_input_commitment_is_exact_bytes_and_delta_is_its_own_original(self):
        incoming = self.incoming([self.row()], indent=3)
        result = self.prepare([incoming])
        chunk = result["chunks"][0]
        envelope = json.loads(chunk["content"])
        acquisition = envelope["acquisition"]
        self.assertFalse(acquisition["original_bytes_retained"])
        self.assertEqual(acquisition["original_bytes_kind"], "integration_json_export")
        self.assertEqual(
            acquisition["original_chunks"][0]["sha256"],
            sha256(incoming["original_bytes"]).hexdigest(),
        )
        self.assertEqual(
            acquisition["original_chunks"][0]["byte_count"],
            len(incoming["original_bytes"]),
        )
        self.assertEqual(
            chunk["manifest"]["original_sha256"], sha256(chunk["content"]).hexdigest()
        )
        self.assertEqual(
            chunk["manifest"]["original_byte_count"], len(chunk["content"])
        )
        self.assertEqual(chunk["manifest"]["chunk_byte_offset"], 0)
        self.assertEqual(
            sources.parse_source(chunk["content"], chunk["manifest"]), [self.row()]
        )

    def test_baseline_mutation_and_evaluation_version_are_rejected(self):
        incoming = [self.incoming([self.row()])]
        self.baseline["source_revision"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "history_delta_baseline_changed"):
            self.prepare(incoming)
        self.seal()
        self.baseline["evaluation_version"] = "unknown"
        self.seal()
        with self.assertRaisesRegex(ValueError, "history_delta_evaluation_version"):
            self.prepare(incoming)

    def test_prepared_bytes_keep_before_after_binding_when_callers_change_inputs(self):
        self.old_rows([self.row()])
        incoming = [self.incoming([self.row(0, deleted=True)])]
        inputs_before = copy.deepcopy((self.baseline, incoming))
        result = self.prepare(incoming)
        saved = copy.deepcopy(result)
        self.assertEqual((self.baseline, incoming), inputs_before)
        self.baseline["source_revision"] = "b" * 64
        self.seal()
        incoming[0]["manifest"]["resource_id"] = 99
        self.assertEqual(result, saved)
        self.assertEqual(
            json.loads(result["chunks"][0]["content"])["baseline_source_revision"],
            "a" * 64,
        )

    def test_incomplete_duplicate_or_unordered_acquisition_identity_is_rejected(self):
        cases = (
            [self.incoming([], count=2)],
            [self.incoming([], count=2), self.incoming([], count=2)],
            [self.incoming([], acquisition_id="old-acquisition")],
        )
        for incoming in cases:
            with self.subTest(incoming=incoming), self.assertRaises(ValueError):
                self.prepare(incoming)

    def test_unsafe_account_resource_parent_and_original_hash_are_rejected(self):
        for field, value in (
            ("config_entry_id", "other"),
            ("api_base", "https://invalid.example/"),
            ("account_anchors", ["other"]),
            ("thermostat_id", 2),
            ("original_sha256", "f" * 64),
        ):
            incoming = self.incoming([self.row()])
            incoming["manifest"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare([incoming])
        with self.assertRaisesRegex(ValueError, "history_source_row_identity"):
            self.prepare([self.incoming([self.row(thermostat_id=99)])])
        self.baseline["identity"] = {
            **self.baseline["identity"],
            "account_anchors": ["other"],
        }
        self.seal()
        with self.assertRaisesRegex(ValueError, "history_delta_baseline_identity"):
            self.prepare([self.incoming([self.row()])])

    def test_baseline_conflicts_remain_bound_and_are_not_cleared(self):
        self.old_rows([self.row()])
        resource = self.baseline["resources"]["runtime_thermostat:1"]
        resource["conflict_hours"] = [START.isoformat()]
        resource["blocked_windows"] = [
            {
                "start": START.isoformat(),
                "end": self.end.isoformat(),
                "reason": "unplaceable_timestamp",
            }
        ]
        self.seal()
        before = copy.deepcopy(self.baseline)
        result = self.prepare([self.incoming([self.row(0)])])
        self.assertEqual(self.baseline, before)
        self.assertEqual(result["baseline_digest"], before["baseline_digest"])

    def test_boolean_resource_identity_cannot_alias_integer_owner(self):
        self.old_rows([self.row()])
        resource = self.baseline["resources"].pop("runtime_thermostat:1")
        resource["resource_id"] = True
        self.baseline["resources"]["runtime_thermostat:True"] = resource
        self.seal()
        with self.assertRaisesRegex(ValueError, "history_delta_resource_identity"):
            self.prepare([self.incoming([self.row(0)])])

    def test_unplaceable_invalid_rows_are_retained_as_challenging_evidence(self):
        invalid = {"timestamp": "invalid", "thermostat_id": 1, "fan": None}
        self.assertEqual(self.rows(self.prepare([self.incoming([invalid])])), [invalid])

    def test_multiple_resources_keep_separate_manifests_and_no_missing_resource_deletion(
        self,
    ):
        self.old_rows([self.row()])
        sensor = {
            "sensor_id": 2,
            "thermostat_id": 1,
            "timestamp": START.isoformat(),
            "temperature": 70,
        }
        self.old_rows([sensor], resource="runtime_sensor", resource_id=2)
        result = self.prepare([self.incoming([self.row(0)])])
        self.assertEqual(result["changed_resources"], ["runtime_thermostat:1"])
        self.assertEqual(len(result["chunks"]), 1)

    def test_row_limit_is_applied_after_whole_acquisition_resolution(self):
        self.end = START + timedelta(days=40)
        self.evaluated_at = self.end + HOUR
        self.baseline["end"] = self.end.isoformat()
        self.seal()
        rows = [
            self.row(index, start=START + index * timedelta(minutes=5))
            for index in range(10001)
        ]
        result = self.prepare(
            [
                self.incoming(rows[:6000], index=0, count=2),
                self.incoming(rows[6000:], index=1, count=2),
            ]
        )
        self.assertEqual([item["row_count"] for item in result["chunks"]], [10000, 1])
        self.assertEqual(self.rows(result), rows)
        self.assertTrue(
            all(len(item["content"]) <= 8 * 1024 * 1024 for item in result["chunks"])
        )
        self.assertEqual(
            [item["manifest"]["chunk_index"] for item in result["chunks"]], [0, 1]
        )

    def test_byte_limit_splits_changed_rows_without_reencoding_the_input_commitment(
        self,
    ):
        rows = [
            self.row(
                index, start=START + timedelta(minutes=5 * index), annotation="x" * 1000
            )
            for index in range(5)
        ]
        incoming = self.incoming(rows)
        with patch.object(delta, "MAX_SOURCE_BYTES", 3500):
            result = self.prepare([incoming])
        self.assertGreater(len(result["chunks"]), 1)
        self.assertTrue(all(len(item["content"]) <= 3500 for item in result["chunks"]))
        self.assertEqual(self.rows(result), rows)


if __name__ == "__main__":
    unittest.main()
