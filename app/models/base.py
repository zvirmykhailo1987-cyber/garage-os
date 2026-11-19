"""Shared model mixins.

Every domain row carries:
  id          local primary key (differs between offline copies)
  uid         globally unique id generated at the edge — the sync identity
  created_at / updated_at (UTC)
  srv_at      when THIS database last touched the row (delta-pull cursor)
  version     optimistic-lock counter, bumped on every write
  sync_base   hub version this copy last saw (edge copies only; 0 = never synced)

``TenantMixin`` adds the two isolation keys: ``org_id`` (the SaaS tenant, enforced by
Postgres RLS) and ``branch_id`` (a service station inside the tenant).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp — every stored datetime is UTC."""
    return datetime.now(UTC)


def new_uid() -> str:
    return uuid.uuid4().hex


class BaseRow(SQLModel):
    id: int | None = Field(default=None, primary_key=True)
    uid: str = Field(default_factory=new_uid, index=True, unique=True, max_length=32)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    srv_at: datetime = Field(default_factory=utcnow, index=True)
    version: int = Field(default=1)
    sync_base: int = Field(default=0)


class TenantMixin(SQLModel):
    org_id: int | None = Field(default=None, index=True)
    branch_id: int | None = Field(default=None, index=True)
