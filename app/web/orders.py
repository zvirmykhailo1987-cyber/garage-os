"""Work orders: list, intake, order card, line items, kanban, customer actions."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlmodel import Session, col, select

from integrations.fiscal import get_fiscal
from integrations.messaging import get_messenger
from integrations.plate import get_plate

from .. import config
from ..audit import TIER2, TIER3, require_confirm
from ..audit import log as audit_log
from ..db import get_session
from ..i18n import get_locale, translator
from ..models import (
    CHANNELS,
    PAY_METHODS,
    ST_PAID,
    WO_STATUSES,
    Branch,
    Car,
    Client,
    Master,
    OutboxMessage,
    Part,
    PartLine,
    Service,
    WorkLine,
    WorkOrder,
    utcnow,
)
from ..paging import paginate, parse_page
from ..services import orders as osvc
from ..tenancy import UserContext, get_scoped, scoped, stamp, touch, write_branch_id
from .deps import check, get_ctx, render, require

router = APIRouter()
KANBAN_PER_COLUMN = 50


def _redirect(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={msg}" if msg else ""), status_code=303)


def _branch_master(s: Session, ctx: UserContext, raw: str, branch_id: int | None) -> Master | None:
    """A mechanic id from a form is trusted only if it belongs to the same branch."""
    mid = osvc.parse_int(raw)
    if mid is None:
        return None
    m = s.get(Master, mid)
    if m is None or m.org_id != ctx.org_id or m.branch_id != branch_id:
        raise HTTPException(status_code=400, detail="err.bad_master")
    return m


def _lines_editable(s: Session, oid: int, ctx: UserContext, money: bool = False) -> WorkOrder:
    """Load an order for a line change under a row lock. A fiscalised order is frozen for
    everybody; a paid order — or any change that sets a price — needs ``orders.pricing``."""
    o = get_scoped(s, WorkOrder, oid, ctx, for_update=True)
    check(ctx, "orders.edit", o.branch_id)
    if o.fiscal_no:
        raise HTTPException(status_code=409, detail="err.order_locked")
    if money or o.paid:
        check(ctx, "orders.pricing", o.branch_id)
    return o


def apply_search(stmt, q: str, org_id: int | None):  # type: ignore[no-untyped-def]
    """Search by order number, plate, make/model, client name or phone.

    Every branch of the OR is index-backed: the order number is matched as a prefix
    range on its index, and free text is matched against the (much smaller) car and
    client tables of this tenant, then joined back by indexed foreign keys. A
    ``LIKE '%q%'`` over work orders would scan the whole table."""
    q = q.strip()[:80]
    if not q:
        return stmt
    like = f"%{q}%"
    num = q.upper()
    car_ids = select(Car.id).where(Car.org_id == org_id, or_(col(Car.plate).ilike(like), col(Car.make).ilike(like),
                                                             col(Car.model).ilike(like)))
    cli_ids = select(Client.id).where(Client.org_id == org_id,
                                      or_(col(Client.name).ilike(like), col(Client.phone).ilike(like)))
    by_number: Any
    if config.IS_POSTGRES:            # LIKE prefix is collation-independent on Postgres
        esc = num.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        by_number = col(WorkOrder.number).like(esc + "%", escape="\\")
    else:                             # SQLite BINARY collation: the range is an exact prefix match
        by_number = (col(WorkOrder.number) >= num) & (col(WorkOrder.number) < num + "\uffff")
    return stmt.where(or_(by_number, col(WorkOrder.car_id).in_(car_ids), col(WorkOrder.client_id).in_(cli_ids)))


# ── list ──────────────────────────────────────────────────────────────────────
@router.get("/orders")
def orders_list(request: Request, status: str = "", q: str = "",
                s: Session = Depends(get_session), ctx: UserContext = Depends(require("orders.view"))):
    stmt = scoped(WorkOrder, ctx)
    if status in WO_STATUSES:
        stmt = stmt.where(WorkOrder.status == status)
    stmt = apply_search(stmt, q, ctx.org_id)
    orders, page, has_next = paginate(s, stmt.order_by(col(WorkOrder.id).desc()),
                                      parse_page(request.query_params.get("page")))
    branch_by_id = {b.id: b for b in s.exec(select(Branch).where(Branch.org_id == ctx.org_id)).all()}
    return render(request, s, ctx, "orders_list.html", rows=osvc.rows_for(s, orders, branch_by_id),
                  f_status=status if status in WO_STATUSES else "", q=q, page=page, has_next=has_next)


# ── intake ────────────────────────────────────────────────────────────────────
def _intake_page(request: Request, s: Session, ctx: UserContext, status_code: int = 200,
                 **extra):  # type: ignore[no-untyped-def]
    bid = write_branch_id(ctx)
    masters = s.exec(scoped(Master, ctx).where(Master.active == True)  # noqa: E712
                     .order_by(col(Master.name))).all() if bid else []
    return render(request, s, ctx, "order_new.html", status_code=status_code, masters=masters,
                  needs_branch=bid is None, form=extra.pop("form", {}), **extra)


@router.get("/orders/new")
def order_new_form(request: Request, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(require("orders.create"))):
    return _intake_page(request, s, ctx)


@router.post("/orders/new")
def order_new(request: Request, plate: str = Form(""), make: str = Form(""), model: str = Form(""),
              year: str = Form(""), vin: str = Form(""), client_name: str = Form(""),
              client_phone: str = Form(""), complaint: str = Form(""), master_id: str = Form(""),
              box: str = Form(""), s: Session = Depends(get_session),
              ctx: UserContext = Depends(require("orders.create"))):
    form = {"plate": plate, "make": make, "model": model, "year": year, "vin": vin,
            "client_name": client_name, "client_phone": client_phone, "complaint": complaint,
            "master_id": master_id, "box": box}
    bid = write_branch_id(ctx)
    if bid is None:
        return _intake_page(request, s, ctx, 400, form=form, error="err.pick_branch")
    check(ctx, "orders.create", bid)
    if not (plate.strip() or make.strip()) or not complaint.strip():
        return _intake_page(request, s, ctx, 400, form=form, error="err.intake_required")
    branch = s.get(Branch, bid)
    if branch is None:
        raise RuntimeError("invariant: branch is set")
    master = _branch_master(s, ctx, master_id, bid)
    client = osvc.find_or_create_client(s, ctx, bid, client_name, client_phone)
    car = osvc.find_or_create_car(s, ctx, bid, plate=plate, make=make, model=model, vin=vin,
                                  year=year, client_id=client.id if client else None)
    o = stamp(WorkOrder(number=osvc.next_number(s, branch), car_id=car.id if car else None,
                        client_id=client.id if client else None,
                        master_id=master.id if master else None, box=box.strip()[:20],
                        complaint=complaint.strip()[:2000]), ctx, bid)
    s.add(o)
    s.flush()
    audit_log(s, actor=ctx.email, action="create_order", tier=TIER2, org_id=ctx.org_id,
              branch_id=bid, entity="WorkOrder", entity_uid=o.uid, detail=o.number)
    s.commit()
    return _redirect(f"/orders/{o.id}", "msg.order_created")


# ── order card ────────────────────────────────────────────────────────────────
@router.get("/orders/{oid}")
def order_detail(oid: int, request: Request, s: Session = Depends(get_session),
                 ctx: UserContext = Depends(require("orders.view"))):
    o = get_scoped(s, WorkOrder, oid, ctx)
    b = osvc.bundle(s, o)
    masters = s.exec(select(Master).where(Master.org_id == ctx.org_id,
                                          Master.branch_id == o.branch_id).order_by(col(Master.name))).all()
    services = s.exec(scoped(Service, ctx, include_shared=True).order_by(col(Service.name))).all()
    stock = s.exec(select(Part).where(Part.org_id == ctx.org_id, Part.branch_id == o.branch_id)
                   .order_by(col(Part.name)).limit(500)).all()
    outbox = s.exec(select(OutboxMessage).where(OutboxMessage.org_id == ctx.org_id,
                                                OutboxMessage.order_id == o.id)
                    .order_by(col(OutboxMessage.id).desc()).limit(20)).all()
    return render(request, s, ctx, "order_detail.html", **b, masters=masters,
                  master_by_id={m.id: m for m in masters}, services=services, stock=stock,
                  outbox=outbox, pay_methods=PAY_METHODS, channels=CHANNELS,
                  conflict=request.query_params.get("conflict") == "1",
                  can_edit=ctx.can("orders.edit", o.branch_id),
                  can_pricing=ctx.can("orders.pricing", o.branch_id),
                  can_notify=ctx.can("orders.notify", o.branch_id),
                  can_fiscal=ctx.can("orders.fiscal", o.branch_id))


@router.post("/orders/{oid}/edit")
def order_edit(oid: int, expected_version: str = Form("0"), status: str | None = Form(None),
               box: str | None = Form(None), complaint: str | None = Form(None),
               master_id: str | None = Form(None), discount_pct: str | None = Form(None),
               pay_method: str | None = Form(None), paid: str | None = Form(None),
               plate: str | None = Form(None), vin: str | None = Form(None),
               make: str | None = Form(None), model: str | None = Form(None),
               year: str | None = Form(None), engine: str | None = Form(None),
               mileage: str | None = Form(None), color: str | None = Form(None),
               s: Session = Depends(get_session), ctx: UserContext = Depends(get_ctx)):
    o = get_scoped(s, WorkOrder, oid, ctx, for_update=True)
    check(ctx, "orders.edit", o.branch_id)
    if osvc.parse_int(expected_version) != o.version:            # optimistic lock
        return RedirectResponse(f"/orders/{oid}?conflict=1", status_code=303)
    pricing_change = (any(v is not None for v in (discount_pct, pay_method, paid))
                      or (status is not None and (status == ST_PAID) != (o.status == ST_PAID)))
    if status is not None and status != o.status and o.fiscal_no:
        raise HTTPException(status_code=409, detail="err.order_locked")
    if pricing_change:
        check(ctx, "orders.pricing", o.branch_id)
        if o.fiscal_no:
            raise HTTPException(status_code=409, detail="err.order_locked")
    if status is not None:
        if status not in WO_STATUSES:
            raise HTTPException(status_code=400, detail="err.bad_status")
        o.status = status
    if box is not None:
        o.box = box.strip()[:20]
    if complaint is not None:
        o.complaint = complaint.strip()[:2000]
    if master_id is not None:
        m = _branch_master(s, ctx, master_id, o.branch_id)
        o.master_id = m.id if m else None
    if discount_pct is not None:
        o.discount_pct = osvc.parse_float(discount_pct, 0.0, 0.0, 100.0)
    if pay_method is not None:
        if pay_method not in PAY_METHODS:
            raise HTTPException(status_code=400, detail="err.bad_pay_method")
        o.pay_method = pay_method
    if paid is not None:
        o.paid = paid in ("true", "on", "1")
    if o.status == ST_PAID:
        o.paid = True
    o.paid_at = (o.paid_at or utcnow()) if o.paid else None
    car = s.get(Car, o.car_id) if o.car_id else None
    if car is not None and car.org_id == o.org_id:
        for f, v, n in (("plate", plate, 20), ("vin", vin, 20), ("make", make, 60),
                        ("model", model, 60), ("engine", engine, 40), ("color", color, 30)):
            if v is not None:
                setattr(car, f, v.strip()[:n].upper() if f in ("plate", "vin") else v.strip()[:n])
        if year is not None:
            car.year = osvc.parse_year(year)
        if mileage is not None:
            car.mileage = osvc.parse_int(mileage)
        touch(car)
        s.add(car)
    touch(o)
    osvc.recompute_totals(s, o)
    audit_log(s, actor=ctx.email, action="edit_order", tier=TIER2, org_id=ctx.org_id,
              branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid, detail=o.status)
    s.commit()
    return _redirect(f"/orders/{oid}", "msg.saved")


@router.post("/orders/{oid}/work")
def order_add_work(oid: int, service: str = Form(""), norm_hours: str = Form("1"),
                   master_id: str = Form(""), s: Session = Depends(get_session),
                   ctx: UserContext = Depends(get_ctx)):
    o = _lines_editable(s, oid, ctx)
    if not service.strip():
        return _redirect(f"/orders/{oid}", "err.work_required")
    m = _branch_master(s, ctx, master_id, o.branch_id)
    s.add(stamp(WorkLine(order_id=oid, service=service.strip()[:160],
                         norm_hours=osvc.parse_float(norm_hours, 1.0, 0.0, 200.0),
                         rate=m.rate if m else 450.0, master_id=m.id if m else None), ctx, o.branch_id))
    touch(o)
    osvc.recompute_totals(s, o)
    s.commit()
    return _redirect(f"/orders/{oid}")


@router.post("/orders/{oid}/work/{lid}/delete")
def order_del_work(oid: int, lid: int, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(get_ctx)):
    o = _lines_editable(s, oid, ctx)
    wl = s.get(WorkLine, lid)
    if wl is None or wl.order_id != o.id or wl.org_id != o.org_id or wl.deleted:    # IDOR guard
        raise HTTPException(status_code=404)
    wl.deleted = True
    touch(wl)
    s.add(wl)
    touch(o)
    osvc.recompute_totals(s, o)
    s.commit()
    return _redirect(f"/orders/{oid}")


@router.post("/orders/{oid}/part")
def order_add_part(oid: int, sku: str = Form(""), name: str = Form(""), qty: str = Form("1"),
                   price: str = Form(""), s: Session = Depends(get_session),
                   ctx: UserContext = Depends(get_ctx)):
    o = _lines_editable(s, oid, ctx)
    stock = None
    if sku.strip():
        stock = s.exec(select(Part).where(Part.org_id == ctx.org_id, Part.branch_id == o.branch_id,
                                          Part.sku == sku.strip())).first()
    # a typed price (anything but the stock price) is a pricing decision, not a mechanic's
    if price.strip() and (stock is None or osvc.parse_float(price, -1.0, 0.0, 1e7) != stock.price):
        check(ctx, "orders.pricing", o.branch_id)
    name = name.strip() or (stock.name if stock else "")
    if not name:
        return _redirect(f"/orders/{oid}", "err.part_required")
    unit = osvc.parse_float(price, stock.price if stock else 0.0, 0.0, 1e7) if price.strip() \
        else (stock.price if stock else 0.0)
    s.add(stamp(PartLine(order_id=oid, sku=sku.strip()[:60], name=name[:160],
                         qty=osvc.parse_float(qty, 1.0, 0.0, 10_000.0), price=unit,
                         part_id=stock.id if stock else None), ctx, o.branch_id))
    touch(o)
    osvc.recompute_totals(s, o)
    s.commit()
    return _redirect(f"/orders/{oid}")


@router.post("/orders/{oid}/part/{lid}/delete")
def order_del_part(oid: int, lid: int, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(get_ctx)):
    o = _lines_editable(s, oid, ctx)
    pl = s.get(PartLine, lid)
    if pl is None or pl.order_id != o.id or pl.org_id != o.org_id or pl.deleted:    # IDOR guard
        raise HTTPException(status_code=404)
    pl.deleted = True
    touch(pl)
    s.add(pl)
    touch(o)
    osvc.recompute_totals(s, o)
    s.commit()
    return _redirect(f"/orders/{oid}")


# ── kanban ────────────────────────────────────────────────────────────────────
@router.get("/kanban")
def kanban(request: Request, s: Session = Depends(get_session),
           ctx: UserContext = Depends(require("orders.view"))):
    collected: list[WorkOrder] = []
    for st in WO_STATUSES:
        collected += list(s.exec(scoped(WorkOrder, ctx).where(WorkOrder.status == st)
                                 .order_by(col(WorkOrder.id).desc()).limit(KANBAN_PER_COLUMN)).all())
    board: dict[str, list] = {st: [] for st in WO_STATUSES}
    for r in osvc.rows_for(s, collected):
        board[r["o"].status].append(r)
    return render(request, s, ctx, "kanban.html", board=board, per_column=KANBAN_PER_COLUMN,
                  counts=osvc.status_counts(s, ctx))


@router.post("/orders/{oid}/status")
async def order_status(oid: int, request: Request, s: Session = Depends(get_session),
                       ctx: UserContext = Depends(get_ctx)):
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)
    st, exp = data.get("status"), data.get("expected_version")
    o = get_scoped(s, WorkOrder, oid, ctx, for_update=True)
    money_move = (st == ST_PAID) != (o.status == ST_PAID)
    if not ctx.can("orders.edit", o.branch_id) or (money_move and not ctx.can("orders.pricing", o.branch_id)):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if o.fiscal_no and st != o.status:
        return JSONResponse({"ok": False, "error": "locked"}, status_code=409)
    if st not in WO_STATUSES:
        return JSONResponse({"ok": False, "error": "bad_status"}, status_code=400)
    if not isinstance(exp, int) or exp != o.version:
        return JSONResponse({"ok": False, "error": "conflict", "version": o.version}, status_code=409)
    was_paid = o.status == ST_PAID
    o.status = st
    if st == ST_PAID:
        o.paid = True
        o.paid_at = o.paid_at or utcnow()
    elif was_paid:                                   # moved out of "paid" (pricing role only)
        o.paid, o.paid_at = False, None
    touch(o)
    audit_log(s, actor=ctx.email, action="change_status", tier=TIER2, org_id=ctx.org_id,
              branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid, detail=st)
    s.add(o)
    s.commit()
    return JSONResponse({"ok": True, "version": o.version})


# ── integrations ──────────────────────────────────────────────────────────────
@router.get("/integrations/plate")
def plate_lookup(plate: str = "", s: Session = Depends(get_session),
                 ctx: UserContext = Depends(require("orders.create"))):
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    res = get_plate().lookup(s, plate, ctx.org_id, ctx.branch_ids)
    return JSONResponse({"found": bool(res), **(res or {})})


@router.post("/orders/{oid}/notify")
def notify_client(oid: int, request: Request, channel: str = Form("sms"), confirm: str = Form(""),
                  s: Session = Depends(get_session), ctx: UserContext = Depends(get_ctx)):
    o = get_scoped(s, WorkOrder, oid, ctx)
    check(ctx, "orders.notify", o.branch_id)
    require_confirm(TIER3, confirm == "yes")
    if channel not in CHANNELS:
        raise HTTPException(status_code=400, detail="err.bad_channel")
    b = osvc.bundle(s, o)
    client = b["client"]
    if client is None or not client.phone:
        return _redirect(f"/orders/{oid}", "err.no_phone")
    tr = translator(get_locale(request))
    text = tr("sms.status", number=o.number, car=osvc.car_label(b["car"]),
              status=tr(f"status.{o.status}"), total=f"{b['totals']['total']:.2f}")
    if ctx.org_id is None:
        raise RuntimeError("invariant: ctx.org_id is set")
    get_messenger().send(s, to=client.phone, text=text, channel=channel, org_id=ctx.org_id,
                         branch_id=o.branch_id, order_id=o.id, client_id=client.id)
    audit_log(s, actor=ctx.email, action="send_message", tier=TIER3, org_id=ctx.org_id,
              branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid, detail=channel)
    s.commit()
    return _redirect(f"/orders/{oid}", "msg.message_logged")


@router.post("/orders/{oid}/fiscal")
def fiscal_receipt(oid: int, confirm: str = Form(""), s: Session = Depends(get_session),
                   ctx: UserContext = Depends(get_ctx)):
    o = get_scoped(s, WorkOrder, oid, ctx, for_update=True)     # no double receipt under a race
    check(ctx, "orders.fiscal", o.branch_id)
    require_confirm(TIER3, confirm == "yes")
    if o.fiscal_no:
        return _redirect(f"/orders/{oid}", "err.already_fiscal")
    if o.total <= 0:
        return _redirect(f"/orders/{oid}", "err.zero_total")
    res = get_fiscal().post_receipt(s, o, o.total)
    o.fiscal_no = res["fiscal_no"]
    touch(o)
    audit_log(s, actor=ctx.email, action="fiscal_receipt", tier=TIER3, org_id=ctx.org_id,
              branch_id=o.branch_id, entity="WorkOrder", entity_uid=o.uid, detail=o.fiscal_no)
    s.add(o)
    s.commit()
    return _redirect(f"/orders/{oid}", "msg.fiscal_done")


@router.get("/orders/{oid}/invoice")
def invoice(oid: int, request: Request, s: Session = Depends(get_session),
            ctx: UserContext = Depends(require("orders.view"))):
    o = get_scoped(s, WorkOrder, oid, ctx)
    b = osvc.bundle(s, o)
    branch = s.get(Branch, o.branch_id) if o.branch_id else None
    return render(request, s, ctx, "invoice.html", **b, branch=branch,
                  car_label=osvc.car_label(b["car"]))
