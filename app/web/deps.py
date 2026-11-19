"""Web-layer dependencies: session → user → tenant context, CSRF, permissions, rendering.

``get_ctx`` is the only way a web route obtains a ``UserContext``. It
  1. resolves the server-side session (expired / revoked → login),
  2. verifies the CSRF token on every unsafe method (deny by default — a POST route
     cannot reach its body without passing this check),
  3. binds the DB session to the user's tenant (Postgres RLS context).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from .. import config
from ..db import get_session, set_tenant, write_intent
from ..i18n import LOCALES, get_locale, has_key, translator
from ..models import ROLES, WO_STATUSES, Branch, User, UserSession, utcnow
from ..permissions import PERMISSIONS
from ..security import (
    PRE_COOKIE,
    SESSION_COOKIE,
    load_session,
    pre_csrf_token,
    sha256_hex,
    tokens_match,
)
from ..tenancy import UserContext, load_user_context

BASE = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["STATUSES"] = WO_STATUSES
templates.env.globals["ROLES"] = ROLES
templates.env.globals["DEMO_MODE"] = config.DEMO_MODE
templates.env.globals["has_key"] = has_key
STATIC_VERSION = "10"
templates.env.globals["STATIC_VERSION"] = STATIC_VERSION
if config.DEMO_MODE:
    from ..demo import DEMO_ACCOUNTS, DEMO_PASSWORD
    templates.env.globals["demo_accounts"] = DEMO_ACCOUNTS
    templates.env.globals["demo_password"] = DEMO_PASSWORD

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class LoginRequired(Exception):
    """Raised by ``get_ctx``; turned into a redirect (HTML) or 401 (JSON)."""


def wants_json(request: Request) -> bool:
    return ("application/json" in request.headers.get("accept", "")
            or request.headers.get("content-type", "").startswith("application/json"))


async def _submitted_csrf(request: Request) -> str:
    token = request.headers.get("x-csrf-token", "")
    if token:
        return token
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        val = form.get("csrf_token", "")
        return val if isinstance(val, str) else ""
    return ""


async def _prefetch_body(request: Request) -> None:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        await request.form()          # parsed once and cached on the request for Form() and CSRF
    else:
        await request.body()


async def get_ctx(request: Request, s: Session = Depends(get_session)) -> UserContext:
    if request.method in UNSAFE:
        # 1) the whole (size-limited) body is read first — a slow client must not hold the
        #    SQLite write lock while uploading; 2) an anonymous request is turned away before
        #    the lock is taken, so a flood of unauthenticated POSTs cannot serialise real writes.
        await _prefetch_body(request)
        raw = request.cookies.get(SESSION_COOKIE, "")
        pre = s.get(UserSession, sha256_hex(raw)) if raw and len(raw) <= 128 else None
        if pre is None or pre.revoked_at is not None or utcnow() >= pre.expires_at:
            raise LoginRequired()
        s.rollback()                  # end the read transaction; the next one is a write
        write_intent(s)
    sess = load_session(s, request.cookies.get(SESSION_COOKIE, ""))
    user = s.get(User, sess.user_id) if sess else None
    if sess is None or user is None or not user.active:
        raise LoginRequired()
    if request.method in UNSAFE and not tokens_match(sess.csrf_token, await _submitted_csrf(request)):
        raise HTTPException(status_code=403, detail="err.csrf")
    set_tenant(s, user.org_id)                  # before reading branches/memberships (RLS on Postgres)
    ctx = load_user_context(s, user, sess.active_branch_id)
    ctx.csrf_token, ctx.session_id = sess.csrf_token, sess.id
    set_tenant(s, ctx.org_id)
    request.state.ctx = ctx
    return ctx


async def anon_csrf(request: Request) -> None:
    """CSRF for forms used before login (login, signup): signed double-submit token."""
    if request.method in UNSAFE:
        pre = request.cookies.get(PRE_COOKIE, "")
        if not pre or not tokens_match(pre_csrf_token(pre), await _submitted_csrf(request)):
            raise HTTPException(status_code=403, detail="err.csrf")


def require(perm: str) -> Callable[..., UserContext]:
    """Route guard: the caller must hold ``perm`` in at least one branch of the scope.
    Object-level routes additionally check ``ctx.can(perm, obj.branch_id)``."""
    if perm not in PERMISSIONS:
        raise ValueError(f"unknown permission {perm}")

    def _dep(ctx: UserContext = Depends(get_ctx)) -> UserContext:
        if not ctx.can(perm):
            raise HTTPException(status_code=403, detail="err.forbidden")
        return ctx
    return _dep


def check(ctx: UserContext, perm: str, branch_id: int | None) -> None:
    if not ctx.can(perm, branch_id):
        raise HTTPException(status_code=403, detail="err.forbidden")


def require_superadmin(ctx: UserContext = Depends(get_ctx)) -> UserContext:
    if not ctx.is_superadmin:
        raise HTTPException(status_code=404)          # the console is invisible to tenants
    return ctx


def base_context(request: Request, s: Session | None, ctx: UserContext | None,
                 status_counts: dict | None = None) -> dict[str, Any]:
    lang = get_locale(request)
    data: dict[str, Any] = {
        "request": request, "t": translator(lang), "lang": lang, "locales": LOCALES,
        "ctx": ctx, "path": request.url.path,
        "next_q": quote(request.url.path + (("?" + request.url.query) if request.url.query else "")),
    }
    if ctx is not None and s is not None and ctx.org_id is not None:
        branches = s.exec(select(Branch).where(Branch.org_id == ctx.org_id,
                                               col(Branch.id).in_(ctx.branch_ids or [-1]))
                          .order_by(col(Branch.name))).all()
        from ..services.orders import status_counts as _counts
        counts = status_counts if status_counts is not None else _counts(s, ctx)
        data.update({
            "branches": branches,
            "active_branch": next((b for b in branches if b.id == ctx.active_branch_id), None),
            "open_orders": sum(c for st, c in counts.items() if st != "paid"),
            "status_counts": counts,
        })
    return data


def render(request: Request, s: Session | None, ctx: UserContext | None, template: str,
           status_code: int = 200, **extra: Any) -> Any:
    data = base_context(request, s, ctx, extra.get("counts"))
    data.update(extra)
    resp = templates.TemplateResponse(request, template, data, status_code=status_code)
    if ctx is not None:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"
