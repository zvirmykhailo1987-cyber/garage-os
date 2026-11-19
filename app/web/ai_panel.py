# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""AI features (human-in-the-loop) and the in-browser Telegram assistant simulator.

1. Note → draft work order (fills the intake form; nothing is saved until a human submits).
2. Natural-language search → visible filters applied as SQL over the tenant's orders.
3. /assistant — the same flows the Telegram bot offers, scoped to the signed-in tenant.
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlmodel import Session, col, select

from ai.runtime import run_skill

from ..audit import TIER2
from ..audit import log as audit_log
from ..db import get_session
from ..models import ST_ESTIMATE, ST_IN_PROGRESS, WO_STATUSES, Branch, Car, WorkOrder, utcnow
from ..paging import paginate, parse_page
from ..services import orders as osvc
from ..tenancy import UserContext, find_order, scoped, touch
from .deps import render, require

router = APIRouter()


@router.post("/ai/work-order-draft")
def ai_work_order_draft(text: str = Form(""), s: Session = Depends(get_session),
                        ctx: UserContext = Depends(require("ai.use"))):
    res = run_skill("work-order-draft", text, session=s, actor=ctx.email, org_id=ctx.org_id,
                    branch_id=ctx.active_branch_id)
    s.commit()
    return JSONResponse(res)


@router.get("/orders/search")
def ai_search(request: Request, q: str = "", s: Session = Depends(get_session),
              ctx: UserContext = Depends(require("ai.use"))):
    res = run_skill("nl-filter", q, session=s, actor=ctx.email, org_id=ctx.org_id,
                    branch_id=ctx.active_branch_id)
    s.commit()
    flt = res["draft"]["filters"]
    stmt = scoped(WorkOrder, ctx)
    for f in flt:
        val = str(f["value"])
        if f["field"] == "status" and val in WO_STATUSES:
            stmt = stmt.where(WorkOrder.status == val)
        elif f["field"] in ("make", "engine"):
            like = f"%{val}%"
            column = Car.engine if f["field"] == "engine" else Car.make
            cars = select(Car.id).where(Car.org_id == ctx.org_id,
                                        or_(col(column).ilike(like), col(Car.model).ilike(like)))
            stmt = stmt.where(col(WorkOrder.car_id).in_(cars))
        elif f["field"] == "updated":
            m = re.fullmatch(r"-(\d{1,3})d", val)
            if m:
                stmt = stmt.where(WorkOrder.updated_at >= utcnow() - timedelta(days=int(m.group(1))))
    orders, page, has_next = paginate(s, stmt.order_by(col(WorkOrder.id).desc()),
                                      parse_page(request.query_params.get("page")))
    branch_by_id = {b.id: b for b in s.exec(select(Branch).where(Branch.org_id == ctx.org_id)).all()}
    return render(request, s, ctx, "orders_list.html", rows=osvc.rows_for(s, orders, branch_by_id),
                  f_status="", q="", ai_q=q, ai_filters=flt, ai_summary=res["draft"]["summary"],
                  ai_fallback=res["fallback"], page=page, has_next=has_next)


# ── Telegram assistant simulator ──────────────────────────────────────────────
@router.get("/assistant")
def assistant(request: Request, s: Session = Depends(get_session),
              ctx: UserContext = Depends(require("orders.view"))):
    return render(request, s, ctx, "assistant.html")


@router.post("/assistant/status")
def assistant_status(request: Request, number: str = Form(""), s: Session = Depends(get_session),
                     ctx: UserContext = Depends(require("orders.view"))):
    o = find_order(s, number[:20], ctx.org_id, ctx.branch_ids)
    reply = {"key": "bot.not_found", "number": number[:20]}
    if o is not None:
        b = osvc.bundle(s, o)
        reply = {"key": "bot.status", "number": o.number, "car": osvc.car_label(b["car"]),
                 "status": o.status, "total": f"{o.total:.2f}"}
    return render(request, s, ctx, "assistant.html", client_reply=reply, last_number=number[:20])


@router.post("/assistant/approve")
def assistant_approve(request: Request, number: str = Form(""), s: Session = Depends(get_session),
                      ctx: UserContext = Depends(require("orders.edit"))):
    o = find_order(s, number[:20], ctx.org_id, ctx.branch_ids)
    if o is None or not ctx.can("orders.edit", o.branch_id):
        reply = {"key": "bot.not_found", "number": number[:20]}
    elif o.status != ST_ESTIMATE:
        reply = {"key": "bot.no_approval", "number": o.number, "status": o.status}
    else:
        o.status = ST_IN_PROGRESS
        touch(o)
        audit_log(s, actor=f"assistant:{ctx.email}", action="approve_estimate", tier=TIER2,
                  org_id=ctx.org_id, branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid)
        s.add(o)
        s.commit()
        reply = {"key": "bot.approved", "number": o.number}
    return render(request, s, ctx, "assistant.html", client_reply=reply, last_number=number[:20])


@router.post("/assistant/voice")
def assistant_voice(request: Request, text: str = Form(""), s: Session = Depends(get_session),
                    ctx: UserContext = Depends(require("ai.use"))):
    res = run_skill("work-order-draft", text, session=s, actor=ctx.email, org_id=ctx.org_id,
                    branch_id=ctx.active_branch_id)
    s.commit()
    return render(request, s, ctx, "assistant.html", master_draft=res, last_voice=text[:2000])
