"""Shared test helpers for `requiem.providers.*` suites.

Mocking strategy
================
Both `AnthropicProvider` and `OpenAIProvider` instantiate their SDKs with a
custom `httpx.AsyncClient` whose transport is `httpx.MockTransport`. That
is the *lowest stable seam* — it exercises every line of the SDK's URL
building, header parsing, model decoding, and exception classification.
Patching SDK methods directly would skip exactly those layers, which is
where the error-mapping bugs the providers are responsible for shipping
out actually live.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx


def mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Wrap a handler in a `MockTransport`. Tests typically use
    `static_handler(status, body, headers)` for a single shot or a list
    pattern via `sequence_handler([...])`.
    """
    return httpx.MockTransport(handler)


def static_handler(
    *,
    status: int = 200,
    json_body: dict[str, Any] | None = None,
    text_body: str | None = None,
    headers: dict[str, str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Always-respond handler."""

    def _h(_req: httpx.Request) -> httpx.Response:
        if json_body is not None:
            return httpx.Response(
                status,
                content=json.dumps(json_body).encode("utf-8"),
                headers={"content-type": "application/json", **(headers or {})},
            )
        return httpx.Response(
            status, content=(text_body or "").encode("utf-8"), headers=headers or {}
        )

    return _h


def sequence_handler(
    responses: list[httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Respond with the next pre-built response in `responses`, cycling
    via index. Useful for asserting an SDK only made one call (any
    second call raises IndexError loudly).
    """
    state = {"i": 0}

    def _h(_req: httpx.Request) -> httpx.Response:
        i = state["i"]
        state["i"] = i + 1
        return responses[i]

    return _h


def request_recorder() -> tuple[
    list[httpx.Request],
    Callable[[Callable[[httpx.Request], httpx.Response]], Callable[[httpx.Request], httpx.Response]],
]:
    """Wrap any handler so it records every inbound request."""
    seen: list[httpx.Request] = []

    def wrap(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> Callable[[httpx.Request], httpx.Response]:
        def _h(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return handler(req)

        return _h

    return seen, wrap
