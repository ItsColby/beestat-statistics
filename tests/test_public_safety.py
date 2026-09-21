"""Tests for the public repository safety guard."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.check_public_safety import (
    _candidate_files,
    _text_failures,
    main,
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

    def test_local_hostname_detection_handles_multiple_labels(self) -> None:
        self.assertIn(
            "local hostname",
            _text_failures("service.zone_a" + ".lan"),
        )
        self.assertIn(
            "local hostname",
            _text_failures("router" + ".local.example"),
        )
        self.assertIn(
            "local hostname",
            _text_failures("router" + ".local..example"),
        )
        self.assertNotIn(
            "local hostname",
            _text_failures("service.zone_a" + ".example"),
        )
        self.assertNotIn(
            "local hostname",
            _text_failures("local" + ".example"),
        )

    def test_local_hostname_detection_handles_long_non_match_linearly(self) -> None:
        text = ("segment." * 20_000) + "example"
        self.assertNotIn("local hostname", _text_failures(text))

    def test_public_examples_and_github_noreply_are_allowed(self) -> None:
        text = (
            "person@example.com person@example.test "
            "1361774+ItsColby@users.noreply.github.com noreply@github.com"
        )
        self.assertEqual(set(), _text_failures(text))

    def test_email_detection_preserves_sentence_punctuation(self) -> None:
        self.assertIn(
            "non-example email address",
            _text_failures("Contact person" + "@real-domain.dev."),
        )
        self.assertIn(
            "non-example email address",
            _text_failures("Contact person" + "@real-domain.dev-suffix"),
        )
        self.assertEqual(set(), _text_failures("Contact person@example.com."))
        self.assertEqual(set(), _text_failures("Contact .noreply@github.com"))

    def test_email_detection_rejects_malformed_domain_prefix(self) -> None:
        self.assertNotIn(
            "non-example email address",
            _text_failures("Malformed @" + ".cX"),
        )

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
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            subprocess.run(
                ["git", "-C", str(parent), "init", "-q"],
                check=True,
                capture_output=True,
            )
            (parent / "outside.txt").write_text(
                "Outside the export.\n", encoding="utf-8"
            )
            root = parent / "export"
            root.mkdir()
            (root / "README.md").write_text("Safe public text.\n", encoding="utf-8")
            top_level = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(Path(top_level.stdout.strip()).resolve(), parent.resolve())
            file_count, failures = run_guard(root)
        self.assertEqual(1, file_count)
        self.assertEqual([], failures)

    def test_export_inside_generated_parent_is_still_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".local" / "export"
            root.mkdir(parents=True)
            (root / "README.md").write_text("Safe text", encoding="utf-8")
            self.assertEqual((1, []), run_guard(root))

    def test_sensitive_filename_is_rejected_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filename = "ghp_" + ("a" * 36)
            (root / filename).write_text("Safe text", encoding="utf-8")
            count, failures = run_guard(root)
            self.assertEqual(1, count)
            self.assertEqual(["<sensitive path>: GitHub token in filename"], failures)

    def test_git_overrides_fail_before_enumeration_without_echoing_values(self) -> None:
        for name, value in (
            ("GIT_INDEX_FILE", ""),
            ("GIT_INDEX_FILE", "private-index"),
            ("GIT_DIR", "private-directory"),
            ("GIT_CONFIG_COUNT", "1"),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}),
                patch("scripts.check_public_safety.subprocess.run") as run,
                redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(2, main([]))
                run.assert_not_called()
                self.assertNotIn("private-", output.getvalue())

    def test_git_enumeration_failure_cannot_fall_back_to_filtered_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "scripts.check_public_safety.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, stdout=str(root).encode()),
                        subprocess.CompletedProcess([], 1),
                    ],
                ),
                self.assertRaisesRegex(RuntimeError, "enumerate"),
            ):
                _candidate_files(root)

    def test_git_scans_tracked_ignored_files_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "-C", str(root), "init", "-q"], check=True, capture_output=True
            )
            (root / ".gitignore").write_text("private.txt\n", encoding="utf-8")
            private = root / "private.txt"
            private.write_text("ghp_" + "a" * 36, encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", "private.txt"],
                check=True,
                capture_output=True,
            )
            (root / "new.txt").write_text("Safe text", encoding="utf-8")
            count, failures = run_guard(root)
            self.assertEqual(3, count)
            self.assertEqual(["private.txt: GitHub token"], failures)

    def test_guard_reports_symlinks_instead_of_reading_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("ghp_" + "a" * 36, encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except OSError as err:
                self.skipTest(f"Symlink creation unavailable: {type(err).__name__}")
            with patch(
                "scripts.check_public_safety._candidate_files", return_value=[link]
            ):
                count, failures = run_guard(root)
            self.assertEqual(1, count)
            self.assertEqual(["link.txt: symbolic link requires review"], failures)

    def test_empty_export_and_unreadable_file_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                (0, ["No repository files were discovered"]), run_guard(root)
            )
            path = root / "README.md"
            path.write_text("Safe text", encoding="utf-8")
            with patch.object(Path, "open", side_effect=PermissionError):
                self.assertEqual((1, ["README.md: unreadable file"]), run_guard(root))

    def test_guard_rejects_oversized_files_without_loading_them_fully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_text("long", encoding="utf-8")
            with patch("scripts.check_public_safety.MAX_FILE_BYTES", 2):
                self.assertEqual(
                    (1, ["large.txt: file exceeds review size limit"]), run_guard(root)
                )

    def test_cli_enumeration_errors_have_nonzero_sanitized_output(self) -> None:
        with (
            patch(
                "scripts.check_public_safety.run_guard", side_effect=OSError("private")
            ),
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(2, main([]))
        self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
