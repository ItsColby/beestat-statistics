"""Reject public repository content that resembles private data or secrets."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
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
        "local hostname",
        re.compile(r"(?i)(?:[a-z0-9_-]+\x2e)+(?:home|lan|local)(?![a-z0-9_-])"),
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


def _candidate_files(root: Path = ROOT) -> list[Path]:
    top_level = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
    )
    is_repository_root = (
        top_level.returncode == 0
        and Path(top_level.stdout.decode("utf-8").strip()).resolve() == root.resolve()
    )
    tracked = (
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=False,
            capture_output=True,
        )
        if is_repository_root
        else None
    )
    if tracked is not None and tracked.returncode == 0:
        paths = [
            root / raw.decode("utf-8") for raw in tracked.stdout.split(b"\0") if raw
        ]
    else:
        paths = [
            path
            for path in root.rglob("*")
            if not IGNORED_DIRECTORY_NAMES.intersection(path.parts)
            and not any(part.endswith(".egg-info") for part in path.parts)
        ]

    return sorted(path for path in paths if path.is_file() and not path.is_symlink())


def _text_failures(text: str) -> set[str]:
    failures = {
        label for label, pattern in PUBLIC_SAFETY_PATTERNS if pattern.search(text)
    }
    for match in EMAIL_RE.finditer(text):
        address = match.group(0).casefold()
        domain = match.group(1).casefold()
        if address not in ALLOWED_EMAILS and domain not in ALLOWED_EMAIL_DOMAINS:
            failures.add("non-example email address")
    return failures


def run_guard(root: Path = ROOT) -> tuple[int, list[str]]:
    files = _candidate_files(root)
    failures: set[str] = set()
    for path in files:
        relative = path.relative_to(root)
        relative_posix = relative.as_posix()
        raw = path.read_bytes()
        is_binary = b"\0" in raw
        try:
            text = "" if is_binary else raw.decode("utf-8")
        except UnicodeDecodeError:
            is_binary = True

        if is_binary:
            expected_hash = REVIEWED_BINARY_SHA256.get(relative_posix)
            if expected_hash != hashlib.sha256(raw).hexdigest():
                failures.add(f"{relative}: unreviewed binary content")
            continue

        for label in _text_failures(text):
            failures.add(f"{relative}: {label}")
    return len(files), sorted(failures)


def main() -> int:
    file_count, failures = run_guard()
    if failures:
        raise SystemExit("Public safety guard failed:\n" + "\n".join(failures))
    print(f"Public safety guard passed for {file_count} repository files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
