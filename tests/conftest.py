# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Test harness.

The whole suite runs against SQLite by default. Set ``STO_TEST_DATABASE_URL`` (restricted
app role) and ``STO_TEST_DATABASE_ADMIN_URL`` (owner) to run it against Postgres with
row-level security enabled — CI does both.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="sto-test-"))
os.environ["STO_ENV"] = "test"
os.environ["DATA_DIR"] = str(_TMP)
os.environ.setdefault("STO_SECRET", "test-secret-" + "x" * 32)
if os.environ.get("STO_TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["STO_TEST_DATABASE_URL"]
    os.environ["DATABASE_ADMIN_URL"] = os.environ.get("STO_TEST_DATABASE_ADMIN_URL",
                                                      os.environ["STO_TEST_DATABASE_URL"])
else:
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.sqlite'}"
    os.environ.pop("DATABASE_ADMIN_URL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app import config  # noqa: E402
from app.db import admin_engine, create_schema, engine  # noqa: E402
from app.manage import seed_plans  # noqa: E402
from app.models import (  # noqa: E402
    ROLE_BRANCH_MANAGER,
    ROLE_MASTER,
    ROLE_NET_ADMIN,
    ROLE_RECEPTIONIST,
    ST_ESTIMATE,
    ST_NEW,
    ApiToken,
    Branch,
    Car,
    Client,
    FleetContract,
    FleetVehicle,
    Master,
    Membership,
    Organization,
    Part,
    PartLine,
    Service,
    SyncDevice,
    User,
    WorkLine,
    WorkOrder,
)
from app.security import api_limiter, hash_password, login_limiter, sha256_hex, signup_limiter  # noqa: E402

PASSWORD = "test-correct-horse-battery"
IS_PG = config.IS_POSTGRES


_PG_READY = False


def _reset_schema() -> None:
    global _PG_READY
    if IS_PG:
        if not _PG_READY:                     # build schema + RLS once, then truncate per test
            with admin_engine.begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
            create_schema(admin_engine)
            from app import rls
            rls.apply(admin_engine, config.DATABASE_URL)
            _PG_READY = True
        names = ", ".join(f'"{t.name}"' for t in SQLModel.metadata.sorted_tables)
        with admin_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    else:
        SQLModel.metadata.drop_all(admin_engine)
        create_schema(admin_engine)


@pytest.fixture(autouse=True)
def db():
    engine.dispose()
    _reset_schema()
    with Session(admin_engine) as s:
        seed_plans(s)
    for lim in (login_limiter, signup_limiter, api_limiter):
        lim.reset()
    yield
    engine.dispose()


@dataclass
class World:
    ids: dict = field(default_factory=dict)

    def __getitem__(self, k):
        return self.ids[k]


def _add(s: Session, obj):
    s.add(obj)
    s.flush()
    return obj


@pytest.fixture
def world() -> World:
    """Two tenants. Org A: two branches (A1, A2) and one user per role. Org B: one branch."""
    w = World()
    pw = hash_password(PASSWORD)
    with Session(admin_engine) as s:
        for key, name, plan in (("A", "Alpha Network", "network"), ("B", "Beta Garage", "network")):
            org = _add(s, Organization(name=name, slug=key.lower(), plan_code=plan, status="active"))
            w.ids[f"org{key}"] = org.id
        a1 = _add(s, Branch(org_id=w["orgA"], name="Alpha One", order_prefix="AON"))
        a2 = _add(s, Branch(org_id=w["orgA"], name="Alpha Two", order_prefix="ATW"))
        b1 = _add(s, Branch(org_id=w["orgB"], name="Beta One", order_prefix="BON"))
        w.ids.update(a1=a1.id, a2=a2.id, b1=b1.id)

        def user(email, org, role, branch):
            u = _add(s, User(org_id=org, email=email, name=email.split("@")[0], password_hash=pw))
            _add(s, Membership(org_id=org, user_id=u.id, role=role, branch_id=branch))
            return u.id

        w.ids["owner"] = user("owner@a.test", w["orgA"], ROLE_NET_ADMIN, None)
        w.ids["manager"] = user("manager@a.test", w["orgA"], ROLE_BRANCH_MANAGER, a1.id)
        w.ids["desk"] = user("desk@a.test", w["orgA"], ROLE_RECEPTIONIST, a1.id)
        w.ids["mech"] = user("mech@a.test", w["orgA"], ROLE_MASTER, a1.id)
        w.ids["desk2"] = user("desk2@a.test", w["orgA"], ROLE_RECEPTIONIST, a2.id)
        w.ids["ownerB"] = user("owner@b.test", w["orgB"], ROLE_NET_ADMIN, None)
        op = _add(s, User(org_id=None, email="op@platform.test", name="Operator", password_hash=pw,
                          is_superadmin=True))
        w.ids["op"] = op.id

        for key, org, br in (("A", w["orgA"], a1.id), ("A2", w["orgA"], a2.id), ("B", w["orgB"], b1.id)):
            m = _add(s, Master(name=f"Mech {key}", rate=500, org_id=org, branch_id=br))
            c = _add(s, Client(name=f"Client {key}", phone=f"+38000000000{len(key)}{key[-1]}",
                               org_id=org, branch_id=br))
            car = _add(s, Car(plate=f"AA{1000 + len(w.ids)}{key[:2].ljust(2, 'X')}", make="Skoda",
                              model="Octavia", client_id=c.id, mileage=50000, org_id=org, branch_id=br))
            prefix = {"A": "AON", "A2": "ATW", "B": "BON"}[key]
            o = _add(s, WorkOrder(number=f"{prefix}-001001", status=ST_ESTIMATE, car_id=car.id,
                                  client_id=c.id, master_id=m.id, org_id=org, branch_id=br))
            wl = _add(s, WorkLine(order_id=o.id, service="Brake pads", norm_hours=1, rate=500,
                                  org_id=org, branch_id=br))
            pl = _add(s, PartLine(order_id=o.id, name="Pads", qty=1, price=1000, org_id=org, branch_id=br))
            _add(s, Part(sku=f"SKU-{key}", name=f"Part {key}", qty=1, min_qty=2, price=100,
                         org_id=org, branch_id=br))
            o.works_total, o.parts_total, o.total = 500, 1000, 1500
            w.ids.update({f"master{key}": m.id, f"client{key}": c.id, f"car{key}": car.id,
                          f"order{key}": o.id, f"wl{key}": wl.id, f"pl{key}": pl.id,
                          f"number{key}": o.number, f"plate{key}": car.plate})
        # second order in A1 (for IDOR on lines)
        o2 = _add(s, WorkOrder(number="AON-001002", status=ST_NEW, org_id=w["orgA"], branch_id=a1.id))
        w.ids["orderA_2"] = o2.id
        # org-wide shared service of org B — must never show up for org A
        _add(s, Service(name="Beta secret service", org_id=w["orgB"], branch_id=None))
        _add(s, Service(name="Alpha shared service", org_id=w["orgA"], branch_id=None))
        fc = _add(s, FleetContract(client_id=w["clientB"], name="Beta fleet", org_id=w["orgB"], branch_id=b1.id))
        _add(s, FleetVehicle(contract_id=fc.id, car_id=w["carB"], unit_no="B-1", org_id=w["orgB"], branch_id=b1.id))
        w.ids["fleetB"] = fc.id
        fa = _add(s, FleetContract(client_id=w["clientA"], name="Alpha fleet", org_id=w["orgA"], branch_id=a1.id))
        _add(s, FleetVehicle(contract_id=fa.id, car_id=w["carA"], unit_no="A-1", org_id=w["orgA"], branch_id=a1.id))
        w.ids["fleetA"] = fa.id
        s.commit()
    return w


# ── HTTP helpers ──────────────────────────────────────────────────────────────
_CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_BODY = re.compile(r'data-csrf="([^"]+)"')


class Browser:
    """A TestClient that knows how to log in and attach CSRF tokens."""

    def __init__(self, raise_errors: bool = True) -> None:
        from app.main import app
        self.c = TestClient(app, raise_server_exceptions=raise_errors, follow_redirects=False)
        self.csrf = ""

    def login(self, email: str, password: str = PASSWORD, expect_ok: bool = True):
        page = self.c.get("/login")
        token = _CSRF_INPUT.search(page.text).group(1)
        r = self.c.post("/login", data={"email": email, "password": password, "csrf_token": token})
        if expect_ok:
            assert r.status_code == 303, r.text[:300]
            self.refresh_csrf("/superadmin" if r.headers["location"] == "/superadmin" else "/settings")
        return r

    def refresh_csrf(self, path: str = "/settings") -> None:
        r = self.c.get(path)
        m = _CSRF_BODY.search(r.text) or _CSRF_INPUT.search(r.text)
        self.csrf = m.group(1) if m else ""

    def get(self, url: str, **kw):
        return self.c.get(url, **kw)

    def post(self, url: str, data: dict | None = None, csrf: bool = True, **kw):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf_token", self.csrf)
        return self.c.post(url, data=data, **kw)

    def post_json(self, url: str, payload: dict, csrf: bool = True):
        headers = {"Accept": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.c.post(url, json=payload, headers=headers)


@pytest.fixture
def browser():
    return Browser()


def as_user(email: str) -> Browser:
    b = Browser()
    b.login(email)
    return b


def make_token(org_id: int, scopes: str, subject: str = "integration", branch_id=None,
               active: bool = True, expires_at=None) -> str:
    raw = "sto_test_" + os.urandom(8).hex()
    with Session(admin_engine) as s:
        s.add(ApiToken(org_id=org_id, token_hash=sha256_hex(raw), scopes=scopes, subject_type=subject,
                       branch_id=branch_id, active=active, expires_at=expires_at, label="t"))
        s.commit()
    return raw


def make_device(org_id: int, active: bool = True) -> str:
    raw = "dev_test_" + os.urandom(8).hex()
    with Session(admin_engine) as s:
        s.add(SyncDevice(org_id=org_id, label="edge", token_hash=sha256_hex(raw), active=active))
        s.commit()
    return raw


def admin_session() -> Session:
    return Session(admin_engine)
