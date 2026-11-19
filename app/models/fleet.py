# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Fleet customers: a contract, its vehicles, mileage-based maintenance, monthly acts."""
from __future__ import annotations

from sqlmodel import Field

from .base import BaseRow, TenantMixin


class FleetContract(BaseRow, TenantMixin, table=True):
    client_id: int = Field(index=True)
    name: str = Field(default="", max_length=120)
    credit_terms_days: int = 0
    price_list: str = Field(default="contract", max_length=40)
    active: bool = True


class FleetVehicle(BaseRow, TenantMixin, table=True):
    contract_id: int = Field(index=True)
    car_id: int = Field(index=True)
    unit_no: str = Field(default="", max_length=30)


class MaintenancePlan(BaseRow, TenantMixin, table=True):
    car_id: int = Field(index=True)
    interval_km: int = 15000
    last_service_km: int = 0
    next_due_km: int = 0


class MonthlyAct(BaseRow, TenantMixin, table=True):
    contract_id: int = Field(index=True)
    period: str = Field(default="", max_length=7)          # YYYY-MM
    orders: int = 0
    total: float = 0.0
