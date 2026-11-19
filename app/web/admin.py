# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Data migration wizard, subscription page and the platform-operator console."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, text
from sqlmodel import Session, col, select

from .. import config
from ..audit import TIER2, TIER3, require_confirm
from ..audit import log as audit_log
from ..db import get_session, set_tenant
from ..models import ORG_STATUSES, Branch, Organization, Plan, StatusCount, User, WorkOrder
from ..services import migration as msvc
from ..tenancy import UserContext, scoped_where, touch, write_branch_id
from .deps import render, require, require_superadmin

router = APIRouter()


# ── migration ─────────────────────────────────────────────────────────────────
@router.get("/migration")
def migration_home(request: Request, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(require("migration.run"))):
    return render(request, s, ctx, "migration.html", csv_text=msvc.load_sample(),
                  needs_branch=write_branch_id(ctx) is None)


@router.post("/migration/preview")
def migration_preview(request: Request, csv_text: str = Form(""), s: Session = Depends(get_session),
                      ctx: UserContext = Depends(require("migration.run"))):
    if (err := msvc.check_size(csv_text)) is not None:
        return render(request, s, ctx, "migration.html", status_code=413, csv_text="", error=err)
    headers, rows = msvc.parse_csv(csv_text)
    return render(request, s, ctx, "migration.html", csv_text=csv_text, headers=headers,
                  rows=rows[:8], row_count=len(rows), mapping=msvc.propose_mapping(headers),
                  needs_branch=write_branch_id(ctx) is None)


@router.post("/migration/apply")
def migration_apply(request: Request, csv_text: str = Form(""), confirm: str = Form(""),
                    s: Session = Depends(get_session), ctx: UserContext = Depends(require("migration.run"))):
    require_confirm(TIER3, confirm == "yes")
    bid = write_branch_id(ctx)
    if bid is None:
        return render(request, s, ctx, "migration.html", status_code=400, csv_text=csv_text,
                      error="err.pick_branch", needs_branch=True)
    if (err := msvc.check_size(csv_text)) is not None:
        return render(request, s, ctx, "migration.html", status_code=413, csv_text="", error=err)
    headers, rows = msvc.parse_csv(csv_text)
    mapping = msvc.propose_mapping(headers)
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    report = msvc.apply_mapping(s, headers, rows, mapping, ctx.org_id, bid)
    audit_log(s, actor=ctx.email, action="migrate_clients", tier=TIER3, org_id=ctx.org_id,
              branch_id=bid, entity="Client", detail=f"imported={report['imported']}")
    s.commit()
    return render(request, s, ctx, "migration.html", csv_text=csv_text, headers=headers,
                  mapping=mapping, report=report)


# ── subscription ──────────────────────────────────────────────────────────────
@router.get("/billing")
def billing(request: Request, s: Session = Depends(get_session),
            ctx: UserContext = Depends(require("billing.view"))):
    usage = {
        "branches": s.exec(select(func.count()).select_from(Branch).where(Branch.org_id == ctx.org_id)).one(),
        "users": s.exec(select(func.count()).select_from(User).where(User.org_id == ctx.org_id)).one(),
        "orders": s.exec(scoped_where(select(func.count()).select_from(WorkOrder), WorkOrder, ctx)).one(),
    }
    plans = s.exec(select(Plan).order_by(col(Plan.price_month))).all()
    return render(request, s, ctx, "billing.html", usage=usage, plans=plans)


# ── platform operator console ─────────────────────────────────────────────────
def _org_counts(s: Session) -> tuple[dict[int, int], dict[int, int]]:
    """(branches, orders) per org in ONE query. On Postgres the operator cannot read tenant
    rows (RLS), so a narrow SECURITY DEFINER aggregate returns only the numbers."""
    if config.IS_POSTGRES:
        rows = s.execute(text("SELECT * FROM platform_org_counts()")).all()
        return {int(r[0]): int(r[1]) for r in rows}, {int(r[0]): int(r[2]) for r in rows}
    branches = {int(o): int(n) for o, n in s.exec(select(Branch.org_id, func.count())
                                                  .group_by(col(Branch.org_id))).all()}
    orders = {int(o): int(n or 0) for o, n in s.exec(select(StatusCount.org_id, func.sum(StatusCount.n))
                                                     .group_by(col(StatusCount.org_id))).all()}
    return branches, orders


@router.get("/superadmin")
def superadmin(request: Request, s: Session = Depends(get_session),
               ctx: UserContext = Depends(require_superadmin)):
    plans = s.exec(select(Plan).order_by(col(Plan.price_month))).all()
    orgs = s.exec(select(Organization).order_by(col(Organization.id))).all()
    users = dict(s.exec(select(User.org_id, func.count()).where(col(User.org_id).is_not(None))
                        .group_by(col(User.org_id))).all())
    branches, orders = _org_counts(s)
    rows = [{"org": o, "plan": next((p for p in plans if p.code == o.plan_code), None),
             "branches": branches.get(o.id or 0, 0), "users": users.get(o.id or 0, 0),
             "orders": orders.get(o.id or 0, 0)} for o in orgs]
    return render(request, s, ctx, "superadmin.html", rows=rows, plans=plans, statuses=ORG_STATUSES)


@router.post("/superadmin/{oid}")
def superadmin_update(oid: int, plan_code: str = Form(""), status: str = Form(""),
                      s: Session = Depends(get_session), ctx: UserContext = Depends(require_superadmin)):
    org = s.get(Organization, oid)
    if org is None:
        raise HTTPException(status_code=404)
    if status not in ORG_STATUSES or not s.exec(select(Plan).where(Plan.code == plan_code)).first():
        raise HTTPException(status_code=400, detail="err.bad_request")
    org.plan_code, org.status = plan_code, status
    touch(org)
    s.add(org)
    set_tenant(s, oid)                     # the audit row belongs to the affected tenant
    audit_log(s, actor=ctx.email, action="operator_update_org", tier=TIER2, org_id=oid,
              entity="Organization", entity_uid=org.uid, detail=f"{plan_code}/{status}")
    s.commit()
    return RedirectResponse("/superadmin?msg=msg.saved", status_code=303)
