"""Reject public repository content that resembles private data or secrets."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 8 * 1024 * 1024
ALLOWED_EMAILS = {"noreply@github.com"}
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "example.test",
    "users.noreply.github.com",
}
REVIEWED_BINARY_SHA256 = {
    "custom_components/beestat_statistics/brand/icon.png": (
        "6b9995752bf6d548654c790f79e481ea32ecec8135c55dff2811e1c2406e1f1e"
    ),
}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}

HOSTNAME_TOKEN_RE = re.compile(r"[A-Z0-9_.-]+", re.IGNORECASE)
LOCAL_HOSTNAME_SUFFIXES = {"home", "lan", "local"}
EMAIL_LOCAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
)
EMAIL_DOMAIN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
)
PUBLIC_SAFETY_PATTERNS = (
    (
        "absolute Windows path",
        re.compile(r"(?<![A-Z0-9])[A-Z]:[\\/]", re.IGNORECASE),
    ),
    (
        "local user path",
        re.compile(r"(?i)(?:\x2fhome\x2f[^/\s]+\x2f|\x2fUsers\x2f[^/\s]+\x2f)"),
    ),
    (
        "private IPv4 address",
        re.compile(
            r"(?<!\d)(?:"
            r"10\.(?:\d{1,3}\.){2}\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r")(?!\d)"
        ),
    ),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "GitHub token",
        re.compile(
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|"
            r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"
        ),
    ),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
)


def _contains_local_hostname(text: str) -> bool:
    """Return whether text contains a dotted local hostname in linear time."""

    for match in HOSTNAME_TOKEN_RE.finditer(text):
        prior_label_count = 0
        for label in match.group(0).casefold().split("."):
            if not _valid_hostname_label(label):
                prior_label_count = 0
                continue
            if prior_label_count and label in LOCAL_HOSTNAME_SUFFIXES:
                return True
            prior_label_count += 1
    return False


def _valid_hostname_label(label: str) -> bool:
    return bool(label) and all(char.isalnum() or char in "-_" for char in label)


def _email_addresses(text: str) -> Iterator[tuple[str, str]]:
    """Yield regex-compatible email and domain pairs without backtracking."""

    search_from = 0
    while (at_index := text.find("@", search_from)) >= 0:
        local_run_start = at_index
        while local_run_start > 0 and text[local_run_start - 1] in EMAIL_LOCAL_CHARS:
            local_run_start -= 1
        local_start = next(
            (
                index
                for index in range(local_run_start, at_index)
                if _is_word_boundary(text, index)
            ),
            None,
        )

        domain_start = at_index + 1
        domain_run_end = domain_start
        while domain_run_end < len(text) and text[domain_run_end] in EMAIL_DOMAIN_CHARS:
            domain_run_end += 1

        valid_end: int | None = None
        last_dot = -1
        top_level_length = 0
        top_level_is_alpha = False
        for index in range(domain_start, domain_run_end):
            char = text[index]
            if char == ".":
                last_dot = index
                top_level_length = 0
                top_level_is_alpha = True
            elif last_dot >= 0:
                top_level_length += 1
                top_level_is_alpha = top_level_is_alpha and char.isalpha()
            if (
                last_dot > domain_start
                and top_level_is_alpha
                and top_level_length >= 2
                and _is_word_boundary(text, index + 1)
            ):
                valid_end = index + 1

        if local_start is not None and valid_end is not None:
            domain = text[domain_start:valid_end]
            yield f"{text[local_start:at_index]}@{domain}", domain
        search_from = at_index + 1


def _is_word_boundary(text: str, index: int) -> bool:
    left_is_word = index > 0 and _is_word_char(text[index - 1])
    right_is_word = index < len(text) and _is_word_char(text[index])
    return left_is_word != right_is_word


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _git_command(*arguments: str) -> list[str]:
    local_names = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
    # GIT_CONFIG_KEY/VALUE entries are inert without GIT_CONFIG_COUNT; native
    # hook cleanup unsets the count and may leave those unused entries behind.
    inherited = sorted(name for name in os.environ if name in local_names)
    if inherited:
        raise ValueError(
            "Inherited local Git overrides are not supported: " + ", ".join(inherited)
        )
    return ["git", "--no-replace-objects", "--no-optional-locks", *arguments]


def _candidate_files(root: Path = ROOT) -> list[Path]:
    top_level = subprocess.run(
        _git_command("-C", str(root), "rev-parse", "--show-toplevel"),
        check=False,
        capture_output=True,
        timeout=30,
    )
    is_repository_root = (
        top_level.returncode == 0
        and Path(os.fsdecode(top_level.stdout).rstrip("\r\n")).resolve()
        == root.resolve()
    )
    if top_level.returncode != 0 and (root / ".git").exists():
        raise RuntimeError("Unable to inspect the repository")
    if is_repository_root:
        tracked = subprocess.run(
            _git_command(
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if tracked.returncode != 0:
            raise RuntimeError("Unable to enumerate repository files")
        paths = [root / os.fsdecode(raw) for raw in tracked.stdout.split(b"\0") if raw]
    else:
        paths = list(_export_files(root))

    return sorted({path for path in paths if path.exists() or path.is_symlink()})


def _export_files(root: Path) -> Iterator[Path]:
    """Walk an export without following links or descending into generated trees."""

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, directories, filenames in os.walk(root, onerror=raise_walk_error):
        parent = Path(directory)
        for name in directories[:]:
            path = parent / name
            if name in IGNORED_DIRECTORY_NAMES or name.endswith(".egg-info"):
                directories.remove(name)
            elif path.is_symlink() or path.is_junction():
                directories.remove(name)
                yield path
        for name in filenames:
            yield parent / name


def _text_failures(text: str) -> set[str]:
    failures = {
        label for label, pattern in PUBLIC_SAFETY_PATTERNS if pattern.search(text)
    }
    if _contains_local_hostname(text):
        failures.add("local hostname")
    for raw_address, raw_domain in _email_addresses(text):
        address = raw_address.casefold()
        domain = raw_domain.casefold()
        if address not in ALLOWED_EMAILS and domain not in ALLOWED_EMAIL_DOMAINS:
            failures.add("non-example email address")
    return failures


def _content_failures(relative_posix: str, raw: bytes) -> set[str]:
    """Inspect UTF-8 text or require an exact reviewed binary hash."""

    if b"\0" not in raw:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return _text_failures(text)
    if REVIEWED_BINARY_SHA256.get(relative_posix) != hashlib.sha256(raw).hexdigest():
        return {"unreviewed binary content"}
    return set()


def is_linked_source(root: Path, path: Path) -> bool:
    """Apply the guard's existing leaf and ancestor link policy."""
    return any(
        candidate.is_symlink() or candidate.is_junction()
        for candidate in (path, *path.parents)
        if candidate != root and root in candidate.parents
    )


def require_source_paths(root: Path, paths) -> None:
    """Reject linked inputs before a consumer reads or copies their content."""
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Source admission requires repository-relative paths")
        if is_linked_source(root, root / relative):
            raise ValueError("Source admission refused an unreviewed linked path")


def run_guard(root: Path = ROOT) -> tuple[int, list[str]]:
    files = _candidate_files(root)
    failures: set[str] = set()
    for path in files:
        relative = path.relative_to(root)
        relative_posix = relative.as_posix()
        path_failures = _text_failures(relative_posix)
        label_path = (
            "<sensitive path>" if path_failures else ascii(relative_posix)[1:-1]
        )
        for label in path_failures:
            failures.add(f"{label_path}: {label} in filename")
        if is_linked_source(root, path):
            failures.add(f"{label_path}: symbolic link requires review")
            continue
        if not path.is_file():
            failures.add(f"{label_path}: non-regular file requires review")
            continue
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            failures.add(f"{label_path}: unreadable file")
            continue
        if len(raw) > MAX_FILE_BYTES:
            failures.add(f"{label_path}: file exceeds review size limit")
            continue
        for label in _content_failures(relative_posix, raw):
            failures.add(f"{label_path}: {label}")
    if not files:
        failures.add("No repository files were discovered")
    return len(files), sorted(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-source-paths", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_source_paths:
            require_source_paths(
                ROOT,
                (
                    os.fsdecode(raw)
                    for raw in sys.stdin.buffer.read().split(b"\0")
                    if raw
                ),
            )
            return 0
        file_count, failures = run_guard()
    except OSError, RuntimeError, ValueError, subprocess.TimeoutExpired:
        print(
            "Public safety guard could not enumerate repository files.", file=sys.stderr
        )
        return 2
    if failures:
        print("Public safety guard failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"Public safety guard passed for {file_count} repository files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
