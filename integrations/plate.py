# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Licence-plate lookup.

The stub answers from the caller's OWN vehicles first, then from the global public cache.
No data → ``None`` (it never invents a vehicle). A paid registry API is a seam.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import col, select

from app import config
from app.models import Car, PlateCache

from .base import PlateLookupPort, SeamNotConnected


def normalize_plate(plate: str) -> str:
    return "".join((plate or "").upper().split())[:20]


class PlateStub:
    provider = "stub"

    def lookup(self, session: Any, plate: str, org_id: int,
               branch_ids: list[int] | None = None) -> dict | None:
        p = normalize_plate(plate)
        if not p:
            return None
        stmt = select(Car).where(Car.org_id == org_id, Car.plate == p)
        if branch_ids is not None:                      # a branch user sees only its branches
            stmt = stmt.where(col(Car.branch_id).in_(branch_ids or [-1]))
        car = session.exec(stmt).first()
        if car and (car.make or car.vin):
            return {"make": car.make, "model": car.model, "year": car.year,
                    "vin": car.vin, "source": "own-records"}
        pc = session.exec(select(PlateCache).where(PlateCache.plate == p)).first()
        if pc:
            return {"make": pc.make, "model": pc.model, "year": pc.year,
                    "vin": pc.vin, "source": "cache"}
        return None


class RegistryProvider:
    provider = "registry"

    def lookup(self, session: Any, plate: str, org_id: int,
               branch_ids: list[int] | None = None) -> dict | None:
        raise SeamNotConnected("vehicle registry API key is not configured")


def get_plate() -> PlateLookupPort:
    return RegistryProvider() if config.PLATE_PROVIDER == "registry" else PlateStub()
