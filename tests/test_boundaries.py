# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Decision branches on the trust boundaries that the scenario tests did not take.

Found by turning on branch coverage: each test here exercises the *other* side of an ``if``
that guards tenancy, money, sync input, a token, a role or a session.
"""
import html
import re
from datetime import timedelta

import pytest
from sqlmodel import func, select

from ai.runtime import llm_client
from ai.runtime import runner as ai_runner
from ai.runtime.runner import run_skill
from app.i18n import translator
from app.models import (
    ROLE_NET_ADMIN,
    ROLE_RECEPTIONIST,
    ApiToken,
    Branch,
    Client,
    Membership,
    Organization,
    PartLine,
    Plan,
    Service,
    SyncDevice,
    User,
    UserSession,
    WorkOrder,
    utcnow,
)
from app.security import api_limiter, hash_password
from app.tenancy import can_access, find_order, load_user_context, scoped
from tests.conftest import PASSWORD, Browser, admin_session, as_user, make_device, make_token
from tests.test_review_regressions import _suspend
from tests.test_sync import _raw_sync


# ── tenancy ───────────────────────────────────────────────────────────────────
def test_membership_in_a_closed_or_foreign_branch_grants_nothing(world):
    with admin_session() as s:
        closed = Branch(org_id=world["orgA"], name="Closed", order_prefix="CLO", active=False)
        ghost = User(org_id=world["orgA"], email="ghost@a.test", name="ghost", password_hash=hash_password(PASSWORD))
        s.add(closed)
        s.add(ghost)
        s.flush()
        s.add(Membership(org_id=world["orgA"], user_id=ghost.id, role=ROLE_RECEPTIONIST, branch_id=closed.id))
        # a forged membership row that names another tenant's branch
        s.add(Membership(org_id=world["orgA"], user_id=ghost.id, role=ROLE_RECEPTIONIST, branch_id=world["b1"]))
        s.commit()
        ctx = load_user_context(s, ghost)
        assert ctx.branch_ids == [] and ctx.roles == {} and not ctx.is_net_admin
        assert s.exec(scoped(WorkOrder, ctx)).all() == []                 # no branch → no rows at all
        assert not ctx.can("orders.view")
    b = as_user("ghost@a.test")
    assert b.get("/orders").status_code == 403
    assert b.get(f"/orders/{world['orderB']}").status_code == 403


def test_shared_rows_are_visible_only_when_asked_for_and_only_in_own_org(world):
    with admin_session() as s:
        owner = load_user_context(s, s.get(User, world["owner"]))
        desk = load_user_context(s, s.get(User, world["desk"]))
        own = s.exec(select(Service).where(Service.name == "Alpha shared service")).one()
        other = s.exec(select(Service).where(Service.name == "Beta secret service")).one()
        assert not can_access(owner, own) and can_access(owner, own, include_shared=True)
        assert not can_access(owner, other, include_shared=True)
        assert owner.role_in(None) == ROLE_NET_ADMIN and desk.role_in(None) is None
        assert find_order(s, world["numberA"], None) is None             # no tenant → no lookup


def test_unknown_permission_name_fails_loudly():
    from app.web.deps import require
    with pytest.raises(ValueError, match="unknown permission"):
        require("orders.delete_everything")


# ── money ─────────────────────────────────────────────────────────────────────
def test_board_move_into_and_out_of_paid_sets_and_clears_the_payment(world):
    b = as_user("manager@a.test")
    oid = world["orderA"]
    r = b.post_json(f"/orders/{oid}/status", {"status": "paid", "expected_version": 1})
    assert r.status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        assert o.paid and o.paid_at is not None
    r = b.post_json(f"/orders/{oid}/status", {"status": "done", "expected_version": 2})
    assert r.status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        assert not o.paid and o.paid_at is None


def test_board_rejects_bad_json_and_unknown_status(world):
    b = as_user("manager@a.test")
    url = f"/orders/{world['orderA']}/status"
    headers = {"X-CSRF-Token": b.csrf, "Accept": "application/json"}
    assert b.c.post(url, content=b"not json", headers={**headers, "Content-Type": "application/json"}).status_code == 400
    assert b.c.post(url, json=[1, 2], headers=headers).status_code == 400
    assert b.post_json(url, {"status": "hacked", "expected_version": 1}).status_code == 400
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderA"]).status == "estimate"


def test_unknown_pay_method_is_rejected(world):
    b = as_user("manager@a.test")
    r = b.post(f"/orders/{world['orderA']}/edit", {"expected_version": "1", "pay_method": "barter"})
    assert r.status_code == 400
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderA"]).pay_method == "none"


def test_part_line_delete_checks_the_parent_order_and_recomputes_the_total(world):
    b = as_user("manager@a.test")
    pl = world["plA"]
    assert b.post(f"/orders/{world['orderA_2']}/part/{pl}/delete").status_code == 404     # IDOR
    with admin_session() as s:
        assert not s.get(PartLine, pl).deleted
    assert b.post(f"/orders/{world['orderA']}/part/{pl}/delete").status_code == 303
    assert b.post(f"/orders/{world['orderA']}/part/{pl}/delete").status_code == 404       # already gone
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        assert s.get(PartLine, pl).deleted
        assert (o.parts_total, o.works_total, o.total, o.vat_total) == (0, 500, 500, 83.33)


def test_llm_answer_with_prices_is_rejected_for_the_offline_extractor(monkeypatch):
    """Money is never taken from the model: a draft that carries a price fails the schema."""
    priced = {"vehicle": {"plate": None, "make_model": "Kia"}, "complaint": "pads", "works": [],
              "parts": [{"name": "Pads", "qty": 1, "price": 1}], "confidence": 0.9, "needs_clarification": []}
    monkeypatch.setattr(ai_runner.config, "LLM_PROVIDER", "selfhost")
    monkeypatch.setattr(ai_runner, "complete", lambda s, u: priced)
    res = run_skill("work-order-draft", "brake pads")
    assert res["fallback"] is True
    assert all("price" not in p for p in res["draft"]["parts"])


class _Reply:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_llm_client_refuses_non_json_and_non_object_answers(monkeypatch):
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, headers=headers)
        return _Reply(answers.pop(0))
    answers = ["not json", "[1, 2]", '{"ok": true}']
    monkeypatch.setattr(llm_client.config, "LLM_PROVIDER", "selfhost")
    monkeypatch.setattr(llm_client.config, "LLM_API_KEY", "k-test")
    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    with pytest.raises(ValueError, match="non-JSON"):
        llm_client.complete("s", "u")
    with pytest.raises(ValueError, match="non-object"):
        llm_client.complete("s", "u")
    assert llm_client.complete("s", "u") == {"ok": True}
    assert sent["url"].endswith("/chat/completions") and sent["headers"]["Authorization"] == "Bearer k-test"


# ── sync input ────────────────────────────────────────────────────────────────
@pytest.fixture
def client_row(world):
    with admin_session() as s:
        c = s.get(Client, world["clientA"])
        return {"uid": c.uid, "name": "should not land", "base_version": c.version,
                "branch_id__uid": s.get(Branch, world["a1"]).uid}


@pytest.mark.parametrize("changes", [
    {"users": [{"uid": "x"}]},                                     # not a synced table
    {"branches": [{"uid": "x" * 10, "name": "mine now"}]},          # pull-only
    {"clients": [{"name": "no uid"}]},
    {"clients": [{"uid": "u" * 33, "name": "uid too long"}]},
    "row:base_version",
    "row:parent_uid_type",
    "row:kind",
])
def test_malformed_sync_rows_are_400_and_apply_nothing(world, client_row, changes):
    tok = make_device(world["orgA"])
    if isinstance(changes, str):
        field = changes.split(":", 1)[1]
        row = {**client_row, **{"base_version": {"base_version": "1"}, "parent_uid_type": {"branch_id__uid": 5},
                                "kind": {"kind": "alien"}}[field]}
        changes = {"clients": [row]}
    assert _raw_sync(tok, {"changes": changes}).status_code == 400
    with admin_session() as s:
        assert s.get(Client, world["clientA"]).name == "Client A"
        assert s.exec(select(func.count()).select_from(Branch)).one() == 3


def test_sync_body_must_be_a_bounded_json_object(world, client_row, monkeypatch):
    from app.sync import engine as sync_engine
    from app.sync import routes as sync_routes
    tok = make_device(world["orgA"])
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    c = Browser().c
    assert c.post("/sync", content=b"{not json", headers=headers).status_code == 400
    assert c.post("/sync", content=b"[1]", headers=headers).status_code == 400
    monkeypatch.setattr(sync_engine, "MAX_ROWS_PER_PUSH", 1)
    assert _raw_sync(tok, {"changes": {"clients": [client_row, {**client_row, "uid": "z" * 32}]}}).status_code == 400
    monkeypatch.setattr(sync_routes, "MAX_BODY", 50)
    assert _raw_sync(tok, {"changes": {"clients": [client_row]}}).status_code == 413
    with admin_session() as s:
        assert s.get(Client, world["clientA"]).name == "Client A"


def test_device_and_api_tokens_are_rate_limited(world, monkeypatch):
    monkeypatch.setattr(api_limiter, "limit", 2)
    dev = make_device(world["orgA"])
    assert [_raw_sync(dev, {}).status_code for _ in range(3)] == [200, 200, 429]
    tok = make_token(world["orgA"], "orders:read")
    c = Browser().c
    url = f"/api/v1/orders/{world['numberA']}/status"
    codes = [c.get(url, headers={"Authorization": f"Bearer {tok}"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ── plans and permissions ─────────────────────────────────────────────────────
def test_suspended_org_cannot_mint_api_tokens_or_sync_devices(world):
    _suspend(world["orgA"])
    owner = as_user("owner@a.test")
    assert owner.post("/settings/tokens", {"label": "bot", "kind": "bot", "scopes": "orders:read"}).status_code == 403
    assert owner.post("/settings/devices", {"label": "edge"}).status_code == 403
    with admin_session() as s:
        assert s.exec(select(func.count()).select_from(ApiToken)).one() == 0
        assert s.exec(select(func.count()).select_from(SyncDevice)).one() == 0


def test_plan_branch_limit(world):
    with admin_session() as s:
        plan = s.exec(select(Plan).where(Plan.code == "network")).one()
        plan.max_branches = 2                                   # org A already has 2 branches
        s.add(plan)
        s.commit()
    owner = as_user("owner@a.test")
    r = owner.post("/settings/branches", {"name": "Third", "prefix": "THR"})
    assert r.headers["location"].endswith("err.plan_branches")
    with admin_session() as s:
        assert s.exec(select(Branch).where(Branch.order_prefix == "THR")).first() is None


# ── authentication ────────────────────────────────────────────────────────────
def test_signup_validates_every_field_and_is_rate_limited(world):
    tr = translator("en")
    b = Browser()
    token = re.search(r'name="csrf_token" value="([^"]+)"', b.get("/signup").text).group(1)
    good = {"org_name": "New Garage", "admin_name": "Nina", "email": "nina@new.test",
            "password": "long-enough-password", "branch_name": "Main", "csrf_token": token}
    for bad, error in (({"org_name": " "}, "err.signup_required"), ({"email": "nina@"}, "err.email_invalid"),
                       ({"password": "p" * 257}, "err.password_long"), ({"email": "owner@a.test"}, "err.email_taken")):
        r = b.c.post("/signup", data={**good, **bad})
        assert r.status_code == 400 and html.escape(tr(error)) in r.text, bad
    assert b.c.post("/signup", data=good).status_code == 303                 # 5th sign-up from this IP
    again = Browser()
    token = re.search(r'name="csrf_token" value="([^"]+)"', again.get("/signup").text).group(1)
    r = again.c.post("/signup", data={**good, "email": "x@new.test", "csrf_token": token})
    assert r.status_code == 429
    with admin_session() as s:
        assert s.exec(select(func.count()).select_from(Organization)).one() == 3


def test_account_without_a_password_cannot_sign_in(world):
    with admin_session() as s:
        u = s.get(User, world["desk"])
        u.password_hash = ""
        s.add(u)
        s.commit()
    r = Browser().login("desk@a.test", PASSWORD, expect_ok=False)
    assert r.status_code == 401 and "sto_session" not in r.headers.get("set-cookie", "")


def test_activity_slides_the_idle_window(world):
    from app import config
    b = as_user("owner@a.test")
    with admin_session() as s:
        sess = s.exec(select(UserSession)).one()
        sess.last_seen_at = utcnow() - timedelta(minutes=config.SESSION_IDLE_MINUTES - 5)
        s.add(sess)
        s.commit()
    assert b.get("/").status_code == 200
    with admin_session() as s:
        sess = s.exec(select(UserSession)).one()
        assert utcnow() - sess.last_seen_at < timedelta(minutes=1)          # active user is not logged out
