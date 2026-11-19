# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Postgres row-level security (runs only when STO_TEST_DATABASE_URL points at Postgres)."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, func, select

from app.db import engine, set_tenant
from app.models import TENANT_MODELS, Client, WorkOrder
from app.rls import assert_runtime_role_is_restricted, tenant_tables
from tests.conftest import IS_PG

pg = pytest.mark.skipif(not IS_PG, reason="needs Postgres (STO_TEST_DATABASE_URL)")


def test_every_model_with_org_id_is_protected():
    from sqlmodel import SQLModel
    with_org = {t.name for t in SQLModel.metadata.tables.values() if "org_id" in t.c}
    exempt = {"organization", "user", "apitoken", "syncdevice"}     # looked up before a tenant is known
    assert with_org - exempt == set(tenant_tables())
    assert len(TENANT_MODELS) + 3 == len(set(tenant_tables()))


@pg
def test_no_tenant_context_means_zero_rows(world):
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(WorkOrder)).one() == 0
        assert s.exec(select(func.count()).select_from(Client)).one() == 0
        for t in tenant_tables():
            assert s.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() == 0, t


@pg
def test_context_shows_only_own_tenant(world):
    with Session(engine) as s:
        set_tenant(s, world["orgA"])
        orgs = set(s.exec(select(WorkOrder.org_id)).all())
        assert orgs == {world["orgA"]}
        s.commit()                                          # next transaction keeps the context
        assert set(s.exec(select(Client.org_id)).all()) == {world["orgA"]}


@pg
def test_cannot_write_into_other_tenant(world):
    with Session(engine) as s:
        set_tenant(s, world["orgA"])
        s.add(Client(name="smuggled", org_id=world["orgB"], branch_id=world["b1"]))
        with pytest.raises(DBAPIError):
            s.commit()


@pg
def test_context_does_not_leak_through_the_pool(world):
    with Session(engine) as s:
        set_tenant(s, world["orgA"])
        assert s.exec(select(func.count()).select_from(WorkOrder)).one() > 0
    for _ in range(5):                                      # fresh sessions reuse pooled connections
        with Session(engine) as s:
            assert s.exec(select(func.count()).select_from(WorkOrder)).one() == 0


@pg
def test_runtime_role_cannot_bypass_rls():
    assert_runtime_role_is_restricted(engine)
