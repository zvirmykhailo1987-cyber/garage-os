# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Public demo (DEMO_MODE=1): the shared demo accounts cannot be taken over by a visitor.

Every visitor knows the same demo password, so nobody may deactivate a demo account, suspend
a demo organization, lock a demo account out or grab a demo e-mail. Outside the demo the same
actions keep working exactly as before.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlmodel import select

from app import config
from app.demo_guard import DEMO_LOCKED, is_protected
from app.models import Organization, User, utcnow
from app.web.deps import templates
from tests.conftest import PASSWORD, Browser, admin_session, as_user

DEMO_DESK = "desk.lviv@northwind.test"
DEMO_OWNER_B = "owner@riverside.test"
MSG_EN = "Demo accounts are shared and cannot be changed in the demo"
MSG_UK = "Демо-акаунти спільні"
BANNER_EN = "resets every night at 04:00 CET"
BANNER_UK = "скидаються щоночі о 04:00 CET"


def _rename(uid: int, email: str) -> None:
    with admin_session() as s:
        u = s.get(User, uid)
        assert u is not None
        u.email = email
        s.add(u)
        s.commit()


@pytest.fixture
def demo(world, monkeypatch):
    """Demo mode on; org A's receptionist and org B's owner carry published demo e-mails."""
    from app.demo import DEMO_ACCOUNTS, DEMO_PASSWORD
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setitem(templates.env.globals, "DEMO_MODE", True)
    monkeypatch.setitem(templates.env.globals, "demo_accounts", DEMO_ACCOUNTS)
    monkeypatch.setitem(templates.env.globals, "demo_password", DEMO_PASSWORD)
    _rename(world["desk"], DEMO_DESK)
    _rename(world["ownerB"], DEMO_OWNER_B)
    return world


def _active(uid: int) -> bool:
    with admin_session() as s:
        u = s.get(User, uid)
        assert u is not None
        return u.active


def _status(oid: int) -> str:
    with admin_session() as s:
        o = s.get(Organization, oid)
        assert o is not None
        return o.status


# ── the guard itself ──────────────────────────────────────────────────────────
def test_only_published_demo_accounts_are_protected_and_only_in_demo_mode(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert is_protected(DEMO_DESK) and is_protected("  Owner@Northwind.TEST ")
    assert not is_protected("someone@else.test") and not is_protected(None) and not is_protected("")
    monkeypatch.setattr(config, "DEMO_MODE", False)
    assert not is_protected(DEMO_DESK)


# ── deactivation ──────────────────────────────────────────────────────────────
def test_demo_account_cannot_be_deactivated(demo):
    r = as_user("owner@a.test").post(f"/settings/users/{demo['desk']}/toggle")
    assert r.status_code == 403 and MSG_EN in r.text
    assert _active(demo["desk"])
    assert Browser().login(DEMO_DESK).status_code == 303                # still usable


def test_demo_account_deactivation_is_refused_as_json_for_api_clients(demo):
    owner = as_user("owner@a.test")
    r = owner.post_json(f"/settings/users/{demo['desk']}/toggle", {})
    assert r.status_code == 403 and r.json() == {"detail": DEMO_LOCKED}
    assert _active(demo["desk"])


def test_demo_refusal_is_translated_to_ukrainian(demo):
    owner = as_user("owner@a.test")
    owner.c.cookies.set("sto_lang", "uk")
    r = owner.post(f"/settings/users/{demo['desk']}/toggle")
    assert r.status_code == 403 and MSG_UK in r.text


def test_a_visitor_created_user_can_still_be_toggled_in_demo(demo):
    r = as_user("owner@a.test").post(f"/settings/users/{demo['mech']}/toggle")
    assert r.status_code == 303 and not _active(demo["mech"])


# ── operator console ──────────────────────────────────────────────────────────
def test_demo_organization_cannot_be_suspended_by_the_operator(demo):
    op = as_user("op@platform.test")
    r = op.post(f"/superadmin/{demo['orgB']}", {"plan_code": "single", "status": "suspended"})
    assert r.status_code == 403 and MSG_EN in r.text
    assert _status(demo["orgB"]) == "active"


def test_operator_can_still_manage_organizations_without_demo_accounts(demo):
    _rename(demo["ownerB"], "visitor@b.test")
    op = as_user("op@platform.test")
    r = op.post(f"/superadmin/{demo['orgB']}", {"plan_code": "single", "status": "suspended"})
    assert r.status_code == 303 and _status(demo["orgB"]) == "suspended"


# ── lockout ───────────────────────────────────────────────────────────────────
def test_wrong_passwords_do_not_lock_a_demo_account(demo):
    b = Browser()
    for _ in range(config.LOGIN_MAX_FAILURES + 1):
        assert b.login(DEMO_DESK, "nope-nope-nope", expect_ok=False).status_code == 401
    assert b.login(DEMO_DESK).status_code == 303
    with admin_session() as s:
        u = s.exec(select(User).where(User.email == DEMO_DESK)).one()
        assert u.locked_until is None and not u.failed_logins


def test_demo_login_is_still_rate_limited_per_ip(demo):
    b = Browser()
    codes = [b.login(DEMO_DESK, "nope-nope-nope", expect_ok=False).status_code for _ in range(12)]
    assert codes[-1] == 429


def test_lockout_expires_on_its_own(world):
    b = Browser()
    for _ in range(config.LOGIN_MAX_FAILURES):
        b.login("owner@a.test", "nope-nope-nope", expect_ok=False)
    assert b.login("owner@a.test", PASSWORD, expect_ok=False).status_code == 401
    with admin_session() as s:
        u = s.exec(select(User).where(User.email == "owner@a.test")).one()
        assert u.locked_until is not None
        assert u.locked_until <= utcnow() + timedelta(minutes=config.LOGIN_LOCKOUT_MINUTES)
        u.locked_until = utcnow() - timedelta(seconds=1)                 # the lockout window passed
        s.add(u)
        s.commit()
    assert b.login("owner@a.test").status_code == 303


# ── e-mail takeover ───────────────────────────────────────────────────────────
def _csrf(page_text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert m
    return m.group(1)


def test_signup_cannot_claim_a_demo_email(demo):
    b = Browser()
    data = {"org_name": "Takeover Ltd", "admin_name": "Eve", "email": DEMO_DESK,
            "password": PASSWORD, "branch_name": "Main",
            "csrf_token": _csrf(b.get("/signup").text)}
    assert b.c.post("/signup", data=data).status_code == 400
    assert Browser().login(DEMO_DESK).status_code == 303


def test_owner_cannot_add_a_second_user_with_a_demo_email(demo):
    r = as_user("owner@a.test").post("/settings/users", {"name": "Eve", "email": DEMO_OWNER_B,
                                                         "password": PASSWORD,
                                                         "role": "net_admin"})
    assert r.headers["location"].endswith("err.email_taken")


# ── banner ────────────────────────────────────────────────────────────────────
def test_demo_banner_says_data_resets_nightly(demo):
    login = Browser().get("/login").text
    assert BANNER_EN in login and DEMO_DESK in login
    owner = as_user("owner@a.test")
    assert BANNER_EN in owner.get("/orders").text
    owner.c.cookies.set("sto_lang", "uk")
    assert BANNER_UK in owner.get("/orders").text
    assert BANNER_EN in as_user("op@platform.test").get("/superadmin").text


# ── production behaviour is unchanged ─────────────────────────────────────────
def test_outside_demo_mode_the_same_accounts_are_not_special(world):
    _rename(world["desk"], DEMO_DESK)
    _rename(world["ownerB"], DEMO_OWNER_B)
    owner = as_user("owner@a.test")
    assert BANNER_EN not in owner.get("/orders").text
    assert owner.post(f"/settings/users/{world['desk']}/toggle").status_code == 303
    assert not _active(world["desk"])
    op = as_user("op@platform.test")
    assert op.post(f"/superadmin/{world['orgB']}",
                   {"plan_code": "single", "status": "suspended"}).status_code == 303
    b = Browser()
    for _ in range(config.LOGIN_MAX_FAILURES):
        b.login(DEMO_OWNER_B, "nope-nope-nope", expect_ok=False)
    assert b.login(DEMO_OWNER_B, PASSWORD, expect_ok=False).status_code == 401   # locked as usual
