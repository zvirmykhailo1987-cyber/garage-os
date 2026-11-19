# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Fleet contracts: vehicles, spend, mileage-based maintenance and monthly acts.
All reads are batched (a fixed number of queries regardless of fleet size)."""
from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from ..models import Car, Client, FleetContract, FleetVehicle, MaintenancePlan, MonthlyAct, WorkOrder
from .orders import car_label


def contracts_overview(s: Session, contracts: list[FleetContract]) -> list[dict[str, Any]]:
    """List page: 4 queries for any number of contracts (was 1 + 4·N)."""
    if not contracts:
        return []
    org_id = contracts[0].org_id
    cids = [c.id for c in contracts]
    clients = {c.id: c for c in s.exec(select(Client).where(
        Client.org_id == org_id, col(Client.id).in_({c.client_id for c in contracts}))).all()}
    vehicles = s.exec(select(FleetVehicle).where(FleetVehicle.org_id == org_id,
                                                 col(FleetVehicle.contract_id).in_(cids))).all()
    car_ids = {v.car_id for v in vehicles}
    sums: dict[int, float] = {}
    if car_ids:
        for car_id, total in s.exec(select(WorkOrder.car_id, func.coalesce(func.sum(WorkOrder.total), 0.0))
                                    .where(WorkOrder.org_id == org_id, col(WorkOrder.car_id).in_(car_ids))
                                    .group_by(col(WorkOrder.car_id))).all():
            if car_id is not None:
                sums[car_id] = float(total)
    out = []
    for c in contracts:
        vs = [v for v in vehicles if v.contract_id == c.id]
        out.append({"c": c, "client": clients.get(c.client_id), "count": len(vs),
                    "total": round(sum(sums.get(v.car_id, 0.0) for v in vs), 2)})
    return out


def contract_detail(s: Session, contract: FleetContract, history_per_vehicle: int = 10) -> dict[str, Any]:
    org_id = contract.org_id
    vehicles = s.exec(select(FleetVehicle).where(FleetVehicle.org_id == org_id,
                                                 FleetVehicle.contract_id == contract.id)
                      .order_by(col(FleetVehicle.unit_no))).all()
    car_ids = [v.car_id for v in vehicles]
    cars = {c.id: c for c in s.exec(select(Car).where(Car.org_id == org_id, col(Car.id).in_(car_ids))).all()} \
        if car_ids else {}
    plans = {p.car_id: p for p in s.exec(select(MaintenancePlan).where(
        MaintenancePlan.org_id == org_id, col(MaintenancePlan.car_id).in_(car_ids))).all()} if car_ids else {}
    orders = s.exec(select(WorkOrder).where(WorkOrder.org_id == org_id, col(WorkOrder.car_id).in_(car_ids))
                    .order_by(col(WorkOrder.id).desc())).all() if car_ids else []
    by_car: dict[int, list[WorkOrder]] = {}
    for o in orders:
        if o.car_id is not None:
            by_car.setdefault(o.car_id, []).append(o)
    rows, total = [], 0.0
    for v in vehicles:
        car = cars.get(v.car_id)
        hist = by_car.get(v.car_id, [])
        vtotal = round(sum(o.total for o in hist), 2)
        plan = plans.get(v.car_id)
        due = None
        if plan and car and car.mileage is not None:
            remaining = plan.next_due_km - car.mileage
            due = {"next_due_km": plan.next_due_km, "remaining": remaining,
                   "overdue": remaining <= 0, "soon": 0 < remaining <= 2000}
        rows.append({"v": v, "car": car, "label": car_label(car), "orders": hist[:history_per_vehicle],
                     "total": vtotal, "due": due})
        total += vtotal
    return {"vehicles": rows, "total": round(total, 2), "count": len(vehicles)}


def build_monthly_act(s: Session, contract: FleetContract, period: str) -> MonthlyAct:
    """Monthly act = PAID orders of the fleet's vehicles accepted in that calendar month.

    The acceptance date never changes after intake, so a later edit cannot move an order into
    another month. Generating the same period again replaces the previous act."""
    from datetime import datetime
    start = datetime.strptime(period + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
    car_ids = [v.car_id for v in s.exec(select(FleetVehicle).where(
        FleetVehicle.org_id == contract.org_id, FleetVehicle.contract_id == contract.id)).all()]
    n, total = 0, 0.0
    if car_ids:
        n, total = s.exec(select(func.count(), func.coalesce(func.sum(WorkOrder.total), 0.0))
                          .where(WorkOrder.org_id == contract.org_id, col(WorkOrder.car_id).in_(car_ids),
                                 WorkOrder.paid == True,  # noqa: E712
                                 col(WorkOrder.accepted_at) >= start, col(WorkOrder.accepted_at) < end)).one()
    for old in s.exec(select(MonthlyAct).where(MonthlyAct.org_id == contract.org_id,
                                               MonthlyAct.contract_id == contract.id,
                                               MonthlyAct.period == period)).all():
        s.delete(old)
    act = MonthlyAct(contract_id=contract.id or 0, period=period, orders=int(n), total=round(float(total), 2),
                     org_id=contract.org_id, branch_id=contract.branch_id)
    s.add(act)
    return act


def list_acts(s: Session, contract: FleetContract) -> list[MonthlyAct]:
    return list(s.exec(select(MonthlyAct).where(MonthlyAct.org_id == contract.org_id,
                                                MonthlyAct.contract_id == contract.id)
                       .order_by(col(MonthlyAct.id).desc())).all())
