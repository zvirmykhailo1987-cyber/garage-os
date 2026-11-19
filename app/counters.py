# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Keeps ``StatusCount`` in step with ``WorkOrder`` inside the same transaction.

A ``before_flush`` hook turns pending inserts / status or branch changes / deletes into
per-(branch, status) deltas; ``after_flush`` applies them with a single atomic upsert per
key (``INSERT … ON CONFLICT DO UPDATE SET n = n + excluded.n``), which is safe under
concurrent writers on both SQLite and Postgres.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import event, func, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, delete, select

from .models import StatusCount, WorkOrder

_KEY = "status_count_deltas"


def _old(obj: Any, attr: str) -> Any:
    hist = inspect(obj).attrs[attr].history
    return hist.deleted[0] if hist.deleted else getattr(obj, attr)


@event.listens_for(Session, "before_flush")
def _collect(session: Session, _ctx: Any, _instances: Any) -> None:
    deltas: dict[tuple[int, int, str], int] = session.info.setdefault(_KEY, defaultdict(int))
    for obj in session.new:
        if isinstance(obj, WorkOrder) and obj.branch_id is not None and obj.org_id is not None:
            deltas[(obj.org_id, obj.branch_id, obj.status)] += 1
    for obj in session.dirty:
        if not isinstance(obj, WorkOrder) or not session.is_modified(obj):
            continue
        old_status, old_branch = _old(obj, "status"), _old(obj, "branch_id")
        if (old_status, old_branch) != (obj.status, obj.branch_id) and obj.org_id is not None:
            if old_branch is not None:
                deltas[(obj.org_id, old_branch, old_status)] -= 1
            if obj.branch_id is not None:
                deltas[(obj.org_id, obj.branch_id, obj.status)] += 1
    for obj in session.deleted:
        if isinstance(obj, WorkOrder) and obj.branch_id is not None and obj.org_id is not None:
            deltas[(obj.org_id, _old(obj, "branch_id"), _old(obj, "status"))] -= 1


@event.listens_for(Session, "after_flush")
def _apply(session: Session, _ctx: Any) -> None:
    deltas = session.info.pop(_KEY, None)
    if not deltas:
        return
    conn = session.connection()
    ins = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    table = StatusCount.__table__  # type: ignore[attr-defined]
    for (org_id, branch_id, status), d in deltas.items():
        if d == 0:
            continue
        stmt = ins(table).values(org_id=org_id, branch_id=branch_id, status=status, n=d)
        conn.execute(stmt.on_conflict_do_update(index_elements=["branch_id", "status"],
                                                set_={"n": table.c.n + stmt.excluded.n}))


@event.listens_for(Session, "after_rollback")
def _discard(session: Session) -> None:
    session.info.pop(_KEY, None)


def recount(s: Session, org_id: int | None = None) -> None:
    """Rebuild the counters from the order table (after bulk loads; repair command)."""
    q = delete(StatusCount)
    src = select(WorkOrder.org_id, WorkOrder.branch_id, WorkOrder.status, func.count()) \
        .where(col(WorkOrder.branch_id).is_not(None))
    if org_id is not None:
        q = q.where(col(StatusCount.org_id) == org_id)
        src = src.where(WorkOrder.org_id == org_id)
    s.exec(q)  # type: ignore[call-overload]
    for org, branch, status, n in s.exec(src.group_by(col(WorkOrder.org_id), col(WorkOrder.branch_id),
                                                       col(WorkOrder.status))).all():
        if org is None or branch is None:
            continue
        s.add(StatusCount(org_id=org, branch_id=branch, status=status, n=n))
    s.commit()
