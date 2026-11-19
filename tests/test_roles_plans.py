"""Roles and subscription plans are enforced on the route, not only hidden in templates."""
from sqlmodel import select

from app.models import Master, Organization, Plan, User, WorkOrder
from tests.conftest import admin_session, as_user


def test_mechanic_can_work_the_order_but_not_money(world):
    b = as_user("mech@a.test")
    oid = world["orderA"]
    assert b.post(f"/orders/{oid}/work", {"service": "Diagnostics", "norm_hours": "0.5"}).status_code == 303
    assert b.post(f"/orders/{oid}/edit", {"expected_version": "2", "status": "in_progress"}).status_code == 303
    assert b.post(f"/orders/{oid}/edit", {"expected_version": "3", "discount_pct": "50"}).status_code == 403
    assert b.post(f"/orders/{oid}/edit", {"expected_version": "3", "status": "paid"}).status_code == 403
    assert b.post(f"/orders/{oid}/fiscal", {"confirm": "yes"}).status_code == 403
    assert b.post(f"/orders/{oid}/notify", {"confirm": "yes"}).status_code == 403
    assert b.post("/orders/new", {"plate": "AA0000AA", "complaint": "x"}).status_code == 403
    assert b.get("/clients").status_code == 403
    page = b.get(f"/orders/{oid}").text
    assert 'name="discount_pct"' not in page and "/fiscal" not in page
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        assert o.status == "in_progress" and o.discount_pct == 0 and not o.fiscal_no


def test_receptionist_cannot_fiscalise_or_edit_catalog(world):
    b = as_user("desk@a.test")
    assert b.post(f"/orders/{world['orderA']}/fiscal", {"confirm": "yes"}).status_code == 403
    assert b.post("/settings/master", {"name": "Ghost", "rate": "1"}).status_code == 403
    assert b.post("/parts/new", {"sku": "Z", "name": "Z"}).status_code == 403
    assert b.post(f"/orders/{world['orderA']}/notify", {"confirm": "yes", "channel": "sms"}).status_code == 303


def test_branch_manager_cannot_manage_team_tokens_or_migration(world):
    b = as_user("manager@a.test")
    assert b.post("/settings/users", {"name": "x", "email": "x@a.test", "password": "0123456789",
                                      "role": "net_admin"}).status_code == 403
    assert b.post("/settings/tokens", {"label": "t", "scopes": "orders:read"}).status_code == 403
    assert b.get("/migration").status_code == 403
    assert b.post("/settings/master", {"name": "New mech", "rate": "400"}).status_code == 303
    assert b.post(f"/orders/{world['orderA']}/fiscal", {"confirm": "yes"}).status_code == 303


def test_tier3_actions_need_explicit_confirmation(world):
    b = as_user("manager@a.test")
    assert b.post(f"/orders/{world['orderA']}/fiscal", {}).status_code == 428
    assert b.post(f"/orders/{world['orderA']}/fiscal", {"confirm": "yes"}).status_code == 303
    r = b.post(f"/orders/{world['orderA']}/fiscal", {"confirm": "yes"})      # no second receipt
    assert r.headers["location"].endswith("err.already_fiscal")


def test_form_cannot_assign_mechanic_from_other_branch(world):
    b = as_user("owner@a.test")
    r = b.post(f"/orders/{world['orderA']}/work", {"service": "x", "master_id": str(world["masterB"])})
    assert r.status_code == 400
    r = b.post(f"/orders/{world['orderA']}/edit", {"expected_version": "1", "master_id": str(world["masterA2"])})
    assert r.status_code == 400


def test_invalid_status_is_rejected(world):
    b = as_user("owner@a.test")
    assert b.post(f"/orders/{world['orderA']}/edit", {"expected_version": "1", "status": "hacked"}).status_code == 400


def test_optimistic_lock_on_form_and_board(world):
    b = as_user("owner@a.test")
    oid = world["orderA"]
    assert b.post(f"/orders/{oid}/edit", {"expected_version": "1", "box": "B1"}).status_code == 303
    r = b.post(f"/orders/{oid}/edit", {"expected_version": "1", "box": "B2"})     # stale
    assert r.headers["location"].endswith("conflict=1")
    r = b.post_json(f"/orders/{oid}/status", {"status": "done", "expected_version": 1})
    assert r.status_code == 409 and r.json()["version"] == 2
    r = b.post_json(f"/orders/{oid}/status", {"status": "done", "expected_version": 2})
    assert r.status_code == 200 and r.json() == {"ok": True, "version": 3}


def test_suspended_subscription_is_read_only(world):
    with admin_session() as s:
        org = s.get(Organization, world["orgA"])
        org.status = "suspended"
        s.add(org)
        s.commit()
    b = as_user("owner@a.test")
    page = b.get("/")
    assert page.status_code == 200 and "read-only" in page.text
    assert b.post("/orders/new", {"plate": "AA1111AA", "complaint": "x"}).status_code == 403
    assert b.post(f"/orders/{world['orderA']}/work", {"service": "x"}).status_code == 403
    assert b.post_json(f"/orders/{world['orderA']}/status", {"status": "done", "expected_version": 1}).status_code == 403


def test_plan_without_ai_blocks_ai_routes(world):
    with admin_session() as s:
        org = s.get(Organization, world["orgA"])
        org.plan_code = "single"
        s.add(org)
        s.commit()
    b = as_user("owner@a.test")
    assert b.post("/ai/work-order-draft", {"text": "brakes"}).status_code == 403
    assert b.get("/orders/search?q=ready").status_code == 403


def test_plan_user_limit(world):
    with admin_session() as s:
        plan = s.exec(select(Plan).where(Plan.code == "network")).one()
        plan.max_users = 6                                    # org A already has 5 users
        s.add(plan)
        s.commit()
    b = as_user("owner@a.test")
    ok = {"name": "New", "email": "new@a.test", "password": "test-pw-0123456789", "role": "master",
          "branch_id": str(world["a1"])}
    assert b.post("/settings/users", ok).headers["location"].endswith("msg.saved")
    ok["email"] = "new2@a.test"
    assert b.post("/settings/users", ok).headers["location"].endswith("err.plan_users")
    with admin_session() as s:
        assert s.exec(select(User).where(User.email == "new2@a.test")).first() is None


def test_owner_can_add_master_only_in_active_branch(world):
    b = as_user("owner@a.test")                              # "whole network" scope by default
    assert b.post("/settings/master", {"name": "Nobody"}).headers["location"].endswith("err.pick_branch")
    b.post("/branch", {"branch_id": str(world["a2"])})
    b.refresh_csrf()
    assert b.post("/settings/master", {"name": "Lviv mech"}).status_code == 303
    with admin_session() as s:
        m = s.exec(select(Master).where(Master.name == "Lviv mech")).one()
        assert m.branch_id == world["a2"] and m.org_id == world["orgA"]
