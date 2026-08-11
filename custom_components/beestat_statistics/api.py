"""Async Beestat API client."""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

import aiohttp

from .url_validation import normalize_api_base

_FINGERPRINT_COMPONENT_MAX = 48
_FINGERPRINT_MAX = 160
_DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


class BeestatApiError(RuntimeError):
    """Raised when Beestat returns an unusable response."""


class BeestatAuthError(BeestatApiError):
    """Raised when Beestat rejects the configured API key."""


class _BeestatNonRetryableError(BeestatApiError):
    """Raised when another identical request cannot plausibly correct the error."""


def _reject_http_redirect(status: int, resource: str, method: str) -> None:
    """Fail closed instead of forwarding API-key query parameters."""

    if 300 <= status < 400:
        raise _BeestatNonRetryableError(
            f"{resource}.{method} refused HTTP redirect {status}"
        )


def exception_fingerprint(err: BaseException) -> str:
    """Return a bounded private-safe location for an unexpected exception."""

    exception_type = _fingerprint_component(type(err).__name__, "Exception")
    for frame in reversed(traceback.extract_tb(err.__traceback__)):
        normalized = frame.filename.replace("\\", "/")
        marker = "/custom_components/beestat_statistics/"
        if marker not in normalized:
            continue
        module = _fingerprint_component(Path(normalized).stem, "module")
        function = _fingerprint_component(frame.name, "function")
        fingerprint = f"{exception_type}@{module}:{function}:{frame.lineno}"
        return fingerprint[:_FINGERPRINT_MAX]
    return exception_type


def _fingerprint_component(value: str, fallback: str) -> str:
    """Return one conservative fixed-size fingerprint component."""

    sanitized = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in value
    )
    return sanitized[:_FINGERPRINT_COMPONENT_MAX] or fallback


def _is_auth_error(value: Any) -> bool:
    text = _error_text(value).lower()
    return any(
        marker in text
        for marker in (
            "api key",
            "api_key",
            "auth",
            "credential",
            "forbidden",
            "unauthorized",
            "permission",
            "token",
        )
    )


def _error_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_error_text(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return " ".join(_error_text(item) for item in value)
    return str(value)


def _redaction_replacements(
    *,
    api_key: str,
    api_base: str,
) -> tuple[tuple[str, str], ...]:
    replacements: list[tuple[str, str]] = []
    if api_key:
        replacements.append((api_key, "<redacted>"))
    base = api_base.rstrip("/")
    if base:
        replacements.append((f"{base}/", "<redacted-url>/"))
        replacements.append((base, "<redacted-url>"))
    return tuple(replacements)


def _redact_text(value: str, replacements: tuple[tuple[str, str], ...]) -> str:
    redacted = value
    for text, replacement in replacements:
        redacted = redacted.replace(text, replacement)
    return redacted


def _normalize_rows(
    data: Any,
    *,
    allow_boolean: bool = False,
) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, bool):
        if not allow_boolean:
            raise BeestatApiError("Unexpected response data shape: bool")
        return []
    if isinstance(data, list):
        rows: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                raise BeestatApiError(
                    f"Unexpected response row shape: {type(row).__name__}"
                )
            rows.append(row)
        return rows
    if isinstance(data, dict):
        if data and all(isinstance(value, dict) for value in data.values()):
            rows = []
            for key, value in data.items():
                row = dict(value)
                row.setdefault("id", key)
                rows.append(row)
            return rows
        return [data]
    raise BeestatApiError(f"Unexpected response data shape: {type(data).__name__}")


def _unwrap_response(payload: Any, resource: str, method: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    if payload.get("error"):
        detail = payload["error"]
        if detail is True:
            detail = payload.get("message") or payload.get("errors") or payload
        if _is_auth_error(detail):
            raise BeestatAuthError(f"{resource}.{method} authentication failed")
        raise BeestatApiError(f"{resource}.{method} returned an error")
    success = payload.get("success")
    if success is False or success == 0:
        detail = payload.get("message") or payload.get("errors") or payload.get("error")
        if _is_auth_error(detail):
            raise BeestatAuthError(f"{resource}.{method} authentication failed")
        raise BeestatApiError(f"{resource}.{method} returned an unsuccessful response")
    if "data" in payload:
        return payload["data"]
    return payload


class BeestatClient:
    """Small wrapper around the Beestat query API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        api_base: str,
        *,
        timeout: int = 60,
        retries: int = 3,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._session = session
        self._api_key = api_key
        self._api_base = normalize_api_base(api_base).rstrip("/") + "/"
        self._redactions = _redaction_replacements(
            api_key=api_key,
            api_base=self._api_base,
        )
        self._timeout = timeout
        self._retries = retries
        self._max_response_bytes = max_response_bytes

    def redact_error(self, err: Exception) -> str:
        """Return an error string safe to expose in Home Assistant state."""

        if isinstance(err, BeestatAuthError | BeestatApiError):
            return _redact_text(str(err), self._redactions)
        if isinstance(err, asyncio.TimeoutError):
            return "Beestat request timed out"
        if isinstance(err, aiohttp.ClientError):
            return "Beestat network request failed"
        if isinstance(err, ValueError):
            return "Beestat returned invalid response data"
        return f"Unexpected integration error ({type(err).__name__})"

    async def async_call(
        self,
        resource: str,
        method: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_boolean_response: bool = False,
    ) -> list[dict[str, Any]]:
        """Call Beestat and return a normalized list of row dictionaries."""

        return _normalize_rows(
            await self.async_call_raw(resource, method, arguments),
            allow_boolean=allow_boolean_response,
        )

    async def async_call_raw(
        self,
        resource: str,
        method: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call Beestat and return the unnormalized response data."""

        params: dict[str, str] = {
            "api_key": self._api_key,
            "resource": resource,
            "method": method,
        }
        if arguments is not None:
            params["arguments"] = json.dumps(arguments, separators=(",", ":"))

        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._session.get(
                        self._api_base,
                        params=params,
                        allow_redirects=False,
                    ) as response:
                        if response.status in (401, 403):
                            raise BeestatAuthError(
                                f"{resource}.{method} authentication failed "
                                f"with HTTP {response.status}"
                            )
                        if (
                            400 <= response.status < 500
                            and response.status not in _RETRYABLE_HTTP_STATUSES
                        ):
                            raise _BeestatNonRetryableError(
                                f"{resource}.{method} returned HTTP {response.status}"
                            )
                        _reject_http_redirect(response.status, resource, method)
                        if response.status >= 400:
                            raise BeestatApiError(
                                f"{resource}.{method} returned HTTP {response.status}"
                            )
                        payload = await self._async_read_json(response)
                data = _unwrap_response(payload, resource, method)
                if method == "sync" and data is False:
                    raise BeestatApiError(
                        f"{resource}.{method} returned an unsuccessful response"
                    )
                return data
            except BeestatAuthError:
                raise
            except _BeestatNonRetryableError as err:
                raise BeestatApiError(
                    f"Failed Beestat call {resource}.{method}: {self.redact_error(err)}"
                ) from None
            except (
                TimeoutError,
                aiohttp.ClientError,
                ValueError,
                BeestatApiError,
            ) as err:
                last_error = err
                if attempt == self._retries:
                    break
                await asyncio.sleep(2**attempt)

        detail = (
            self.redact_error(last_error)
            if last_error is not None
            else "Beestat request failed"
        )
        raise BeestatApiError(
            f"Failed Beestat call {resource}.{method}: {detail}"
        ) from None

    async def _async_read_json(self, response: aiohttp.ClientResponse) -> Any:
        """Decode one response without retaining an unbounded remote body."""

        content_length = response.content_length
        if content_length is not None and content_length > self._max_response_bytes:
            raise _BeestatNonRetryableError("Beestat response exceeded the size limit")

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
            size += len(chunk)
            if size > self._max_response_bytes:
                raise _BeestatNonRetryableError(
                    "Beestat response exceeded the size limit"
                )
            chunks.append(chunk)
        return json.loads(b"".join(chunks))

    async def async_sync_runtime(self) -> list[dict[str, Any]]:
        """Ask Beestat to sync runtime data before reading it."""

        return await self.async_sync_resource("runtime")

    async def async_sync_resource(self, resource: str) -> list[dict[str, Any]]:
        """Ask Beestat to sync one resource before reading it."""

        return await self.async_call(
            resource,
            "sync",
            allow_boolean_response=True,
        )

    async def async_read_id(self, resource: str) -> list[dict[str, Any]]:
        """Read all rows for a Beestat resource."""

        return await self.async_call(resource, "read_id")

    async def async_dismiss_alert(self, thermostat_id: int, guid: str) -> None:
        """Dismiss one Beestat alert by thermostat and alert GUID."""

        await self.async_call_raw(
            "thermostat",
            "dismiss_alert",
            {
                "thermostat_id": thermostat_id,
                "guid": guid,
            },
        )

    async def async_read_runtime_thermostat_summary(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Read runtime_thermostat_summary rows for a local date window."""

        return await self.async_call(
            "runtime_thermostat_summary",
            "read_id",
            {
                "attributes": {
                    "date": {
                        "operator": "between",
                        "value": [start_date, end_date],
                    },
                }
            },
        )

    async def async_read_runtime_sensor(
        self,
        sensor_id: int,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Read runtime_sensor rows for one Beestat sensor and timestamp window."""

        return await self.async_call(
            "runtime_sensor",
            "read",
            {
                "attributes": {
                    "sensor_id": sensor_id,
                    "timestamp": {
                        "operator": "between",
                        "value": [start, end],
                    },
                }
            },
        )

    async def async_read_runtime_thermostat(
        self,
        thermostat_id: int,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Read runtime_thermostat rows for one thermostat and timestamp window."""

        return await self.async_call(
            "runtime_thermostat",
            "read",
            {
                "attributes": {
                    "thermostat_id": thermostat_id,
                    "timestamp": {
                        "operator": "between",
                        "value": [start, end],
                    },
                }
            },
        )
