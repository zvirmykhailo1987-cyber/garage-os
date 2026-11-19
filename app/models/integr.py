"""Integration journals and the audit log.

``OutboxMessage`` is a transactional outbox: every outgoing customer message is written
here first. The free stub leaves it ``SIMULATED``; a paid provider would mark it ``SENT``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field

from .base import BaseRow, TenantMixin, utcnow

MSG_SIMULATED, MSG_QUEUED, MSG_SENT, MSG_FAILED = "SIMULATED", "QUEUED", "SENT", "FAILED"
CHANNELS = ["sms", "viber", "telegram"]


class OutboxMessage(BaseRow, TenantMixin, table=True):
    __table_args__ = (Index("ix_outbox_order", "order_id"),)
    channel: str = Field(default="sms", max_length=20)
    to: str = Field(default="", max_length=60)
    text: str = Field(default="", max_length=1000)
    status: str = Field(default=MSG_SIMULATED, max_length=20)
    provider: str = Field(default="stub", max_length=30)
    order_id: int | None = None
    client_id: int | None = None


class PlateCache(BaseRow, table=True):
    """Global cache of licence-plate lookups (public vehicle data, not tenant data)."""
    plate: str = Field(default="", index=True, max_length=20)
    make: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=60)
    year: int | None = None
    vin: str = Field(default="", max_length=20)
    source: str = Field(default="stub", max_length=30)


class CallEvent(BaseRow, TenantMixin, table=True):
    direction: str = Field(default="in", max_length=5)
    phone: str = Field(default="", index=True, max_length=40)
    client_id: int | None = None
    status: str = Field(default="logged", max_length=20)
    provider: str = Field(default="stub", max_length=30)
    at: datetime = Field(default_factory=utcnow, index=True)


class AuditEntry(BaseRow, TenantMixin, table=True):
    """Write-action audit trail with a risk tier (Tier 2+ is always logged)."""
    __table_args__ = (Index("ix_audit_org_at", "org_id", "at"),)
    actor: str = Field(default="", max_length=120)
    action: str = Field(default="", max_length=60)
    tier: int = 0
    entity: str = Field(default="", max_length=40)
    entity_uid: str = Field(default="", max_length=32)
    detail: str = Field(default="", max_length=300)
    at: datetime = Field(default_factory=utcnow, index=True)
