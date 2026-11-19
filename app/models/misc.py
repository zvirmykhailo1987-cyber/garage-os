# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Plans, counters, AI run log, sync metadata, Telegram links."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from .base import BaseRow, TenantMixin, new_uid, utcnow


class Plan(BaseRow, table=True):
    """Subscription plan (global, not tenant data)."""
    code: str = Field(default="", index=True, unique=True, max_length=30)
    title: str = Field(default="", max_length=60)
    price_month: float = 0.0
    max_branches: int = 1
    max_users: int = 3
    ai_enabled: bool = True


class BranchCounter(SQLModel, table=True):
    """Per-branch order-number sequence: O(1) and race-safe (row lock on Postgres)."""
    branch_id: int = Field(primary_key=True)
    next_seq: int = 1001


class AiRun(BaseRow, TenantMixin, table=True):
    """Observability for the AI runtime: which skill, which provider, fallback or not."""
    skill: str = Field(default="", index=True, max_length=40)
    provider: str = Field(default="stub", max_length=20)
    confidence: float = 0.0
    fallback: bool = False
    ms: int = 0
    actor: str = Field(default="", max_length=120)


class TelegramLink(BaseRow, TenantMixin, table=True):
    """A Telegram chat bound to one client (via a one-time code issued in the CRM)."""
    client_id: int = Field(index=True)
    chat_id: str = Field(default="", index=True, max_length=32)


class SyncMeta(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=40)
    value: str = Field(default="", max_length=200)


class SyncConflict(SQLModel, table=True):
    """A rejected edge edit (stale ``base_version``). Kept for manual review — never silent."""
    id: int | None = Field(default=None, primary_key=True)
    uid: str = Field(default_factory=new_uid, index=True, max_length=32)
    org_id: int | None = Field(default=None, index=True)
    entity: str = Field(default="", max_length=40)
    row_uid: str = Field(default="", index=True, max_length=32)
    hub_version: int = 0
    base_version: int = 0
    incoming: str = Field(default="", max_length=8000)      # JSON of the rejected edit
    device: str = Field(default="", max_length=80)
    at: datetime = Field(default_factory=utcnow, index=True)
    reviewed: bool = False


class StatusCount(SQLModel, table=True):
    """Denormalised number of work orders per (branch, status).

    Maintained atomically by a session hook (``app/counters.py``) on every insert, status
    change, branch move or delete, so navigation badges, the dashboard pipeline and the
    board headers read a handful of rows instead of counting the whole order table."""
    branch_id: int = Field(primary_key=True)
    status: str = Field(primary_key=True, max_length=20)
    org_id: int = Field(index=True)
    n: int = 0
