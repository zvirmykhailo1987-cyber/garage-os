# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""CRM entities. All are tenant rows (org_id + branch_id).

``Service`` may be organization-wide: ``branch_id=None`` but ``org_id`` set — it is
shared by the branches of THAT organization only.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field

from .base import BaseRow, TenantMixin

CLIENT_KINDS = ["private", "company", "fleet"]


class Client(BaseRow, TenantMixin, table=True):
    __table_args__ = (
        Index("ix_client_branch_phone", "branch_id", "phone"),
        Index("ix_client_branch_name", "branch_id", "name"),
    )
    name: str = Field(default="", index=True, max_length=160)
    phone: str = Field(default="", index=True, max_length=40)
    kind: str = Field(default="private", max_length=20)
    discount: float = 0.0
    tax_id: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=500)
    tg_code_hash: str = Field(default="", index=True, max_length=64)   # one-time Telegram link code
    tg_code_expires: datetime | None = None


class Car(BaseRow, TenantMixin, table=True):
    __table_args__ = (Index("ix_car_branch_plate", "branch_id", "plate"),)
    client_id: int | None = Field(default=None, index=True)
    plate: str = Field(default="", index=True, max_length=20)
    vin: str = Field(default="", max_length=20)
    make: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=60)
    year: int | None = None
    engine: str = Field(default="", max_length=40)
    mileage: int | None = None
    color: str = Field(default="", max_length=30)


class Master(BaseRow, TenantMixin, table=True):
    """Mechanic with an hourly labour rate (copied into each WorkLine at the time)."""
    name: str = Field(default="", index=True, max_length=120)
    rate: float = 450.0
    active: bool = True


class Service(BaseRow, TenantMixin, table=True):
    name: str = Field(default="", index=True, max_length=160)
    norm_hours: float = 1.0


class Part(BaseRow, TenantMixin, table=True):
    sku: str = Field(default="", index=True, max_length=60)
    name: str = Field(default="", index=True, max_length=160)
    brand: str = Field(default="", max_length=60)
    supplier: str = Field(default="", max_length=80)
    location: str = Field(default="", max_length=40)
    qty: float = 0.0
    min_qty: float = 0.0
    cost: float = 0.0
    price: float = 0.0
