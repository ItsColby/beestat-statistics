"""Async Beestat API client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp


class BeestatApiError(RuntimeError):
    """Raised when Beestat returns an unusable response."""


class BeestatAuthError(BeestatApiError):
    """Raised when Beestat rejects the configured API key."""


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
            rows: list[dict[str, Any]] = []
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
        raise BeestatApiError(f"{resource}.{method} returned an error: {detail}")
    success = payload.get("success")
    if success is False or success == 0:
        detail = payload.get("message") or payload.get("errors") or payload.get("error")
        if _is_auth_error(detail):
            raise BeestatAuthError(f"{resource}.{method} authentication failed")
        suffix = f": {detail}" if detail else ""
        raise BeestatApiError(f"{resource}.{method} returned an unsuccessful response{suffix}")
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
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._api_base = api_base.rstrip("/") + "/"
        self._redactions = _redaction_replacements(
            api_key=api_key,
            api_base=self._api_base,
        )
        self._timeout = timeout
        self._retries = retries

    def redact_error(self, err: Exception | str) -> str:
        """Return an error string safe to expose in Home Assistant state."""

        return _redact_text(str(err), self._redactions)

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
                    async with self._session.get(self._api_base, params=params) as response:
                        if response.status in (401, 403):
                            raise BeestatAuthError(
                                (
                                    f"{resource}.{method} authentication failed "
                                    f"with HTTP {response.status}"
                                )
                            )
                        if response.status >= 400:
                            body = self.redact_error(await response.text())[:200]
                            raise BeestatApiError(
                                f"{resource}.{method} returned HTTP {response.status}: {body}"
                            )
                        payload = await response.json(content_type=None)
                return _unwrap_response(payload, resource, method)
            except BeestatAuthError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, BeestatApiError) as err:
                last_error = err
                if attempt == self._retries:
                    break
                await asyncio.sleep(2**attempt)

        detail = self.redact_error(last_error or "unknown error")
        raise BeestatApiError(
            f"Failed Beestat call {resource}.{method}: {detail}"
        ) from last_error

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
