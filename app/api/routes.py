# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""REST API /api/v1 — Telegram bot, fleet portal, integrations.

Tenant rules: every lookup is inside the token's organization (and its branch, if the
token is branch-bound). Another tenant's object answers 404 — identical to "not found".
Bot tokens act for a Telegram chat: they only see orders of the client that chat was
linked to with a one-time code.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ai.runtime import run_skill
from integrations.telephony import get_telephony

from ..audit import TIER2
from ..audit import log as audit_log
from ..db import get_session
from ..models import ST_ESTIMATE, ST_IN_PROGRESS, Client, FleetContract, TelegramLink, WorkOrder, utcnow
from ..security import sha256_hex
from ..services import fleet as fsvc
from ..services import orders as osvc
from ..tenancy import find_order, touch
from .deps import ApiPrincipal, chat_limit, require_scope

router = APIRouter(prefix="/api/v1", tags=["api"])


class DraftIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    chat_id: str | None = Field(default=None, max_length=32, pattern=r"^-?\d+$")


class CallIn(BaseModel):
    phone: str = Field(min_length=3, max_length=40)


class LinkIn(BaseModel):
    code: str = Field(min_length=8, max_length=8)
    chat_id: str = Field(min_length=1, max_length=32, pattern=r"^-?\d+$")


@router.get("/health")
def health() -> dict:
    return {"ok": True}


def _order_for(s: Session, number: str, p: ApiPrincipal, chat_id: str | None) -> WorkOrder:
    chat_limit(p, chat_id)
    o = find_order(s, number[:20], p.org_id, [p.branch_id] if p.branch_id else None)
    if o is None:
        raise HTTPException(404, "order not found")
    if p.subject_type == "bot":
        link = s.exec(select(TelegramLink).where(TelegramLink.org_id == p.org_id,
                                                 TelegramLink.chat_id == (chat_id or ""))).first()
        if link is None or link.client_id != o.client_id:
            raise HTTPException(404, "order not found")
    return o


@router.get("/orders/{number}/status")
def order_status(number: str, chat_id: str | None = Query(default=None, max_length=32),
                 s: Session = Depends(get_session),
                 p: ApiPrincipal = Depends(require_scope("orders:read"))) -> dict:
    o = _order_for(s, number, p, chat_id)
    b = osvc.bundle(s, o)
    return {"number": o.number, "status": o.status, "vehicle": osvc.car_label(b["car"]),
            "plate": b["car"].plate if b["car"] else None, "total": o.total, "paid": o.paid,
            "updated_at": o.updated_at.isoformat()}


@router.post("/estimates/{number}/approve")
def approve_estimate(number: str, chat_id: str | None = Query(default=None, max_length=32),
                     s: Session = Depends(get_session),
                     p: ApiPrincipal = Depends(require_scope("estimates:approve"))) -> dict:
    o = _order_for(s, number, p, chat_id)
    if s.get_bind().dialect.name == "postgresql":
        s.refresh(o, with_for_update=True)           # no lost update against a concurrent web edit
    if o.status != ST_ESTIMATE:
        raise HTTPException(409, f"order is '{o.status}', not 'estimate'")
    o.status = ST_IN_PROGRESS
    touch(o)
    audit_log(s, actor=p.actor, action="approve_estimate", tier=TIER2, org_id=p.org_id,
              branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid)
    s.add(o)
    s.commit()
    return {"number": o.number, "status": o.status}


@router.post("/ai/work-order-draft")
def ai_draft(body: DraftIn, s: Session = Depends(get_session),
             p: ApiPrincipal = Depends(require_scope("ai:draft"))) -> dict:
    chat_limit(p, body.chat_id)
    res = run_skill("work-order-draft", body.text, session=s, actor=p.actor, org_id=p.org_id,
                    branch_id=p.branch_id)
    s.commit()
    return res


@router.get("/fleet/{contract_id}/vehicles")
def fleet_vehicles(contract_id: int, s: Session = Depends(get_session),
                   p: ApiPrincipal = Depends(require_scope("fleet:read"))) -> dict:
    c = s.get(FleetContract, contract_id)
    if c is None or c.org_id != p.org_id or (p.branch_id and c.branch_id != p.branch_id) \
            or (p.subject_type == "fleet" and p.subject_id != contract_id):
        raise HTTPException(404, "contract not found")
    ov = fsvc.contract_detail(s, c, history_per_vehicle=0)
    return {"contract": c.name, "total": ov["total"], "vehicles": [
        {"unit_no": r["v"].unit_no, "vehicle": r["label"],
         "plate": r["car"].plate if r["car"] else None,
         "mileage": r["car"].mileage if r["car"] else None, "total": r["total"],
         "maintenance_due_km": r["due"]["next_due_km"] if r["due"] else None}
        for r in ov["vehicles"]]}


@router.post("/webhooks/telephony")
def telephony_webhook(body: CallIn, s: Session = Depends(get_session),
                      p: ApiPrincipal = Depends(require_scope("telephony:write"))) -> dict:
    return get_telephony().on_incoming(s, body.phone, p.org_id, p.branch_id)


@router.post("/telegram/link")
def telegram_link(body: LinkIn, s: Session = Depends(get_session),
                  p: ApiPrincipal = Depends(require_scope("telegram:link"))) -> dict:
    chat_limit(p, body.chat_id)
    """Bind a Telegram chat to a client using the one-time code shown in the CRM."""
    stmt = select(Client).where(Client.org_id == p.org_id, Client.tg_code_hash == sha256_hex(body.code.upper()))
    if p.branch_id:
        stmt = stmt.where(Client.branch_id == p.branch_id)
    client = s.exec(stmt).first()
    if client is None or not client.tg_code_expires or client.tg_code_expires < utcnow():
        raise HTTPException(404, "code not found or expired")
    existing = s.exec(select(TelegramLink).where(TelegramLink.org_id == p.org_id,
                                                 TelegramLink.chat_id == body.chat_id)).first()
    if existing is not None:
        existing.client_id = client.id or 0
        touch(existing)
        s.add(existing)
    else:
        s.add(TelegramLink(org_id=p.org_id, branch_id=client.branch_id, client_id=client.id or 0,
                           chat_id=body.chat_id))
    client.tg_code_hash, client.tg_code_expires = "", None      # one-time
    s.add(client)
    s.commit()
    return {"linked": True, "client": client.name}
