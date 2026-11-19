"""Dashboard, clients, stock, catalog, team, tokens."""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..audit import TIER2
from ..audit import log as audit_log
from ..db import get_session
from ..models import (
    CLIENT_KINDS,
    ROLE_NET_ADMIN,
    ROLES,
    ApiToken,
    Branch,
    Car,
    Client,
    Master,
    Membership,
    Part,
    Service,
    SyncDevice,
    User,
    WorkOrder,
    utcnow,
)
from ..paging import paginate, parse_page
from ..security import check_password_policy, hash_password, new_secret_token, sha256_hex
from ..services import orders as osvc
from ..tenancy import UserContext, get_scoped, scoped, scoped_where, stamp, write_branch_id
from .auth import EMAIL_RE
from .deps import check, render, require

router = APIRouter()
API_SCOPES = ["orders:read", "estimates:approve", "ai:draft", "fleet:read", "telephony:write",
              "telegram:link"]


def _redirect(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={msg}" if msg else ""), status_code=303)


# ── dashboard ─────────────────────────────────────────────────────────────────
@router.get("/")
def dashboard(request: Request, s: Session = Depends(get_session),
              ctx: UserContext = Depends(require("orders.view"))):
    counts = osvc.status_counts(s, ctx)
    now = utcnow()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    revenue_today = s.exec(scoped_where(
        select(func.coalesce(func.sum(WorkOrder.total), 0.0)).select_from(WorkOrder), WorkOrder, ctx)
        .where(col(WorkOrder.paid_at) >= today0)).one()
    week0 = today0 - timedelta(days=6)
    revenue_week = s.exec(scoped_where(
        select(func.coalesce(func.sum(WorkOrder.total), 0.0)).select_from(WorkOrder), WorkOrder, ctx)
        .where(col(WorkOrder.paid_at) >= week0)).one()
    low_parts = s.exec(scoped(Part, ctx).where(Part.qty <= Part.min_qty)
                       .order_by(col(Part.qty)).limit(6)).all()
    low_count = s.exec(scoped_where(select(func.count()).select_from(Part), Part, ctx)
                       .where(Part.qty <= Part.min_qty)).one()
    recent = list(s.exec(scoped(WorkOrder, ctx).order_by(col(WorkOrder.id).desc()).limit(8)).all())
    kpi = {"in_work": sum(counts.get(st, 0) for st in ("in_progress", "waiting_parts", "estimate")),
           "revenue_today": float(revenue_today or 0), "revenue_week": float(revenue_week or 0),
           "ready": counts.get("done", 0), "low_parts": low_count}
    return render(request, s, ctx, "dashboard.html", rows=osvc.rows_for(s, recent), kpi=kpi,
                  low_parts=low_parts, counts=counts)


# ── clients ───────────────────────────────────────────────────────────────────
@router.get("/clients")
def clients_list(request: Request, q: str = "", s: Session = Depends(get_session),
                 ctx: UserContext = Depends(require("clients.view"))):
    stmt = scoped(Client, ctx)
    q = q.strip()[:80]
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Client.name.ilike(like), Client.phone.ilike(like)))  # type: ignore[attr-defined]
    clients, page, has_next = paginate(s, stmt.order_by(col(Client.name)), parse_page(request.query_params.get("page")))
    ids = [c.id for c in clients]
    cars_by_client: dict = {}
    visits: dict = {}
    if ids:
        for car in s.exec(select(Car).where(Car.org_id == ctx.org_id, col(Car.client_id).in_(ids))).all():
            cars_by_client.setdefault(car.client_id, []).append(car)
        visits = dict(s.exec(select(WorkOrder.client_id, func.count())
                             .where(WorkOrder.org_id == ctx.org_id, col(WorkOrder.client_id).in_(ids),
                                    col(WorkOrder.branch_id).in_(ctx.branch_ids or [-1]))
                             .group_by(col(WorkOrder.client_id))).all())
    rows = [{"c": c, "cars": cars_by_client.get(c.id, []), "visits": visits.get(c.id, 0)} for c in clients]
    return render(request, s, ctx, "clients_list.html", rows=rows, q=q, page=page,
                  has_next=has_next, kinds=CLIENT_KINDS, can_edit=ctx.can("clients.edit"),
                  needs_branch=write_branch_id(ctx) is None)


@router.get("/clients/{cid}")
def client_card(cid: int, request: Request, s: Session = Depends(get_session),
                ctx: UserContext = Depends(require("clients.view"))):
    return _client_page(request, s, ctx, get_scoped(s, Client, cid, ctx))


def _client_page(request: Request, s: Session, ctx: UserContext, c: Client, tg_code: str = ""):  # type: ignore[no-untyped-def]
    cid = c.id
    cars = s.exec(select(Car).where(Car.org_id == ctx.org_id, Car.client_id == cid)).all()
    stmt = select(WorkOrder).where(WorkOrder.org_id == ctx.org_id, WorkOrder.client_id == cid,
                                   col(WorkOrder.branch_id).in_(ctx.branch_ids or [-1])) \
        .order_by(col(WorkOrder.id).desc())
    orders, page, has_next = paginate(s, stmt, parse_page(request.query_params.get("page")))
    return render(request, s, ctx, "client_card.html", c=c, cars=cars,
                  hist=osvc.rows_for(s, orders), page=page, has_next=has_next, tg_code=tg_code,
                  can_edit=ctx.can("clients.edit", c.branch_id))


@router.post("/clients/new")
def client_new(name: str = Form(""), phone: str = Form(""), kind: str = Form("private"),
               discount: str = Form("0"), s: Session = Depends(get_session),
               ctx: UserContext = Depends(require("clients.edit"))):
    bid = write_branch_id(ctx)
    if bid is None:
        return _redirect("/clients", "err.pick_branch")
    check(ctx, "clients.edit", bid)
    if not name.strip():
        return _redirect("/clients", "err.name_required")
    c = stamp(Client(name=name.strip()[:160], phone=phone.strip()[:40],
                     kind=kind if kind in CLIENT_KINDS else "private",
                     discount=osvc.parse_float(discount, 0.0, 0.0, 100.0)), ctx, bid)
    s.add(c)
    s.commit()
    return _redirect(f"/clients/{c.id}", "msg.saved")


@router.post("/clients/{cid}/telegram-code")
def client_tg_code(cid: int, request: Request, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(require("clients.edit"))):
    """One-time code (valid 24 h) the customer sends to the bot: /start CODE."""
    c = get_scoped(s, Client, cid, ctx)
    check(ctx, "clients.edit", c.branch_id)
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    c.tg_code_hash = sha256_hex(code)
    c.tg_code_expires = utcnow() + timedelta(hours=24)
    s.add(c)
    s.commit()
    return _client_page(request, s, ctx, c, tg_code=code)


# ── stock ─────────────────────────────────────────────────────────────────────
@router.get("/parts")
def parts_list(request: Request, q: str = "", low: str = "", s: Session = Depends(get_session),
               ctx: UserContext = Depends(require("parts.view"))):
    stmt = scoped(Part, ctx)
    q = q.strip()[:80]
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Part.sku.ilike(like), Part.name.ilike(like), Part.brand.ilike(like)))  # type: ignore[attr-defined]
    if low == "1":
        stmt = stmt.where(Part.qty <= Part.min_qty)
    parts, page, has_next = paginate(s, stmt.order_by(col(Part.name)), parse_page(request.query_params.get("page")))
    return render(request, s, ctx, "parts_list.html", parts=parts, q=q, low=low == "1", page=page,
                  has_next=has_next, can_edit=ctx.can("parts.edit"),
                  needs_branch=write_branch_id(ctx) is None)


@router.post("/parts/new")
def part_new(sku: str = Form(""), name: str = Form(""), brand: str = Form(""),
             supplier: str = Form(""), qty: str = Form("0"), min_qty: str = Form("0"),
             cost: str = Form("0"), price: str = Form("0"), s: Session = Depends(get_session),
             ctx: UserContext = Depends(require("parts.edit"))):
    bid = write_branch_id(ctx)
    if bid is None:
        return _redirect("/parts", "err.pick_branch")
    check(ctx, "parts.edit", bid)
    if not (sku.strip() and name.strip()):
        return _redirect("/parts", "err.sku_name_required")
    f = osvc.parse_float
    s.add(stamp(Part(sku=sku.strip()[:60], name=name.strip()[:160], brand=brand.strip()[:60],
                     supplier=supplier.strip()[:80], qty=f(qty), min_qty=f(min_qty),
                     cost=f(cost), price=f(price)), ctx, bid))
    s.commit()
    return _redirect("/parts", "msg.saved")


# ── settings: catalog, team, branches, tokens ─────────────────────────────────
@router.get("/settings")
def settings(request: Request, s: Session = Depends(get_session),
             ctx: UserContext = Depends(require("orders.view"))):
    return _settings_page(request, s, ctx)


def _settings_page(request: Request, s: Session, ctx: UserContext, new_secret: str = ""):  # nosec B107  # type: ignore[no-untyped-def]
    """Rendered directly (not via redirect) when a secret was just created, so the secret
    is shown exactly once and never travels in a URL."""
    data: dict = {"masters": s.exec(scoped(Master, ctx).order_by(col(Master.name))).all(),
                  "services": s.exec(scoped(Service, ctx, include_shared=True).order_by(col(Service.name))).all(),
                  "can_catalog": ctx.can("catalog.edit"), "can_team": ctx.can("team.manage"),
                  "can_tokens": ctx.can("tokens.manage"), "roles": ROLES, "scopes": API_SCOPES,
                  "needs_branch": write_branch_id(ctx) is None}
    if ctx.is_net_admin:
        users = s.exec(select(User).where(User.org_id == ctx.org_id).order_by(col(User.name))).all()
        mems = s.exec(select(Membership).where(Membership.org_id == ctx.org_id)).all()
        data["team"] = [{"u": u, "m": [m for m in mems if m.user_id == u.id]} for u in users]
        data["all_branches"] = s.exec(select(Branch).where(Branch.org_id == ctx.org_id)
                                      .order_by(col(Branch.name))).all()
        data["tokens"] = s.exec(select(ApiToken).where(ApiToken.org_id == ctx.org_id)
                                .order_by(col(ApiToken.id).desc())).all()
        data["devices"] = s.exec(select(SyncDevice).where(SyncDevice.org_id == ctx.org_id)
                                 .order_by(col(SyncDevice.id).desc())).all()
    data["new_secret"] = new_secret
    return render(request, s, ctx, "settings.html", **data)


@router.post("/settings/master")
def add_master(name: str = Form(""), rate: str = Form("450"), s: Session = Depends(get_session),
               ctx: UserContext = Depends(require("catalog.edit"))):
    bid = write_branch_id(ctx)
    if bid is None:
        return _redirect("/settings", "err.pick_branch")
    check(ctx, "catalog.edit", bid)
    if not name.strip():
        return _redirect("/settings", "err.name_required")
    s.add(stamp(Master(name=name.strip()[:120], rate=osvc.parse_float(rate, 450.0, 0.0, 1e5)), ctx, bid))
    s.commit()
    return _redirect("/settings", "msg.saved")


@router.post("/settings/service")
def add_service(name: str = Form(""), norm_hours: str = Form("1"), shared: str = Form(""),
                s: Session = Depends(get_session), ctx: UserContext = Depends(require("catalog.edit"))):
    if not name.strip():
        return _redirect("/settings", "err.name_required")
    if shared == "1":
        if not ctx.is_net_admin:
            raise HTTPException(status_code=403, detail="err.forbidden")
        bid = None
    else:
        bid = write_branch_id(ctx)
        if bid is None:
            return _redirect("/settings", "err.pick_branch")
        check(ctx, "catalog.edit", bid)
    s.add(stamp(Service(name=name.strip()[:160], norm_hours=osvc.parse_float(norm_hours, 1.0, 0.0, 200.0)),
                ctx, bid))
    s.commit()
    return _redirect("/settings", "msg.saved")


@router.post("/settings/users")
def add_user(name: str = Form(""), email: str = Form(""), password: str = Form(""),
             role: str = Form(""), branch_id: str = Form(""), s: Session = Depends(get_session),
             ctx: UserContext = Depends(require("team.manage"))):
    email = email.strip().lower()
    if not name.strip() or not EMAIL_RE.match(email) or role not in ROLES:
        return _redirect("/settings", "err.user_fields")
    if (err := check_password_policy(password)) is not None:
        return _redirect("/settings", err)
    bid = osvc.parse_int(branch_id)
    if role != ROLE_NET_ADMIN and bid not in ctx.branch_ids:
        return _redirect("/settings", "err.user_fields")
    n_users = s.exec(select(func.count()).select_from(User).where(User.org_id == ctx.org_id)).one()
    if ctx.plan is not None and n_users >= ctx.plan.max_users:
        return _redirect("/settings", "err.plan_users")
    if s.exec(select(func.count()).select_from(User).where(User.email == email)).one():
        return _redirect("/settings", "err.email_taken")
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    u = User(org_id=ctx.org_id, email=email, name=name.strip()[:120], password_hash=hash_password(password))
    s.add(u)
    try:
        s.flush()
    except IntegrityError:                      # concurrent add with the same e-mail
        s.rollback()
        return _redirect("/settings", "err.email_taken")
    if u.id is None:
        raise RuntimeError("invariant: u.id is set")
    s.add(Membership(org_id=ctx.org_id, user_id=u.id, role=role,
                     branch_id=None if role == ROLE_NET_ADMIN else bid))
    audit_log(s, actor=ctx.email, action="add_user", tier=TIER2, org_id=ctx.org_id,
              entity="User", entity_uid=u.uid, detail=f"{email} {role}")
    s.commit()
    return _redirect("/settings", "msg.saved")


@router.post("/settings/users/{uid}/toggle")
def toggle_user(uid: int, s: Session = Depends(get_session),
                ctx: UserContext = Depends(require("team.manage"))):
    u = s.get(User, uid)
    if u is None or u.org_id != ctx.org_id:
        raise HTTPException(status_code=404)
    if u.id == ctx.user_id:
        return _redirect("/settings", "err.self_disable")
    u.active = not u.active                        # deactivation also kills live sessions (get_ctx)
    s.add(u)
    audit_log(s, actor=ctx.email, action="toggle_user", tier=TIER2, org_id=ctx.org_id,
              entity="User", entity_uid=u.uid, detail=str(u.active))
    s.commit()
    return _redirect("/settings", "msg.saved")


@router.post("/settings/branches")
def add_branch(name: str = Form(""), region: str = Form(""), prefix: str = Form(""),
               s: Session = Depends(get_session), ctx: UserContext = Depends(require("team.manage"))):
    pfx = "".join(ch for ch in prefix.upper() if ch.isalnum())[:4]
    if not name.strip() or not pfx:
        return _redirect("/settings", "err.branch_fields")
    n = s.exec(select(func.count()).select_from(Branch).where(Branch.org_id == ctx.org_id)).one()
    if ctx.plan is not None and n >= ctx.plan.max_branches:
        return _redirect("/settings", "err.plan_branches")
    if s.exec(select(Branch).where(Branch.org_id == ctx.org_id, Branch.order_prefix == pfx)).first():
        return _redirect("/settings", "err.prefix_taken")     # order numbers must stay unique per tenant
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    s.add(Branch(org_id=ctx.org_id, name=name.strip()[:120], region=region.strip()[:80], order_prefix=pfx))
    s.commit()
    return _redirect("/settings", "msg.saved")


@router.post("/settings/tokens")
def create_token(request: Request, label: str = Form(""), kind: str = Form("bot"), scopes: list[str] = Form([]),
                 branch_id: str = Form(""), s: Session = Depends(get_session),
                 ctx: UserContext = Depends(require("tokens.manage"))):
    if not ctx.writable:
        raise HTTPException(status_code=403, detail="err.forbidden")
    chosen = [sc for sc in scopes if sc in API_SCOPES]
    if not label.strip() or not chosen or kind not in ("bot", "integration"):
        return _redirect("/settings", "err.token_fields")
    bid = osvc.parse_int(branch_id)
    if bid is not None and bid not in ctx.branch_ids:
        return _redirect("/settings", "err.token_fields")
    raw = new_secret_token("sto")
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    tok = ApiToken(org_id=ctx.org_id, branch_id=bid, token_hash=sha256_hex(raw), prefix=raw[:10],
                   label=label.strip()[:80], subject_type=kind, scopes=",".join(chosen),
                   created_by=ctx.user_id, expires_at=utcnow() + timedelta(days=365))
    s.add(tok)
    audit_log(s, actor=ctx.email, action="create_token", tier=TIER2, org_id=ctx.org_id,
              entity="ApiToken", entity_uid=tok.uid, detail=tok.scopes)
    s.commit()
    return _settings_page(request, s, ctx, new_secret=raw)


@router.post("/settings/tokens/{tid}/revoke")
def revoke_token(tid: int, s: Session = Depends(get_session),
                 ctx: UserContext = Depends(require("tokens.manage"))):
    tok = s.get(ApiToken, tid)
    if tok is None or tok.org_id != ctx.org_id:
        raise HTTPException(status_code=404)
    tok.active = False
    s.add(tok)
    audit_log(s, actor=ctx.email, action="revoke_token", tier=TIER2, org_id=ctx.org_id,
              entity="ApiToken", entity_uid=tok.uid)
    s.commit()
    return _redirect("/settings", "msg.revoked")


@router.post("/settings/devices")
def create_device(request: Request, label: str = Form(""), s: Session = Depends(get_session),
                  ctx: UserContext = Depends(require("tokens.manage"))):
    if not ctx.writable:
        raise HTTPException(status_code=403, detail="err.forbidden")
    if not label.strip():
        return _redirect("/settings", "err.name_required")
    raw = new_secret_token("dev")
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    s.add(SyncDevice(org_id=ctx.org_id, label=label.strip()[:80], token_hash=sha256_hex(raw), prefix=raw[:10]))
    s.commit()
    return _settings_page(request, s, ctx, new_secret=raw)


@router.post("/settings/devices/{did}/revoke")
def revoke_device(did: int, s: Session = Depends(get_session),
                  ctx: UserContext = Depends(require("tokens.manage"))):
    dev = s.get(SyncDevice, did)
    if dev is None or dev.org_id != ctx.org_id:
        raise HTTPException(status_code=404)
    dev.active = False
    s.add(dev)
    s.commit()
    return _redirect("/settings", "msg.revoked")
