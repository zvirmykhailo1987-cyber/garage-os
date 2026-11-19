"""Engines, sessions and the per-transaction tenant context for Postgres RLS.

How tenant isolation reaches the database
-----------------------------------------
Every request that resolves a user (web) or a token (REST/sync) calls
``set_tenant(session, org_id)``. On Postgres an ``after_begin`` hook then runs
``set_config('app.current_org', <org>, true)`` at the start of EVERY transaction of
that session — transaction-local, so a pooled connection never leaks a tenant to the
next request. Row-level-security policies (app/rls.py) compare ``org_id`` with that
setting; with no context the setting is empty and every tenant table returns 0 rows.

SQLite has no RLS, so there the application-level filter in ``tenancy.py`` is the
only layer; on Postgres both layers apply (defence in depth).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import config

ORG_KEY = "tenant_org_id"
WRITE_INTENT = "sqlite_write_intent"


def write_intent(session: Session) -> None:
    """Mark a session as writing: on SQLite every transaction it begins from now on — including
    those after an intermediate commit — starts with BEGIN IMMEDIATE. Call before the first
    query (a transaction that is already open keeps its mode)."""
    if session.info.get(WRITE_INTENT):
        return
    session.info[WRITE_INTENT] = True
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.bind = bind.execution_options(**{WRITE_INTENT: True})


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
            dbapi_conn.isolation_level = None      # let SQLAlchemy emit BEGIN itself (below)

        @event.listens_for(eng, "begin")
        def _begin(conn: Any) -> None:
            # Write requests begin IMMEDIATE: pysqlite would otherwise take the write lock only
            # at the first write, so two requests could both pass an optimistic-lock check.
            # Reads begin DEFERRED and run concurrently under WAL. (Postgres uses FOR UPDATE.)
            immediate = conn.get_execution_options().get(WRITE_INTENT)
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

        return eng
    return create_engine(
        url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800,
        connect_args={"connect_timeout": 10, "options": "-c statement_timeout=15000"},
    )


config.ensure_dirs()
engine = make_engine(config.DATABASE_URL)
admin_engine = engine if config.DATABASE_ADMIN_URL == config.DATABASE_URL \
    else make_engine(config.DATABASE_ADMIN_URL)

_SET_ORG = text("SELECT set_config('app.current_org', :org, true)")


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, _transaction: Any, connection: Any) -> None:
    if connection.dialect.name != "postgresql":
        return
    org = session.info.get(ORG_KEY)
    connection.execute(_SET_ORG, {"org": str(org) if org else ""})


def set_tenant(session: Session, org_id: int | None) -> None:
    """Bind the session to one tenant (or none). Applies to the current transaction too."""
    session.info[ORG_KEY] = org_id
    bind = session.get_bind()
    if bind.dialect.name == "postgresql" and session.in_transaction():
        session.execute(_SET_ORG, {"org": str(org_id) if org_id else ""})


def create_schema(eng: Engine | None = None) -> None:
    from . import models  # noqa: F401  (registers tables)
    SQLModel.metadata.create_all(eng or admin_engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. The session starts WITHOUT a tenant: tenant tables are empty
    on Postgres until a dependency resolves the caller and calls ``set_tenant``."""
    with Session(engine) as s:
        yield s


from . import counters  # noqa: E402,F401  (registers the StatusCount session hooks)
