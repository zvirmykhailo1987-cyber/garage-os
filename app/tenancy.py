# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""The single tenant-isolation seam.

Rules (each one has an HTTP regression test):
  * every domain query goes through ``scoped()`` / ``scoped_where()``;
  * every lookup by id or by number goes through ``get_scoped()`` / ``find_order()``
    and answers 404 — not 403 — for another tenant's row (no existence oracle);
  * a row with ``branch_id=None`` is visible only inside its own organization, and only
    where the caller explicitly asks for shared rows (organization-wide services);
  * the platform operator (superadmin) manages tenants but cannot read tenant data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy import false
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from .models import (
    ORG_SUSPENDED,
    ORG_TRIAL,
    ROLE_NET_ADMIN,
    Branch,
    Membership,
    Organization,
    Plan,
    User,
    WorkOrder,
    utcnow,
)
from .permissions import WRITE_PERMISSIONS, role_allows

T = TypeVar("T")


@dataclass
class UserContext:
    user_id: int | None = None
    name: str = ""
    email: str = ""
    org_id: int | None = None
    org: Organization | None = None
    plan: Plan | None = None
    is_superadmin: bool = False
    is_net_admin: bool = False
    roles: dict[int, str] = field(default_factory=dict)      # branch_id -> role
    branch_ids: list[int] = field(default_factory=list)       # all branches of own org allowed
    active_branch_id: int | None = None
    csrf_token: str = ""
    session_id: str = ""

    @property
    def scope_ids(self) -> list[int]:
        if self.active_branch_id and self.active_branch_id in self.branch_ids:
            return [self.active_branch_id]
        return self.branch_ids

    def role_in(self, branch_id: int | None) -> str | None:
        if branch_id is None:
            return ROLE_NET_ADMIN if self.is_net_admin else None
        return self.roles.get(branch_id)

    @property
    def writable(self) -> bool:
        return org_is_writable(self.org)

    def can(self, perm: str, branch_id: int | None = None) -> bool:
        """Permission check. With ``branch_id`` — the role in that branch; without — any
        branch in the current scope (used for lists and navigation)."""
        if self.org_id is None:
            return False
        if perm in WRITE_PERMISSIONS and not self.writable:
            return False
        if perm == "ai.use" and self.plan is not None and not self.plan.ai_enabled:
            return False
        if branch_id is not None:
            return branch_id in self.branch_ids and role_allows(self.role_in(branch_id), perm)
        if perm in ("team.manage", "tokens.manage", "migration.run"):
            return self.is_net_admin
        return any(role_allows(self.roles.get(b), perm) for b in self.scope_ids)


def org_is_writable(org: Organization | None) -> bool:
    """False while the subscription is suspended or the trial has run out."""
    if org is None or org.status == ORG_SUSPENDED:
        return False
    return not (org.status == ORG_TRIAL and org.trial_ends_at and org.trial_ends_at < utcnow())


def org_writable(s: Session, org_id: int | None) -> bool:
    return org_is_writable(s.get(Organization, org_id) if org_id else None)


def load_user_context(s: Session, user: User, active_branch_id: int | None = None) -> UserContext:
    ctx = UserContext(user_id=user.id, name=user.name, email=user.email,
                      org_id=user.org_id, is_superadmin=user.is_superadmin)
    if user.org_id is None:
        return ctx                                   # platform operator: no tenant data at all
    ctx.org = s.get(Organization, user.org_id)
    if ctx.org is not None:
        ctx.plan = s.exec(select(Plan).where(Plan.code == ctx.org.plan_code)).first()
    branches = s.exec(select(Branch).where(Branch.org_id == user.org_id,
                                           Branch.active == True)).all()  # noqa: E712
    own = {b.id for b in branches if b.id is not None}
    for m in s.exec(select(Membership).where(Membership.user_id == user.id,
                                             Membership.org_id == user.org_id)).all():
        if m.role == ROLE_NET_ADMIN:
            ctx.is_net_admin = True
        elif m.branch_id in own and m.branch_id is not None:
            ctx.roles[m.branch_id] = m.role
    if ctx.is_net_admin:
        ctx.roles = dict.fromkeys(own, ROLE_NET_ADMIN)
    ctx.branch_ids = sorted(ctx.roles)
    if active_branch_id in ctx.branch_ids:
        ctx.active_branch_id = active_branch_id
    elif len(ctx.branch_ids) == 1:
        ctx.active_branch_id = ctx.branch_ids[0]
    return ctx


def scoped_where(stmt: Any, model: Any, ctx: UserContext, include_shared: bool = False) -> Any:
    """Add the tenant filter to any select (including aggregate selects)."""
    ids = ctx.scope_ids
    if ctx.org_id is None or not ids:
        return stmt.where(false())
    cond = (model.org_id == ctx.org_id) & (model.branch_id.in_(ids))
    if include_shared:
        cond = cond | ((model.org_id == ctx.org_id) & (model.branch_id.is_(None)))
    return stmt.where(cond)


def scoped(model: type[T], ctx: UserContext, include_shared: bool = False) -> SelectOfScalar[T]:
    return cast("SelectOfScalar[T]", scoped_where(select(model), model, ctx, include_shared))


def can_access(ctx: UserContext, obj: Any, include_shared: bool = False) -> bool:
    if obj is None or ctx.org_id is None or getattr(obj, "org_id", None) != ctx.org_id:
        return False
    bid = getattr(obj, "branch_id", None)
    if bid is None:
        return include_shared
    return bid in ctx.branch_ids


def get_scoped(s: Session, model: type[T], obj_id: int, ctx: UserContext,
               include_shared: bool = False, for_update: bool = False) -> T:
    """``for_update`` takes a row lock on Postgres (``SELECT … FOR UPDATE``) so that a
    version check, a fiscal-receipt check etc. cannot race a concurrent writer."""
    obj = s.get(model, obj_id, with_for_update=for_update and s.get_bind().dialect.name == "postgresql")
    if not can_access(ctx, obj, include_shared):
        raise HTTPException(status_code=404)
    return obj  # type: ignore[return-value]


def find_order(s: Session, number: str, org_id: int | None,
               branch_ids: list[int] | None = None) -> WorkOrder | None:
    """Look an order up by its human number INSIDE one tenant (numbers repeat across orgs)."""
    if org_id is None:
        return None
    stmt = select(WorkOrder).where(WorkOrder.org_id == org_id,
                                   WorkOrder.number == number.strip().upper())
    if branch_ids is not None:
        stmt = stmt.where(col(WorkOrder.branch_id).in_(branch_ids or [-1]))
    return s.exec(stmt).first()


def write_branch_id(ctx: UserContext) -> int | None:
    if ctx.active_branch_id in ctx.branch_ids:
        return ctx.active_branch_id
    if len(ctx.branch_ids) == 1:
        return ctx.branch_ids[0]
    return None


def stamp(obj: Any, ctx: UserContext, branch_id: int | None) -> Any:
    """Set both isolation keys on a new row."""
    obj.org_id = ctx.org_id
    obj.branch_id = branch_id
    return obj


def touch(obj: Any) -> None:
    now = utcnow()
    obj.updated_at = now
    obj.srv_at = now
    obj.version = (obj.version or 1) + 1


__all__ = [
    "UserContext",
    "can_access",
    "find_order",
    "get_scoped",
    "load_user_context",
    "scoped",
    "scoped_where",
    "stamp",
    "touch",
    "write_branch_id",
]
