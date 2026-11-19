# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Security headers on every response (pure ASGI — does not buffer bodies)."""
from __future__ import annotations

from typing import Any

from . import config

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; object-src 'none'")
# Swagger UI (dev only, ENABLE_API_DOCS=1) needs its CDN assets and an inline bootstrap.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
            "https://fastapi.tiangolo.com; frame-ancestors 'none'")

BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


# Request bodies are bounded before anything reads them (a multi-GB POST must not be buffered).
BODY_LIMIT = 6_000_000                     # forms: a 1 MB (UTF-8) CSV can triple when url-encoded
BODY_LIMITS = {"/sync": 6_000_000}         # the sync route also checks its own 5 MB cap


class BodyTooLarge(Exception):
    """Raised from ``receive`` when a streamed body passes the limit (handled as 413)."""


def _limit_for(path: str) -> int:
    for prefix, limit in BODY_LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return limit
    return BODY_LIMIT


async def _send_413(send: Any) -> None:
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": b"request body too large"})


class BodyLimitMiddleware:
    """413 on a declared Content-Length above the limit; a chunked/streamed body is counted
    as it is read and aborts with ``BodyTooLarge`` once it passes the limit."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _limit_for(scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = limit + 1
                if declared > limit or declared < 0:
                    await _send_413(send)
                    return
        received = 0

        async def _receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        await self.app(scope, _receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        csp = DOCS_CSP if path in ("/docs", "/redoc") else CSP

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", [])
                           if h[0].lower() not in (b"server",)]
                headers += BASE_HEADERS
                headers.append((b"content-security-policy", csp.encode()))
                if config.COOKIE_SECURE:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send)
