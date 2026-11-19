# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""CSRF on every state-changing web route, security headers, CSP-compatible templates."""
import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app
from tests.conftest import Browser, as_user

ROOT = Path(__file__).resolve().parent.parent


def _routes():
    def walk(rs):
        for r in rs:
            if isinstance(r, APIRoute):
                yield r
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
    return list(walk(app.routes))


def test_every_unsafe_web_route_rejects_missing_csrf(world):
    """Deny by default: enumerate ALL POST routes; none may accept a request without the token."""
    b = as_user("owner@a.test")
    ids = {"oid": world["orderA"], "lid": world["wlA"], "cid": world["clientA"], "uid": world["desk"],
           "tid": 1, "did": 1}
    checked = 0
    for r in _routes():
        if "POST" not in r.methods or r.path.startswith(("/api/", "/sync")):
            continue
        url = re.sub(r"\{(\w+)\}", lambda m: str(ids.get(m.group(1), 1)), r.path)
        resp = b.post(url, {"x": "1"}, csrf=False)
        assert resp.status_code == 403, (r.path, resp.status_code)
        resp = b.post(url, {"x": "1", "csrf_token": "forged"}, csrf=False)
        assert resp.status_code == 403, (r.path, "forged token accepted")
        checked += 1
    assert checked >= 30


def test_json_endpoint_requires_csrf_header(world):
    b = as_user("owner@a.test")
    r = b.post_json(f"/orders/{world['orderA']}/status", {"status": "done", "expected_version": 1}, csrf=False)
    assert r.status_code == 403


def test_security_headers_and_strict_csp():
    r = Browser().get("/login")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin"
    csp = h["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert "server" not in {k.lower() for k in h.keys()} or "uvicorn" not in h.get("server", "")


def test_api_docs_closed_by_default():
    b = Browser()
    assert b.get("/docs").status_code == 404
    assert b.get("/openapi.json").status_code == 404


def test_templates_have_no_inline_style_or_script():
    """Strict CSP (no 'unsafe-inline') only works if the markup has none."""
    bad = []
    for f in (ROOT / "templates").glob("*.*"):
        text = f.read_text(encoding="utf-8")
        if re.search(r"\sstyle=", text) or re.search(r"\son[a-z]+=", text) or re.search(r"<script>", text):
            bad.append(f.name)
        if re.search(r"<script(?![^>]*\ssrc=)", text):
            bad.append(f.name + " (inline script)")
    assert bad == []


def test_frontend_never_injects_html():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js


def test_authenticated_pages_are_not_cached(world):
    r = as_user("owner@a.test").get("/orders")
    assert r.headers["cache-control"] == "no-store"
