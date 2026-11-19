# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Public demo: the shared demo accounts cannot be taken over.

The demo publishes one password for every demo account, so any visitor could otherwise
deactivate a colleague's account, suspend a demo organization from the operator console or
lock an account out with wrong passwords. With ``DEMO_MODE=1`` these actions are refused with
``err.demo_account``; outside the demo this module changes nothing.
"""
from __future__ import annotations

from fastapi import HTTPException

from . import config

DEMO_LOCKED = "err.demo_account"


def is_protected(email: str | None) -> bool:
    """True only in demo mode, for one of the published demo accounts."""
    if not config.DEMO_MODE:
        return False
    # lazy: demo.py imports the security module
    from .demo import DEMO_ACCOUNTS
    return (email or "").strip().lower() in {e for e, _, _ in DEMO_ACCOUNTS}


def refuse_if_protected(*emails: str | None) -> None:
    if any(is_protected(e) for e in emails):
        raise HTTPException(status_code=403, detail=DEMO_LOCKED)
