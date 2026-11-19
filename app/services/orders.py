"""Order domain logic shared by the web UI and the REST API (no HTTP here)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from integrations.plate import normalize_plate

from .. import config
from ..models import (
    Branch,
    BranchCounter,
    Car,
    Client,
    PartLine,
    StatusCount,
    WorkLine,
    WorkOrder,
    order_totals,
)
from ..tenancy import UserContext, stamp


def next_number(s: Session, branch: Branch) -> str:
    """Per-branch order number from a counter row: O(1), and race-safe on Postgres
    (``SELECT … FOR UPDATE`` serialises concurrent creations). Format ``PREFIX-001042``."""
    if branch.id is None:
        raise RuntimeError("invariant: branch.id is set")
    prefix = (branch.order_prefix or "WO").upper()
    if config.IS_POSTGRES:
        ctr = s.exec(select(BranchCounter).where(BranchCounter.branch_id == branch.id)
                     .with_for_update()).first()
    else:
        ctr = s.get(BranchCounter, branch.id)
    if ctr is None:
        mx = s.exec(select(func.max(WorkOrder.number)).where(WorkOrder.branch_id == branch.id)).one()
        start = int(str(mx).rsplit("-", 1)[-1]) + 1 if mx and str(mx).rsplit("-", 1)[-1].isdigit() else 1001
        ctr = BranchCounter(branch_id=branch.id, next_seq=start)
        s.add(ctr)
        s.flush()
    while True:                      # skip numbers that already exist (imports, synced rows)
        n = ctr.next_seq
        ctr.next_seq = n + 1
        number = f"{prefix}-{config.EDGE_TAG}{n:05d}" if config.EDGE_TAG else f"{prefix}-{n:06d}"
        if s.exec(select(WorkOrder.id).where(WorkOrder.branch_id == branch.id,
                                             WorkOrder.number == number)).first() is None:
            break
    s.add(ctr)
    return number


def status_counts(s: Session, ctx: UserContext) -> dict[str, int]:
    """Orders per status in the caller's scope — read from ``StatusCount`` (a few rows)."""
    ids = ctx.scope_ids
    if ctx.org_id is None or not ids:
        return {}
    rows = s.exec(select(StatusCount.status, func.sum(StatusCount.n))
                  .where(StatusCount.org_id == ctx.org_id, col(StatusCount.branch_id).in_(ids))
                  .group_by(col(StatusCount.status))).all()
    return {st: int(n or 0) for st, n in rows}


def lines(s: Session, order: WorkOrder) -> tuple[list[WorkLine], list[PartLine]]:
    works = list(s.exec(select(WorkLine).where(WorkLine.order_id == order.id,
                                               WorkLine.org_id == order.org_id,
                                               WorkLine.deleted == False)  # noqa: E712
                        .order_by(col(WorkLine.id))).all())
    parts = list(s.exec(select(PartLine).where(PartLine.order_id == order.id,
                                               PartLine.org_id == order.org_id,
                                               PartLine.deleted == False)  # noqa: E712
                        .order_by(col(PartLine.id))).all())
    return works, parts


def recompute_totals(s: Session, order: WorkOrder) -> dict:
    s.flush()
    works, parts = lines(s, order)
    t = order_totals(works, parts, order.discount_pct, order.vat_pct)
    order.works_total, order.parts_total = t["works"], t["parts"]
    order.discount_total, order.vat_total, order.total = t["discount"], t["vat"], t["total"]
    s.add(order)
    return t


def car_label(car: Car | None) -> str:
    if not car:
        return "—"
    return f"{car.make} {car.model}".strip() or car.plate or "—"


def bundle(s: Session, order: WorkOrder) -> dict:
    """Everything the order card needs, scoped to the order's own tenant."""
    car = s.get(Car, order.car_id) if order.car_id else None
    client = s.get(Client, order.client_id) if order.client_id else None
    if car is not None and car.org_id != order.org_id:
        car = None
    if client is not None and client.org_id != order.org_id:
        client = None
    works, parts = lines(s, order)
    return {"o": order, "car": car, "client": client, "works": works, "parts": parts,
            "totals": order_totals(works, parts, order.discount_pct, order.vat_pct)}


def rows_for(s: Session, orders: list[WorkOrder],
             branch_by_id: dict[Any, Branch] | None = None) -> list[dict[str, Any]]:
    """Batch-load cars and clients for a page of orders: 2 queries per page, not N+1."""
    car_ids = {o.car_id for o in orders if o.car_id}
    client_ids = {o.client_id for o in orders if o.client_id}
    org_ids = {o.org_id for o in orders}                  # defence in depth: never join across tenants
    cars = ({c.id: c for c in s.exec(select(Car).where(col(Car.id).in_(car_ids),
                                                        col(Car.org_id).in_(org_ids))).all()}
            if car_ids else {})
    clients = ({c.id: c for c in s.exec(select(Client).where(col(Client.id).in_(client_ids),
                                                              col(Client.org_id).in_(org_ids))).all()}
               if client_ids else {})
    out = []
    for o in orders:
        row: dict[str, Any] = {"o": o, "car": cars.get(o.car_id), "client": clients.get(o.client_id)}
        if branch_by_id is not None:
            row["branch"] = branch_by_id.get(o.branch_id) if o.branch_id else None
        out.append(row)
    return out


def find_or_create_client(s: Session, ctx: UserContext, branch_id: int,
                          name: str, phone: str) -> Client | None:
    name, phone = name.strip()[:160], phone.strip()[:40]
    client = None
    if phone:
        client = s.exec(select(Client).where(Client.org_id == ctx.org_id,
                                             Client.branch_id == branch_id,
                                             Client.phone == phone)).first()
    if client is None and (name or phone):
        client = stamp(Client(name=name or phone, phone=phone), ctx, branch_id)
        s.add(client)
        s.flush()
    return client


def find_or_create_car(s: Session, ctx: UserContext, branch_id: int, *, plate: str, make: str,
                       model: str, vin: str, year: str, client_id: int | None) -> Car | None:
    p = normalize_plate(plate)
    car = None
    if p:
        car = s.exec(select(Car).where(Car.org_id == ctx.org_id, Car.branch_id == branch_id,
                                       Car.plate == p)).first()
    if car is None and (p or make.strip()):
        car = stamp(Car(plate=p, make=make.strip()[:60], model=model.strip()[:60],
                        vin=vin.strip().upper()[:20], year=parse_year(year),
                        client_id=client_id), ctx, branch_id)
        s.add(car)
        s.flush()
    return car


def parse_year(v: Any) -> int | None:
    try:
        y = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return y if 1950 <= y <= 2100 else None


def parse_float(v: Any, default: float = 0.0, lo: float = 0.0, hi: float = 1e9) -> float:
    try:
        f = float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default
    if f != f:          # NaN
        return default
    return min(hi, max(lo, f))


def parse_int(v: Any) -> int | None:
    s = str(v or "").strip()
    return int(s) if s.isdigit() and len(s) < 10 else None
