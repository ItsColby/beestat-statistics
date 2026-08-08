"""Validation for the credential-bearing Beestat API base URL."""

from __future__ import annotations

from urllib.parse import urlsplit


def normalize_api_base(value: object) -> str:
    """Return a bounded HTTPS API base or raise ``ValueError``."""

    text = str(value).strip()
    if not text or "\\" in text or any(character.isspace() for character in text):
        raise ValueError("Invalid Beestat API URL")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as err:
        raise ValueError("Invalid Beestat API URL") from err
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Beestat API URL must use HTTPS")
    if "%" in parsed.hostname or port == 0:
        raise ValueError("Invalid Beestat API URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Beestat API URL must not contain user information")
    if parsed.query or parsed.fragment or "?" in text or "#" in text:
        raise ValueError("Beestat API URL must not contain a query or fragment")
    return text
