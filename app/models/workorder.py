# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Work orders, their labour/part lines and the deterministic money maths.

``order_totals`` is plain code on purpose: money is never computed by an LLM.
VAT is *included* in the price (Ukrainian retail convention) — it is extracted, not added.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from .base import BaseRow, TenantMixin, utcnow

VAT_PCT_DEFAULT = 20.0

ST_NEW, ST_ESTIMATE, ST_IN_PROGRESS = "new", "estimate", "in_progress"
ST_WAITING_PARTS, ST_DONE, ST_PAID = "waiting_parts", "done", "paid"
WO_STATUSES = [ST_NEW, ST_ESTIMATE, ST_IN_PROGRESS, ST_WAITING_PARTS, ST_DONE, ST_PAID]
WO_OPEN_STATUSES = [s for s in WO_STATUSES if s != ST_PAID]
# moving into these statuses is a natural moment to message the customer
WO_NOTIFY_STATUSES = [ST_ESTIMATE, ST_WAITING_PARTS, ST_DONE]

PAY_METHODS = ["none", "cash", "card", "transfer"]


class WorkOrder(BaseRow, TenantMixin, table=True):
    # Every tenant query filters ``org_id = ? AND branch_id IN (...)`` — indexes lead with
    # both keys and cover the columns the list/board/KPI queries need (no table lookups).
    __table_args__ = (
        Index("ix_wo_scope_status", "org_id", "branch_id", "status", "id"),
        Index("ix_wo_scope_id", "org_id", "branch_id", "id"),
        Index("ix_wo_scope_paid", "org_id", "branch_id", "paid_at", "total"),
        Index("ix_wo_client_id", "client_id", "id"),
        UniqueConstraint("branch_id", "number", name="uq_wo_branch_number"),
    )
    number: str = Field(default="", index=True, max_length=20)
    status: str = Field(default=ST_NEW, max_length=20)
    car_id: int | None = Field(default=None, index=True)
    client_id: int | None = Field(default=None, index=True)
    master_id: int | None = Field(default=None, index=True)
    box: str = Field(default="", max_length=20)
    complaint: str = Field(default="", max_length=2000)
    accepted_at: datetime | None = Field(default_factory=utcnow)
    promised_at: datetime | None = None
    discount_pct: float = 0.0
    vat_pct: float = VAT_PCT_DEFAULT
    pay_method: str = Field(default="none", max_length=20)
    paid: bool = False
    paid_at: datetime | None = None          # revenue is reported on this, not on updated_at
    source: str = Field(default="desk", max_length=20)          # desk | phone | online | telegram
    fiscal_no: str = Field(default="", max_length=80)
    # denormalised totals — written on every change, read by lists in O(1)
    works_total: float = 0.0
    parts_total: float = 0.0
    discount_total: float = 0.0
    vat_total: float = 0.0
    total: float = 0.0


class WorkLine(BaseRow, TenantMixin, table=True):
    order_id: int = Field(index=True)
    service: str = Field(default="", max_length=160)
    master_id: int | None = None
    norm_hours: float = 0.0
    rate: float = 0.0
    deleted: bool = False          # soft delete, so offline copies learn about removals


class PartLine(BaseRow, TenantMixin, table=True):
    order_id: int = Field(index=True)
    sku: str = Field(default="", max_length=60)
    name: str = Field(default="", max_length=160)
    qty: float = 1.0
    price: float = 0.0
    part_id: int | None = None
    deleted: bool = False


def order_totals(work_lines: Iterable, part_lines: Iterable,
                 discount_pct: float, vat_pct: float) -> dict:
    """Order sums rounded to kopecks. VAT is included in the total (shown as "incl. VAT")."""
    works = round(sum((w.norm_hours or 0) * (w.rate or 0) for w in work_lines), 2)
    parts = round(sum((p.qty or 0) * (p.price or 0) for p in part_lines), 2)
    subtotal = round(works + parts, 2)
    discount = round(subtotal * (discount_pct or 0) / 100.0, 2)
    total = round(subtotal - discount, 2)
    vat = round(total - total / (1 + (vat_pct or 0) / 100.0), 2)
    return {"works": works, "parts": parts, "subtotal": subtotal,
            "discount": discount, "vat": vat, "total": total}
