"""Tests for the public repository safety guard."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.check_public_safety import (
    ROOT,
    _text_failures,
    run_guard,
)


class PublicSafetyGuardTests(unittest.TestCase):
    def test_generic_patterns_reject_sensitive_shapes(self) -> None:
        samples = {
            "absolute Windows path": "C:" + r"\Users\Example\file.txt",
            "local user path": "/home/" + "example/private.txt",
            "local hostname": "router" + ".local",
            "non-example email address": "person" + "@real-domain.dev",
            "GitHub token": "ghp_" + ("a" * 36),
        }
        for expected, sample in samples.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, _text_failures(sample))

    def test_all_rfc1918_address_ranges_are_rejected(self) -> None:
        samples = (
            "10" + ".1.2.3",
            "172" + ".16.1.2",
            "172" + ".31.1.2",
            "192" + ".168.1.2",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIn("private IPv4 address", _text_failures(sample))

    def test_public_examples_and_github_noreply_are_allowed(self) -> None:
        text = " ".join(
            (
                "person@example.com",
                "person@example.test",
                "1361774+ItsColby@users.noreply.github.com",
                "noreply@github.com",
            )
        )
        self.assertEqual(set(), _text_failures(text))

    def test_guard_scans_tracked_and_untracked_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Safe public text.\n", encoding="utf-8")
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual([], failures)

    def test_guard_scans_text_without_a_file_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitignore").write_text("Safe public text.\n", encoding="utf-8")
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual([], failures)

    def test_guard_rejects_unreviewed_binary_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00private")
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual(["image.png: unreviewed binary content"], failures)

    def test_guard_ignores_generated_cache_directories_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Safe public text.\n", encoding="utf-8")
            cache = root / ".ruff_cache"
            cache.mkdir()
            (cache / "cache.bin").write_bytes(b"\x00generated")
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual([], failures)

    def test_guard_scans_tree_nested_inside_parent_repository(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "README.md").write_text("Safe public text.\n", encoding="utf-8")
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
