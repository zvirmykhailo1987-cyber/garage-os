"""Hub endpoint. Auth: one token per edge device (``Authorization: Bearer dev_…``)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlmodel import Session, select

from ..db import get_session, set_tenant, write_intent
from ..models import SyncDevice, utcnow
from ..security import api_limiter, sha256_hex
from ..tenancy import org_writable
from .engine import SyncError, _dt, hub_apply, pull_delta

router = APIRouter(tags=["sync"])
MAX_BODY = 5_000_000
PULL_OVERLAP_S = 300          # > the longest push transaction we accept


def sync_device(authorization: str = Header(default=""), s: Session = Depends(get_session)) -> SyncDevice:
    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw.startswith("dev_") or len(raw) > 200:
        raise HTTPException(401, "missing device token")
    # The lookup is by sha256 of a 256-bit random token: an attacker cannot steer it
    # byte-by-byte, so the comparison leaks no useful timing (no plain `!=` on secrets).
    dev = s.exec(select(SyncDevice).where(SyncDevice.token_hash == sha256_hex(raw))).first()
    if dev is None or not dev.active:
        raise HTTPException(401, "invalid or revoked device token")
    if not api_limiter.hit(f"dev:{dev.id}"):
        raise HTTPException(429, "rate limit exceeded")
    set_tenant(s, dev.org_id)
    s.commit()                    # don't hold a transaction while the (possibly slow) body arrives
    return dev


@router.post("/sync")
async def sync_endpoint(request: Request, s: Session = Depends(get_session),
                        dev: SyncDevice = Depends(sync_device)) -> JSONResponse:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "payload too large")
    try:
        data = await request.json()
    except ValueError as e:
        raise HTTPException(400, "bad sync payload: not JSON") from e
    write_intent(s)
    writable = org_writable(s, dev.org_id)
    try:
        if not isinstance(data, dict):
            raise SyncError("body must be an object")
        since = data.get("since")
        if since is not None and (not isinstance(since, str) or _dt(since) is None):
            raise SyncError("since must be an ISO timestamp")   # validated BEFORE anything is applied
        changes = data.get("changes") or {}
        if changes and not writable:
            raise HTTPException(403, "subscription is suspended: read-only")
        protocol = data.get("protocol", 1)
        result = hub_apply(s, dev.org_id, changes, dev.label,
                           protocol=protocol if isinstance(protocol, int) and not isinstance(protocol, bool) else 1)
    except (SyncError, ValueError) as e:
        s.rollback()
        raise HTTPException(400, f"bad sync payload: {e}") from e
    except (IntegrityError, StatementError, DBAPIError) as e:   # e.g. a uid RLS hides from us
        s.rollback()
        raise HTTPException(400, "bad sync payload: rejected by the database") from e
    # overlap window: rows stamped just before `now` but committed a moment later are
    # re-sent next time; edge_apply skips versions it already has, so this is idempotent
    now = utcnow()
    cursor = now - timedelta(seconds=PULL_OVERLAP_S)
    dev.last_seen_at = now
    s.add(dev)
    s.commit()
    if "conflicts" in result:
        return JSONResponse({"now": cursor.isoformat(), **result}, status_code=409)
    return JSONResponse({"now": cursor.isoformat(), "accepted": result["accepted"],
                         "renumbered": result.get("renumbered", {}),
                         "changes": pull_delta(s, dev.org_id, since)})
