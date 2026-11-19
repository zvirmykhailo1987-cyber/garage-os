"""Risk tiers and the audit log.

Tier 0 read · Tier 1 reversible · Tier 2 impactful (create order, change status) ·
Tier 3 irreversible or external (message a customer, fiscal receipt, data migration) —
Tier 3 requires an explicit confirmation from the user on the request itself.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session

from .models import AuditEntry

TIER0, TIER1, TIER2, TIER3 = 0, 1, 2, 3


def log(s: Session, *, actor: str, action: str, tier: int, org_id: int | None,
        branch_id: int | None = None, entity: str = "", entity_uid: str = "",
        detail: str = "") -> None:
    """Add an audit row. The caller commits (same transaction as the change itself)."""
    s.add(AuditEntry(actor=(actor or "?")[:120], action=action, tier=tier, org_id=org_id,
                     branch_id=branch_id, entity=entity, entity_uid=entity_uid,
                     detail=detail[:300]))


def require_confirm(tier: int, confirmed: bool) -> None:
    if tier >= TIER3 and not confirmed:
        raise HTTPException(status_code=428, detail="err.confirm_required")
