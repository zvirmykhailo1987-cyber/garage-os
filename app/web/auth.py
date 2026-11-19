# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Login, logout, self-signup, branch switch and language switch."""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import config
from ..db import get_session, set_tenant
from ..i18n import LOCALE_COOKIE, LOCALES
from ..models import (
    ORG_TRIAL,
    ROLE_NET_ADMIN,
    Branch,
    Membership,
    Organization,
    User,
    UserSession,
    utcnow,
)
from ..security import (
    PRE_COOKIE,
    burn_password_check,
    check_password_policy,
    clear_session_cookie,
    create_session,
    hash_password,
    is_locked,
    login_limiter,
    new_pre_cookie,
    pre_csrf_token,
    register_failed_login,
    revoke_session,
    set_session_cookie,
    signup_limiter,
    verify_password,
)
from ..tenancy import UserContext
from .deps import anon_csrf, client_ip, get_ctx, render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def safe_next(nxt: str | None) -> str:
    """Only same-site relative paths — no open redirect (``//evil``, ``/\\evil``, schemes)."""
    if not nxt or not nxt.startswith("/") or nxt.startswith(("//", "/\\")) or "\n" in nxt or "\r" in nxt:
        return "/"
    return nxt


def _anon_page(request: Request, template: str, status_code: int = 200, **extra):  # type: ignore[no-untyped-def]
    pre = request.cookies.get(PRE_COOKIE) or new_pre_cookie()
    resp = render(request, None, None, template, status_code=status_code,
                  csrf_token=pre_csrf_token(pre), **extra)
    resp.set_cookie(PRE_COOKIE, pre, httponly=True, secure=config.COOKIE_SECURE,
                    samesite="lax", max_age=3600 * 4)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return _anon_page(request, "login.html", next=safe_next(next))


@router.post("/login", dependencies=[Depends(anon_csrf)])
def login(request: Request, email: str = Form(""), password: str = Form(""),
          next: str = Form("/"), s: Session = Depends(get_session)):
    ip = client_ip(request)
    email = email.strip().lower()[:254]
    if not login_limiter.hit(f"ip:{ip}"):
        return _anon_page(request, "login.html", 429, error="err.rate_limited", email=email,
                          next=safe_next(next))
    user = s.exec(select(User).where(User.email == email)).first() if email else None
    if user is None or not user.active:
        burn_password_check(password)
        return _anon_page(request, "login.html", 401, error="err.bad_login", email=email,
                          next=safe_next(next))
    if is_locked(user):                      # same answer as a wrong password: no account oracle
        burn_password_check(password)
        return _anon_page(request, "login.html", 401, error="err.bad_login", email=email,
                          next=safe_next(next))
    ok, rehash = verify_password(user.password_hash, password)
    if not ok:
        register_failed_login(user)
        s.add(user)
        s.commit()
        return _anon_page(request, "login.html", 401, error="err.bad_login", email=email,
                          next=safe_next(next))
    if rehash:                                     # legacy sha256 / old argon2 params → argon2id
        user.password_hash = hash_password(password)
    user.failed_logins, user.locked_until = 0, None
    s.add(user)
    raw = create_session(s, user, ip=ip)
    s.commit()
    target = "/superadmin" if user.is_superadmin else safe_next(next)
    resp = RedirectResponse(target, status_code=303)
    set_session_cookie(resp, raw)
    resp.delete_cookie(PRE_COOKIE)
    return resp


@router.post("/logout")
def logout(s: Session = Depends(get_session), ctx: UserContext = Depends(get_ctx)):
    sess = s.get(UserSession, ctx.session_id)
    if sess is not None:
        revoke_session(s, sess)
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp)
    return resp


@router.post("/branch")
def switch_branch(branch_id: str = Form(""), next: str = Form("/"),
                  s: Session = Depends(get_session), ctx: UserContext = Depends(get_ctx)):
    sess = s.get(UserSession, ctx.session_id)
    bid = int(branch_id) if branch_id.isdigit() else None
    if sess is not None:
        if bid is None and ctx.is_net_admin:
            sess.active_branch_id = None              # "whole network"
        elif bid in ctx.branch_ids:
            sess.active_branch_id = bid
        s.add(sess)
        s.commit()
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/lang/{code}")
def set_lang(code: str, next: str = "/"):
    resp = RedirectResponse(safe_next(next), status_code=303)
    if code in LOCALES:
        resp.set_cookie(LOCALE_COOKIE, code, max_age=3600 * 24 * 365, samesite="lax",
                        secure=config.COOKIE_SECURE)
    return resp


# ── self-signup: organization (trial) + first admin + first branch ────────────
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "org"


@router.get("/signup")
def signup_form(request: Request):
    return _anon_page(request, "signup.html", form={})


@router.post("/signup", dependencies=[Depends(anon_csrf)])
def signup(request: Request, org_name: str = Form(""), admin_name: str = Form(""),
           email: str = Form(""), password: str = Form(""), branch_name: str = Form(""),
           region: str = Form(""), prefix: str = Form(""), s: Session = Depends(get_session)):
    form = {"org_name": org_name, "admin_name": admin_name, "email": email,
            "branch_name": branch_name, "region": region, "prefix": prefix}
    email = email.strip().lower()
    if not signup_limiter.hit(f"ip:{client_ip(request)}"):
        return _anon_page(request, "signup.html", 429, error="err.rate_limited", form=form)
    err = None
    if not (org_name.strip() and admin_name.strip() and branch_name.strip()):
        err = "err.signup_required"
    elif not EMAIL_RE.match(email):
        err = "err.email_invalid"
    else:
        err = check_password_policy(password)
    if err is None and s.exec(select(func.count()).select_from(User).where(User.email == email)).one():
        err = "err.email_taken"
    if err:
        return _anon_page(request, "signup.html", 400, error=err, form=form)

    org = Organization(name=org_name.strip()[:120], slug=_slug(org_name), plan_code="trial",
                       status=ORG_TRIAL, trial_ends_at=utcnow() + timedelta(days=30))
    s.add(org)
    s.flush()
    if org.id is None:
        raise RuntimeError("invariant: org.id is set")
    set_tenant(s, org.id)
    pfx = re.sub(r"[^A-Z0-9]", "", (prefix or branch_name).upper())[:4] or "WO"
    branch = Branch(org_id=org.id, name=branch_name.strip()[:120], region=region.strip()[:80],
                    order_prefix=pfx)
    user = User(org_id=org.id, email=email, name=admin_name.strip()[:120],
                password_hash=hash_password(password))
    s.add(branch)
    s.add(user)
    try:
        s.flush()
    except IntegrityError:                      # two sign-ups with the same e-mail at once
        s.rollback()
        return _anon_page(request, "signup.html", 400, error="err.email_taken", form=form)
    if user.id is None:
        raise RuntimeError("invariant: user.id is set")
    s.add(Membership(org_id=org.id, user_id=user.id, branch_id=None, role=ROLE_NET_ADMIN))
    raw = create_session(s, user, ip=client_ip(request), active_branch_id=branch.id)
    s.commit()
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, raw)
    resp.delete_cookie(PRE_COOKIE)
    return resp
