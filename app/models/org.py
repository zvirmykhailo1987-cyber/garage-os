# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Tenancy and identity: organizations, branches, users, sessions, tokens."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from .base import BaseRow, utcnow

ROLE_NET_ADMIN = "net_admin"          # tenant owner — every branch of the organization
ROLE_BRANCH_MANAGER = "branch_manager"
ROLE_RECEPTIONIST = "receptionist"
ROLE_MASTER = "master"                # mechanic
ROLES = [ROLE_NET_ADMIN, ROLE_BRANCH_MANAGER, ROLE_RECEPTIONIST, ROLE_MASTER]

ORG_TRIAL, ORG_ACTIVE, ORG_SUSPENDED = "trial", "active", "suspended"
ORG_STATUSES = [ORG_TRIAL, ORG_ACTIVE, ORG_SUSPENDED]


class Organization(BaseRow, table=True):
    """A SaaS tenant: one service-station network."""
    name: str = Field(default="", index=True, max_length=120)
    slug: str = Field(default="", index=True, max_length=60)
    plan_code: str = Field(default="trial", max_length=30)
    status: str = Field(default=ORG_TRIAL, max_length=20)
    trial_ends_at: datetime | None = None


class Branch(BaseRow, table=True):
    """A service station inside an organization. ``order_prefix`` prefixes order numbers."""
    org_id: int = Field(index=True)
    name: str = Field(default="", max_length=120)
    region: str = Field(default="", max_length=80)
    order_prefix: str = Field(default="WO", max_length=6)
    address: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)
    active: bool = True


class User(BaseRow, table=True):
    org_id: int | None = Field(default=None, index=True)     # None = platform operator
    email: str = Field(default="", index=True, unique=True, max_length=254)
    name: str = Field(default="", max_length=120)
    password_hash: str = Field(default="", max_length=255)
    is_superadmin: bool = False
    active: bool = True
    failed_logins: int = 0
    locked_until: datetime | None = None


class Membership(BaseRow, table=True):
    """User ↔ branch with a role. A net_admin row may have ``branch_id=None`` (all branches)."""
    org_id: int = Field(index=True)
    user_id: int = Field(index=True)
    branch_id: int | None = Field(default=None, index=True)
    role: str = Field(default=ROLE_RECEPTIONIST, max_length=30)


class UserSession(SQLModel, table=True):
    """Server-side web session. The cookie carries a random token; only its hash is stored,
    so a database leak does not leak live sessions. Logout sets ``revoked_at``."""
    id: str = Field(primary_key=True, max_length=64)            # sha256(token)
    user_id: int = Field(index=True)
    csrf_token: str = Field(max_length=64)
    active_branch_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked_at: datetime | None = None
    ip: str = Field(default="", max_length=64)


class ApiToken(BaseRow, table=True):
    """REST bearer token (separate from web sessions). Only the hash is stored."""
    org_id: int = Field(index=True)
    branch_id: int | None = None
    token_hash: str = Field(default="", index=True, unique=True, max_length=64)
    prefix: str = Field(default="", max_length=12)               # shown in the UI for recognition
    label: str = Field(default="", max_length=80)
    subject_type: str = Field(default="bot", max_length=20)      # bot | fleet | integration
    subject_id: int | None = None                             # e.g. FleetContract.id
    scopes: str = Field(default="", max_length=300)              # csv
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_by: int | None = None
    active: bool = True


class SyncDevice(BaseRow, table=True):
    """An offline edge copy allowed to sync with the hub — one token per device."""
    org_id: int = Field(index=True)
    label: str = Field(default="", max_length=80)
    token_hash: str = Field(default="", index=True, unique=True, max_length=64)
    prefix: str = Field(default="", max_length=12)
    last_seen_at: datetime | None = None
    active: bool = True
