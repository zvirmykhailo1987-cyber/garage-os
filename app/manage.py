# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Operational commands.

    python -m app.manage init            schema + RLS policies (Postgres) + plans (+ demo data
                                         when DEMO_MODE=1). Idempotent.
    python -m app.manage create-operator create the platform operator from
                                         STO_OPERATOR_EMAIL / STO_OPERATOR_PASSWORD
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from . import config
from .db import admin_engine, create_schema
from .models import Plan, User

PLANS = [
    {"code": "trial", "title": "Trial", "price_month": 0, "max_branches": 2, "max_users": 5, "ai_enabled": True},
    {"code": "single", "title": "Single station", "price_month": 1490, "max_branches": 1, "max_users": 6,
     "ai_enabled": False},
    {"code": "network", "title": "Network", "price_month": 4900, "max_branches": 20, "max_users": 100,
     "ai_enabled": True},
]


def seed_plans(s: Session) -> None:
    for p in PLANS:
        if not s.exec(select(Plan).where(Plan.code == p["code"])).first():
            s.add(Plan.model_validate(p))
    s.commit()


def init() -> None:
    create_schema(admin_engine)
    if config.IS_POSTGRES:
        from . import rls
        rls.apply(admin_engine, config.DATABASE_URL)
    with Session(admin_engine) as s:
        seed_plans(s)
        if config.DEMO_MODE:
            from .demo import seed_demo
            seed_demo(s)
    # planner statistics: without them SQLite picks the low-selectivity org_id index over
    # client_id / order_id and a 200k-order page goes from ~0.1 s to ~0.5 s
    with admin_engine.begin() as conn:
        conn.execute(text("ANALYZE"))


def create_operator() -> None:
    from .security import check_password_policy, hash_password
    email = os.environ.get("STO_OPERATOR_EMAIL", "").strip().lower()
    password = os.environ.get("STO_OPERATOR_PASSWORD", "")
    if not email or check_password_policy(password):
        raise SystemExit("set STO_OPERATOR_EMAIL and STO_OPERATOR_PASSWORD (>= 10 chars)")
    with Session(admin_engine) as s:
        if s.exec(select(User).where(User.email == email)).first():
            raise SystemExit("user exists")
        s.add(User(email=email, name="Platform operator", is_superadmin=True,
                   password_hash=hash_password(password)))
        s.commit()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        init()
    elif cmd == "create-operator":
        create_operator()
    else:
        raise SystemExit(__doc__)
