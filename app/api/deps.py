# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""REST authentication: bearer token → principal bound to ONE tenant."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Depends, Header, HTTPException, Request
from sqlmodel import Session, select

from ..db import get_session, set_tenant, write_intent
from ..models import ApiToken, Organization, Plan, utcnow
from ..security import api_limiter, chat_limiter, sha256_hex
from ..tenancy import org_is_writable


@dataclass
class ApiPrincipal:
    token_id: int
    org_id: int
    branch_id: int | None
    subject_type: str
    subject_id: int | None
    scopes: set[str] = field(default_factory=set)
    writable: bool = True
    ai_enabled: bool = True

    @property
    def actor(self) -> str:
        return f"token:{self.token_id}"


def api_principal(request: Request, authorization: str = Header(default=""),
                  s: Session = Depends(get_session)) -> ApiPrincipal:
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        write_intent(s)
    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw.strip() or len(raw) > 200:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    tok = s.exec(select(ApiToken).where(ApiToken.token_hash == sha256_hex(raw.strip()))).first()
    now = utcnow()
    if tok is None or not tok.active or (tok.expires_at and tok.expires_at < now):
        raise HTTPException(401, "invalid, revoked or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    if tok.id is None:
        raise RuntimeError("invariant: tok.id is set")
    if not api_limiter.hit(f"tok:{tok.id}"):
        raise HTTPException(429, "rate limit exceeded")
    if tok.last_used_at is None or now - tok.last_used_at > timedelta(minutes=5):
        tok.last_used_at = now
        s.add(tok)
        s.commit()
    set_tenant(s, tok.org_id)
    org = s.get(Organization, tok.org_id)
    plan = s.exec(select(Plan).where(Plan.code == org.plan_code)).first() if org else None
    return ApiPrincipal(tok.id, tok.org_id, tok.branch_id, tok.subject_type, tok.subject_id,
                        {x.strip() for x in tok.scopes.split(",") if x.strip()},
                        writable=org_is_writable(org), ai_enabled=bool(plan is None or plan.ai_enabled))


READ_SCOPES = {"orders:read", "fleet:read"}


def require_scope(scope: str):  # type: ignore[no-untyped-def]
    def _dep(p: ApiPrincipal = Depends(api_principal)) -> ApiPrincipal:
        if scope not in p.scopes:
            raise HTTPException(403, f"token lacks scope {scope}")
        if scope not in READ_SCOPES and not p.writable:
            raise HTTPException(403, "subscription is suspended: read-only")
        if scope == "ai:draft" and not p.ai_enabled:
            raise HTTPException(403, "AI features are not included in this plan")
        return p
    return _dep


def chat_limit(p: ApiPrincipal, chat_id: str | None) -> None:
    """Bots act for many Telegram users: one noisy chat must not exhaust the whole token."""
    if p.subject_type == "bot" and not chat_limiter.hit(f"{p.token_id}:{chat_id or '-'}"):
        raise HTTPException(429, "rate limit exceeded for this chat")
