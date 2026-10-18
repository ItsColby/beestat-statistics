"""Fake aiohttp response and session stand-ins for API tests."""

from __future__ import annotations

import json


class _FakeResponse:
    def __init__(
        self,
        payload,
        *,
        status: int = 200,
        json_error: BaseException | None = None,
        include_content_length: bool = True,
    ) -> None:
        self.status = status
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.content = _FakeContent(body, json_error)
        self.content_length = len(body) if include_content_length else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _FakeContent:
    def __init__(self, body: bytes, read_error: BaseException | None = None) -> None:
        self._body = body
        self._read_error = read_error

    async def iter_chunked(self, size: int):
        if self._read_error is not None:
            raise self._read_error
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _FakeSession:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = iter(payloads)
        self.call_count = 0
        self.allow_redirects: list[bool] = []
        self.requests: list[dict[str, str]] = []

    def get(self, _url, *, params, allow_redirects: bool):
        self.call_count += 1
        self.allow_redirects.append(allow_redirects)
        self.requests.append(dict(params))
        response = next(self._payloads)
        return (
            response if isinstance(response, _FakeResponse) else _FakeResponse(response)
        )
