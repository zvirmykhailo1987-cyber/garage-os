# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Fiscal receipts (software cash register). The stub issues a clearly-marked SIMULATED
number and fiscalises nothing. Fiscalisation is irreversible — Tier 3 in the route."""
from __future__ import annotations

from typing import Any

from app import config
from app.models import utcnow

from .base import FiscalPort, SeamNotConnected


class FiscalStub:
    provider = "stub"

    def post_receipt(self, session: Any, order: Any, total: float) -> dict:
        stamp = utcnow().strftime("%Y%m%d%H%M%S")
        return {"fiscal_no": f"SIMULATED-{order.number}-{stamp}", "status": "SIMULATED",
                "total": total, "provider": "stub"}


class CashRegisterProvider:
    provider = "cash-register"

    def post_receipt(self, session: Any, order: Any, total: float) -> dict:
        raise SeamNotConnected("cash-register key / contract is not configured")


def get_fiscal() -> FiscalPort:
    return CashRegisterProvider() if config.FISCAL_PROVIDER == "register" else FiscalStub()
