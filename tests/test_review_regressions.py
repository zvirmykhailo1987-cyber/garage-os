"""Regression tests for the findings of the independent code review."""
import asyncio

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.main import app
from app.models import (
    ApiToken,
    Branch,
    Client,
    Organization,
    Part,
    PartLine,
    StatusCount,
    WorkLine,
    WorkOrder,
)
from tests.conftest import admin_session, as_user, make_device, make_token
from tests.test_sync import _edge, _raw_sync, _sync


def _suspend(org_id):
    with admin_session() as s:
        org = s.get(Organization, org_id)
        org.status = "suspended"
        s.add(org)
        s.commit()


# ── sync ──────────────────────────────────────────────────────────────────────
def test_partial_sync_row_does_not_wipe_relations(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        uid, ver = o.uid, o.version
    r = _raw_sync(tok, {"changes": {"orders": [{"uid": uid, "base_version": ver, "box": "7"}]}})
    assert r.status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        assert o.box == "7" and o.branch_id == world["a1"]
        assert o.client_id == world["clientA"] and o.car_id == world["carA"]


def test_sync_insert_without_branch_or_with_unknown_parent_is_rejected(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        branch_uid = s.get(Branch, world["a1"]).uid
    no_branch = {"uid": "a" * 32, "name": "orphan", "base_version": 0}
    assert _raw_sync(tok, {"changes": {"clients": [no_branch]}}).status_code == 400
    bad_parent = {"uid": "b" * 32, "plate": "X", "base_version": 0, "branch_id__uid": branch_uid,
                  "client_id__uid": "c" * 32}
    assert _raw_sync(tok, {"changes": {"cars": [bad_parent]}}).status_code == 400
    with admin_session() as s:
        assert s.exec(select(Client).where(Client.name == "orphan")).first() is None


def test_malformed_sync_input_is_400_and_applies_nothing(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        c = s.get(Client, world["clientA"])
        branch_uid = s.get(Branch, world["a1"]).uid
        good = {"uid": c.uid, "name": "should not land", "base_version": c.version, "branch_id__uid": branch_uid}
    for since in (12345, "nope"):
        assert _raw_sync(tok, {"since": since, "changes": {"clients": [good]}}).status_code == 400
    for field, value in (("discount", "abc"), ("name", {"x": 1}), ("name", "x" * 500)):
        r = _raw_sync(tok, {"changes": {"clients": [{**good, field: value}]}})
        assert r.status_code == 400, (field, r.status_code)
    with admin_session() as s:
        assert s.get(Client, world["clientA"]).name == "Client A"


def test_edit_made_while_push_is_in_flight_is_not_lost(world):
    tok = make_device(world["orgA"])
    edge = _edge("inflight")
    assert _sync(edge, tok)[0]
    from app.sync import client as sync_client
    real = sync_client.dirty_rows

    def dirty_then_edit(s, org_id):
        out = real(s, org_id)                         # snapshot is taken …
        c = s.exec(select(Client).where(Client.name == "Client A (v1)")).one()
        c.name, c.version = "Client A (v2, typed during the request)", c.version + 1
        s.add(c)
        s.commit()                                    # … then the user keeps typing
        return out
    with Session(edge) as s:
        c = s.exec(select(Client).where(Client.name == "Client A")).one()
        c.name, c.version = "Client A (v1)", c.version + 1
        s.add(c)
        s.commit()
    sync_client.dirty_rows = dirty_then_edit
    try:
        assert _sync(edge, tok)[0]
    finally:
        sync_client.dirty_rows = real
    with Session(edge) as s:
        c = s.exec(select(Client).where(Client.name.like("Client A (v%"))).one()  # type: ignore[attr-defined]
        assert c.name == "Client A (v2, typed during the request)" and c.version != c.sync_base
    assert _sync(edge, tok)[0]                       # the newer edit reaches the hub next cycle
    with admin_session() as s:
        assert s.get(Client, world["clientA"]).name == "Client A (v2, typed during the request)"


def test_overlapping_pull_window_is_idempotent(world):
    tok = make_device(world["orgA"])
    edge = _edge("overlap")
    for _ in range(3):
        assert _sync(edge, tok)[0]
    with Session(edge) as s:
        from app.models import SyncConflict
        assert s.exec(select(func.count()).select_from(SyncConflict)).one() == 0


# ── plan / suspension ─────────────────────────────────────────────────────────
def test_suspended_org_cannot_write_through_api_or_sync_but_can_revoke(world):
    tok = make_token(world["orgA"], "orders:read,estimates:approve,telephony:write")
    dev = make_device(world["orgA"])
    _suspend(world["orgA"])
    c = TestClient(app)
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post(f"/api/v1/estimates/{world['numberA']}/approve", headers=h).status_code == 403
    assert c.post("/api/v1/webhooks/telephony", headers=h, json={"phone": "+380000"}).status_code == 403
    assert c.get(f"/api/v1/orders/{world['numberA']}/status", headers=h).status_code == 200   # reads ok
    with admin_session() as s:
        cl = s.get(Client, world["clientA"])
        row = {"uid": cl.uid, "name": "x", "base_version": cl.version}
    assert _raw_sync(dev, {"changes": {"clients": [row]}}).status_code == 403
    assert _raw_sync(dev, {"changes": {}}).status_code == 200                                # pull ok
    owner = as_user("owner@a.test")
    with admin_session() as s:
        tid = s.exec(select(ApiToken)).one().id
    assert owner.post(f"/settings/tokens/{tid}/revoke").status_code == 303
    assert c.get(f"/api/v1/orders/{world['numberA']}/status", headers=h).status_code == 401


def test_api_ai_respects_plan(world):
    with admin_session() as s:
        org = s.get(Organization, world["orgA"])
        org.plan_code = "single"
        s.add(org)
        s.commit()
    tok = make_token(world["orgA"], "ai:draft")
    r = TestClient(app).post("/api/v1/ai/work-order-draft", headers={"Authorization": f"Bearer {tok}"},
                             json={"text": "brakes"})
    assert r.status_code == 403


def test_bot_rate_limit_is_per_chat(world):
    bot = make_token(world["orgA"], "orders:read", subject="bot")
    c = TestClient(app)
    h = {"Authorization": f"Bearer {bot}"}
    codes = [c.get("/api/v1/orders/X/status?chat_id=1", headers=h).status_code for _ in range(22)]
    assert codes[-1] == 429
    assert c.get("/api/v1/orders/X/status?chat_id=2", headers=h).status_code == 404   # other chats unaffected


# ── money and locking ─────────────────────────────────────────────────────────
def test_mechanic_cannot_set_prices(world):
    b = as_user("mech@a.test")
    oid = world["orderA"]
    assert b.post(f"/orders/{oid}/part", {"name": "Bolt", "price": "0.01"}).status_code == 403
    assert b.post(f"/orders/{oid}/part", {"sku": "SKU-A", "price": "1"}).status_code == 403
    assert b.post(f"/orders/{oid}/part", {"sku": "SKU-A"}).status_code == 303               # stock price ok
    with admin_session() as s:
        assert [p.price for p in s.exec(select(PartLine).where(PartLine.order_id == oid,
                                                             PartLine.deleted == False))] == [1000, 100]  # noqa: E712


def test_paid_and_fiscalised_orders_are_frozen(world):
    oid = world["orderA"]
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        o.paid, o.status = True, "paid"
        s.add(o)
        s.commit()
    mech = as_user("mech@a.test")
    assert mech.post(f"/orders/{oid}/work/{world['wlA']}/delete").status_code == 403
    manager = as_user("manager@a.test")
    assert manager.post(f"/orders/{oid}/fiscal", {"confirm": "yes"}).status_code == 303
    for url, data in ((f"/orders/{oid}/work/{world['wlA']}/delete", {}),
                      (f"/orders/{oid}/part", {"name": "x"}), (f"/orders/{oid}/work", {"service": "x"})):
        assert manager.post(url, data).status_code == 409, url
    with admin_session() as s:
        version = s.get(WorkOrder, oid).version
    r = manager.post(f"/orders/{oid}/edit", {"expected_version": str(version), "discount_pct": "50"})
    assert r.status_code == 409
    with admin_session() as s:
        assert s.get(WorkOrder, oid).total == 1500 and not s.get(WorkLine, world["wlA"]).deleted


def test_order_prefix_must_be_unique_in_org(world):
    owner = as_user("owner@a.test")
    r = owner.post("/settings/branches", {"name": "Clone", "prefix": "AON"})
    assert r.headers["location"].endswith("err.prefix_taken")


# ── cross-branch reads inside one org ─────────────────────────────────────────
def test_branch_user_plate_lookup_and_client_history_stay_in_branch(world):
    desk = as_user("desk@a.test")                     # branch A1 only
    assert desk.get(f"/integrations/plate?plate={world['plateA2']}").json() == {"found": False}
    assert desk.get(f"/integrations/plate?plate={world['plateA']}").json()["found"] is True


# ── status counters ───────────────────────────────────────────────────────────
def test_status_counters_follow_every_change(world):
    b = as_user("owner@a.test")
    b.post("/branch", {"branch_id": str(world["a1"])})
    b.refresh_csrf()
    b.post("/orders/new", {"plate": "QQ1234QQ", "complaint": "noise"})
    b.post_json(f"/orders/{world['orderA']}/status", {"status": "done", "expected_version": 1})
    with admin_session() as s:
        real = dict(s.exec(select(WorkOrder.status, func.count()).where(WorkOrder.branch_id == world["a1"])
                           .group_by(WorkOrder.status)).all())
        cached = {c.status: c.n for c in s.exec(select(StatusCount).where(StatusCount.branch_id == world["a1"]))
                  if c.n}
    assert real == cached
    nav = b.get("/kanban").text
    assert f'<span class="cnt">{real.get("done", 0)}</span>' in nav


# ── Telegram bot ──────────────────────────────────────────────────────────────
def test_bot_encodes_user_input_in_paths():
    from tg_bot.bot import Api
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.raw_path.decode())
        return httpx.Response(404, json={})

    async def run():
        async with httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(handler)) as http:
            api = Api(http)
            await api.status("../../fleet/1/vehicles", 5)
            await api.approve("A/B?x=1", 5)
    asyncio.run(run())
    assert seen[0].startswith("/api/v1/orders/..%2F..%2Ffleet%2F1%2Fvehicl/status")   # encoded + capped at 20
    assert seen[1].startswith("/api/v1/estimates/A%2FB%3Fx%3D1/approve")


def test_parts_from_stock_keep_stock_price(world):
    with admin_session() as s:
        assert s.exec(select(Part).where(Part.sku == "SKU-A")).one().price == 100


def test_sync_ignores_raw_foreign_keys_pointing_at_another_tenant(world):
    """Security review: raw integer FKs in a push used to be written as-is, letting a device
    attach its own order to another tenant's client and car (rendered by the order list)."""
    tok = make_device(world["orgB"])
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderB"])
        uid, ver = o.uid, o.version
    r = _raw_sync(tok, {"changes": {"orders": [{"uid": uid, "base_version": ver, "client_id": world["clientA"],
                                                "car_id": world["carA"], "branch_id": world["a1"]}]}})
    assert r.status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderB"])
        assert (o.client_id, o.car_id, o.branch_id) == (world["clientB"], world["carB"], world["b1"])
    page = as_user("owner@b.test").get("/orders").text
    assert "Client A" not in page and world["plateA"] not in page


# ── second review round ───────────────────────────────────────────────────────
def test_edge_copies_number_orders_in_their_own_space(world, monkeypatch):
    from app import config
    from app.services.orders import next_number
    monkeypatch.setattr(config, "EDGE_TAG", "7A3F")
    with admin_session() as s:
        n = next_number(s, s.get(Branch, world["a1"]))
    assert n.startswith("AON-7A3F") and len(n) <= 20


def test_device_cannot_write_hub_owned_or_invalid_values(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        uid, ver = o.uid, o.version
    for bad in ({"status": "bogus"}, {"pay_method": "bitcoin"}, {"discount_pct": -900}):
        assert _raw_sync(tok, {"changes": {"orders": [{"uid": uid, "base_version": ver, **bad}]}}).status_code == 400
    r = _raw_sync(tok, {"changes": {"orders": [{"uid": uid, "base_version": ver, "total": 1.0,
                                                "fiscal_no": "FAKE", "number": "HACK-1"}]}})
    assert r.status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        assert (o.total, o.fiscal_no, o.number) == (1500.0, "", world["numberA"])
        assert s.exec(select(StatusCount).where(StatusCount.status == "bogus")).first() is None


def test_device_line_push_recomputes_total_and_fiscal_orders_are_frozen(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        wl = s.get(WorkLine, world["wlA"])
        line = {"uid": wl.uid, "base_version": wl.version, "norm_hours": 3}
    assert _raw_sync(tok, {"changes": {"work_lines": [line]}}).status_code == 200
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        assert o.total == 1500 + 1000                      # 3 h × 500 + parts 1000, computed by the hub
        o.fiscal_no = "FISC-1"
        s.add(o)
        s.commit()
        wl = s.get(WorkLine, world["wlA"])
        line = {"uid": wl.uid, "base_version": wl.version, "norm_hours": 0}
        order_row = {"uid": o.uid, "base_version": o.version, "paid": False}
    assert _raw_sync(tok, {"changes": {"work_lines": [line]}}).status_code == 409
    assert _raw_sync(tok, {"changes": {"orders": [order_row]}}).status_code == 409
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderA"]).total == 2500


def test_mechanic_cannot_move_order_out_of_paid_and_fiscal_status_is_frozen(world):
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        o.status, o.paid = "paid", True
        s.add(o)
        s.commit()
        v = s.get(WorkOrder, world["orderA"]).version
    mech = as_user("mech@a.test")
    assert mech.post_json(f"/orders/{world['orderA']}/status", {"status": "new", "expected_version": v}).status_code == 403
    assert mech.post(f"/orders/{world['orderA']}/edit", {"expected_version": str(v), "status": "new"}).status_code == 403
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        o.fiscal_no = "FISC-1"
        s.add(o)
        s.commit()
        v = o.version
    owner = as_user("owner@a.test")
    assert owner.post_json(f"/orders/{world['orderA']}/status", {"status": "new", "expected_version": v}).status_code == 409
    assert owner.post(f"/orders/{world['orderA']}/edit", {"expected_version": str(v), "status": "new"}).status_code == 409


def test_revenue_is_reported_by_payment_date(world):
    from datetime import timedelta

    from app.models import utcnow
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        o.status, o.paid, o.paid_at = "paid", True, utcnow() - timedelta(days=40)
        s.add(o)
        s.commit()
        v = o.version
    owner = as_user("owner@a.test")
    owner.post(f"/orders/{world['orderA']}/edit", {"expected_version": str(v), "box": "B9"})   # late edit
    page = owner.get("/").text
    assert "1 500.00" not in page.split("Paid, last 7 days")[1][:400]


def test_absurd_ids_are_404_not_500(world):
    b = as_user("owner@a.test")
    for url in ("/orders/99999999999999999999", "/clients/99999999999999999999", "/fleet/99999999999999999999"):
        assert b.get(url).status_code == 404, url
    tok = make_token(world["orgA"], "fleet:read")
    r = TestClient(app).get("/api/v1/fleet/99999999999999999999/vehicles", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code in (404, 422)


def test_branch_bound_bot_links_only_its_branch(world):
    import re
    code = re.search(r"/start ([A-Z0-9]{8})", as_user("owner@a.test").post(
        f"/clients/{world['clientA']}/telegram-code").text).group(1)
    bot = make_token(world["orgA"], "telegram:link", subject="bot", branch_id=world["a2"])
    r = TestClient(app).post("/api/v1/telegram/link", headers={"Authorization": f"Bearer {bot}"},
                             json={"code": code, "chat_id": "9"})
    assert r.status_code == 404


def test_rate_limiter_evicts_idle_keys_instead_of_resetting():
    from app.security import RateLimiter
    lim = RateLimiter(limit=1, window_s=60)
    assert lim.hit("victim") and not lim.hit("victim")
    for i in range(50_001):
        lim._hits[f"idle{i}"] = __import__("collections").deque()
    lim.hit("spray")
    assert not lim.hit("victim")                    # the victim's live bucket survived the spray


def test_sqlite_write_requests_lock_up_front_but_reads_stay_concurrent(tmp_path):
    """Second review: IMMEDIATE on every transaction blocked reads during a sync round trip.
    Now only write-intent transactions take the lock; readers run alongside (WAL)."""
    import pytest
    from sqlalchemy import text
    from sqlmodel import Session

    from app.db import make_engine, write_intent
    eng = make_engine(f"sqlite:///{tmp_path / 'lock.sqlite'}")
    with eng.connect() as c:
        c.execute(text("CREATE TABLE t (x int)"))
        c.commit()
    with Session(eng) as writer, Session(eng) as reader, Session(eng) as other_writer:
        write_intent(writer)
        writer.execute(text("SELECT 1"))                       # holds the write lock (no write yet)
        assert reader.execute(text("SELECT count(*) FROM t")).scalar() == 0   # reads are not blocked
        write_intent(other_writer)
        with pytest.raises(Exception, match="locked"):     # a second writer waits (busy_timeout), then fails
            other_writer.execute(text("SELECT 1"))
        writer.commit()
        writer.execute(text("SELECT 1"))                   # after a commit the intent still holds
        assert writer.connection().get_execution_options().get("sqlite_write_intent") is True



# ── verification round ────────────────────────────────────────────────────────
def _edge_session(edge):
    from app.db import set_tenant
    s = Session(edge)
    set_tenant(s, 1)
    return s


def test_sync_round_trip_does_not_lock_the_edge(world):
    from app.db import write_intent
    tok = make_device(world["orgA"])
    edge = _edge("nolock")
    inner = TestClient(app)._transport
    seen = []

    class Probe(httpx.BaseTransport):
        def handle_request(self, request):
            with Session(edge) as other:              # the edge's own web request during the push
                write_intent(other)
                other.add(Client(name="typed meanwhile", org_id=1, branch_id=None))
                other.commit()
                seen.append("written")
            return inner.handle_request(request)
    from app.sync.client import sync_once
    assert sync_once(Probe(), eng=edge, hub_url="http://testserver", token=tok)[0]
    assert seen == ["written"]


def test_refused_new_line_on_fiscalised_order_does_not_block_the_edge(world):
    tok = make_device(world["orgA"])
    edge = _edge("orphan")
    assert _sync(edge, tok)[0]
    with admin_session() as s:
        from app.tenancy import touch
        o = s.get(WorkOrder, world["orderA"])
        o.fiscal_no = "FISC-9"
        touch(o)                                   # as the fiscal route does
        s.add(o)
        s.commit()
    with _edge_session(edge) as s:
        o = s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one()
        s.add(WorkLine(order_id=o.id, service="late line", norm_hours=1, rate=1, org_id=1, branch_id=o.branch_id))
        c = s.exec(select(Client).where(Client.name == "Client A2")).one()
        c.name, c.version = "Client A2 (edge edit)", c.version + 1
        s.add(c)
        s.commit()
    assert _sync(edge, tok)[1] == "conflict"
    assert _sync(edge, tok) == (True, "ok")                  # the queue is free again
    with admin_session() as s:
        assert s.get(Client, world["clientA2"]).name == "Client A2 (edge edit)"
        assert s.exec(select(WorkLine).where(WorkLine.service == "late line")).first() is None
    with _edge_session(edge) as s:
        assert s.exec(select(WorkLine).where(WorkLine.service == "late line")).one().deleted
        assert s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one().fiscal_no == "FISC-9"


def test_device_cannot_move_lines_or_change_money_on_paid_orders(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        other = s.get(WorkOrder, world["orderA_2"])
        wl = s.get(WorkLine, world["wlA"])
        move = {"uid": wl.uid, "base_version": wl.version, "order_id__uid": other.uid}
        o = s.get(WorkOrder, world["orderA"])
        o.status, o.paid = "paid", True
        s.add(o)
        s.commit()
        money = {"uid": o.uid, "base_version": o.version, "discount_pct": 50}
        unpay = {"uid": o.uid, "base_version": o.version, "paid": False, "status": "new"}
    assert _raw_sync(tok, {"changes": {"orders": [money]}}).status_code == 409
    assert _raw_sync(tok, {"changes": {"orders": [unpay]}}).status_code == 409
    assert _raw_sync(tok, {"changes": {"work_lines": [move]}}).status_code == 409   # lines of a paid order
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderA"])
        assert (o.discount_pct, o.paid, o.status) == (0, True, "paid")
        assert s.get(WorkLine, world["wlA"]).order_id == world["orderA"]


def test_line_cannot_be_moved_to_another_order_by_a_device(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        other = s.get(WorkOrder, world["orderA_2"])
        wl = s.get(WorkLine, world["wlA"])
        move = {"uid": wl.uid, "base_version": wl.version, "order_id__uid": other.uid}
    assert _raw_sync(tok, {"changes": {"work_lines": [move]}}).status_code == 200
    with admin_session() as s:
        assert s.get(WorkLine, world["wlA"]).order_id == world["orderA"]
        assert s.get(WorkOrder, world["orderA"]).total == 1500


def test_device_cannot_take_hub_order_numbers(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        branch_uid = s.get(Branch, world["a1"]).uid
    rows = [{"uid": "e" * 32, "number": "AON-001003", "base_version": 0, "branch_id__uid": branch_uid},
            {"uid": "f" * 32, "number": "", "base_version": 0, "branch_id__uid": branch_uid}]
    assert _raw_sync(tok, {"changes": {"orders": rows}}).status_code == 200
    with admin_session() as s:
        nums = {o.number for o in s.exec(select(WorkOrder).where(WorkOrder.branch_id == world["a1"]))}
    assert len(nums) == 4 and "AON-001003" in nums                  # renumbered by the hub, all unique
    desk = as_user("desk@a.test")
    assert desk.post("/orders/new", {"plate": "ZZ0001ZZ", "complaint": "x"}).status_code == 303


def test_conflict_keeps_relations_on_the_edge(world):
    tok = make_device(world["orgA"])
    edge = _edge("parents")
    assert _sync(edge, tok)[0]
    with admin_session() as s:                                      # hub: order moves to a NEW client
        c = Client(name="Brand new client", org_id=world["orgA"], branch_id=world["a1"])
        s.add(c)
        s.flush()
        o = s.get(WorkOrder, world["orderA"])
        o.client_id, o.version = c.id, o.version + 1
        s.add(o)
        s.commit()
    with _edge_session(edge) as s:
        o = s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one()
        o.box, o.version = "EDGE", o.version + 1
        s.add(o)
        s.commit()
    assert _sync(edge, tok)[1] == "conflict"
    with _edge_session(edge) as s:
        o = s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one()
        client = s.get(Client, o.client_id) if o.client_id else None
        assert client is not None and client.name == "Brand new client"


def test_edge_keeps_pulling_while_push_is_refused(world):
    tok = make_device(world["orgA"])
    edge = _edge("suspended")
    assert _sync(edge, tok)[0]
    with _edge_session(edge) as s:
        c = s.exec(select(Client).where(Client.name == "Client A")).one()
        c.name, c.version = "pending", c.version + 1
        s.add(c)
        s.commit()
    _suspend(world["orgA"])
    with admin_session() as s:
        c2 = s.get(Client, world["clientA2"])
        c2.name, c2.version = "renamed on the hub", c2.version + 1
        from app.models import utcnow
        c2.srv_at = utcnow()
        s.add(c2)
        s.commit()
    assert _sync(edge, tok)[1] == "HTTP 403"
    with _edge_session(edge) as s:
        assert s.exec(select(Client).where(Client.name == "renamed on the hub")).first() is not None


# ── verification round 2 ──────────────────────────────────────────────────────
def test_renumbered_edge_order_gets_the_hub_number_back(world):
    """Two copies picked the same number: the hub renumbers the edge order and the edge
    adopts both the hub's order and the new number instead of failing on UNIQUE."""
    tok = make_device(world["orgA"])
    edge = _edge("renumber")
    assert _sync(edge, tok)[0]
    with _edge_session(edge) as s:
        branch = s.exec(select(Branch).where(Branch.name == "Alpha One")).one()
        s.add(WorkOrder(number="AON-009999", branch_id=branch.id, org_id=1, complaint="offline"))
        s.commit()
    with admin_session() as s:
        from app.tenancy import touch
        hub_order = WorkOrder(number="AON-009999", org_id=world["orgA"], branch_id=world["a1"], complaint="hub")
        touch(hub_order)
        s.add(hub_order)
        s.commit()
    for _ in range(3):
        _sync(edge, tok)
    assert _sync(edge, tok) == (True, "ok")
    with admin_session() as s:
        hub_copy = s.exec(select(WorkOrder).where(WorkOrder.complaint == "offline")).one()
        assert hub_copy.number != "AON-009999"
    with _edge_session(edge) as s:
        local = s.exec(select(WorkOrder).where(WorkOrder.complaint == "offline")).one()
        assert local.number == hub_copy.number
        assert s.exec(select(WorkOrder).where(WorkOrder.number == "AON-009999")).one().complaint == "hub"


def test_ten_digit_ids_are_404(world):
    b = as_user("owner@a.test")
    assert b.get("/orders/3000000000").status_code == 404


# ── second verification round ─────────────────────────────────────────────────
class _EditDuringPush(httpx.BaseTransport):
    """Delegates to the app, but edits an edge order while the push is 'in flight'."""

    def __init__(self, inner, edge, complaint):
        self.inner, self.edge, self.complaint, self.done = inner, edge, complaint, False

    def handle_request(self, request):
        if not self.done:
            self.done = True
            with _edge_session(self.edge) as s:
                o = s.exec(select(WorkOrder).where(WorkOrder.complaint == self.complaint)).one()
                o.box, o.version = "B9", o.version + 1          # what touch() does on the edge
                s.add(o)
                s.commit()
        return self.inner.handle_request(request)


def test_renumbered_order_edited_in_flight_keeps_the_edit_and_gets_the_number(world):
    from app.sync.client import sync_once
    tok = make_device(world["orgA"])
    edge = _edge("renum-inflight")
    assert _sync(edge, tok)[0]
    hub_number = "AON-009998"
    with _edge_session(edge) as s:
        branch = s.exec(select(Branch).where(Branch.name == "Alpha One")).one()
        s.add(WorkOrder(number=hub_number, branch_id=branch.id, org_id=1, complaint="inflight"))
        s.commit()
    with admin_session() as s:
        from app.tenancy import touch
        taken = WorkOrder(number=hub_number, org_id=world["orgA"], branch_id=world["a1"], complaint="hub2")
        touch(taken)
        s.add(taken)
        s.commit()
    tr = _EditDuringPush(TestClient(app)._transport, edge, "inflight")
    assert sync_once(tr, eng=edge, hub_url="http://testserver", token=tok)[0]
    for _ in range(2):
        assert _sync(edge, tok) == (True, "ok")
    with admin_session() as s:
        hub_copy = s.exec(select(WorkOrder).where(WorkOrder.complaint == "inflight")).one()
        assert hub_copy.number != hub_number and hub_copy.box == "B9"       # the edit reached the hub
    with _edge_session(edge) as s:
        local = s.exec(select(WorkOrder).where(WorkOrder.complaint == "inflight")).one()
        assert local.number == hub_copy.number and local.box == "B9"
        from app.models import SyncConflict
        assert s.exec(select(func.count()).select_from(SyncConflict)).one() == 0


def test_parked_numbers_are_renumbered_unique_and_do_not_fake_edits(world):
    from app.sync.engine import HUB_NUMBER, _free_local_number, _hub_number
    assert HUB_NUMBER.match("ЛЬВ-001050") and not HUB_NUMBER.match("KYI-1A2B00001")
    with admin_session() as s:
        data = {"number": "AON-000123-DUP001", "branch_id": world["a1"]}
        assert _hub_number(s, data) and not data["number"].endswith("DUP001")
        s.rollback()
    edge = _edge("parked")
    with _edge_session(edge) as s:
        s.add(Branch(name="E", prefix="E", org_id=1))
        s.commit()
        bid = s.exec(select(Branch)).one().id
        a = WorkOrder(number="E-000001", branch_id=bid, org_id=1, version=3, sync_base=3)
        b = WorkOrder(number="E-000001-DUP001", branch_id=bid, org_id=1)
        s.add(a)
        s.add(b)
        s.commit()
        _free_local_number(s, {"number": "E-000001", "branch_id": bid}, "other-uid")
        s.commit()
        s.refresh(a)
        assert a.number == "E-000001-DUP002" and a.version == a.sync_base == 3


def test_oversized_and_anonymous_writes_are_refused_early(world):
    c = TestClient(app, follow_redirects=False)
    big = c.post("/orders/new", content=b"x" * 10, headers={"content-length": str(50_000_000),
                                                             "content-type": "application/json"})
    assert big.status_code == 413
    streamed = c.post("/orders/new", content=iter([b"a=" + b"x" * 3_000_000, b"x" * 3_000_000]),
                      headers={"content-type": "application/x-www-form-urlencoded"})
    assert streamed.status_code in (400, 413)          # FastAPI turns a failed form parse into 400
    anon = c.post("/orders/new", data={"plate": "AA0000AA"})
    assert anon.status_code in (303, 401) and "/login" in anon.headers.get("location", "/login")


def test_out_of_range_ids_in_forms_are_400_and_negative_paths_404(world):
    b = as_user("owner@a.test")
    assert b.get("/orders/-99999999999999999999").status_code == 404
    r = b.post("/orders/new", {"plate": "AA1111AA", "master_id": "99999999999999999999",
                               "branch_id": str(world["a1"])})
    assert r.status_code in (400, 303)


def test_old_protocol_devices_still_receive_the_hub_number(world):
    """A device without `protocol: 2` ignores `renumbered`; the hub stores its renumbered
    order one version ahead so the next pull carries the number back."""
    tok = make_device(world["orgA"])
    with admin_session() as s:
        taken = s.get(WorkOrder, world["orderA"]).number
        branch_uid = s.get(Branch, world["a1"]).uid
    row = {"uid": "legacy-order-uid-0001", "base_version": 0, "number": taken,
           "branch_id__uid": branch_uid, "complaint": "legacy"}
    r = _raw_sync(tok, {"changes": {"orders": [row]}})
    assert r.status_code == 200, r.text
    assert "legacy-order-uid-0001" in r.json()["renumbered"]
    with admin_session() as s:
        hub = s.exec(select(WorkOrder).where(WorkOrder.uid == "legacy-order-uid-0001")).one()
        assert hub.version == 2 and hub.number != taken


def test_revoked_session_is_refused_before_the_write_lock(world):
    from app.models import UserSession
    from app.models.base import utcnow
    b = as_user("owner@a.test")
    with admin_session() as s:
        for us in s.exec(select(UserSession)).all():
            us.revoked_at = utcnow()
            s.add(us)
        s.commit()
    r = b.post("/orders/new", {"plate": "AA2222AA"})
    assert r.status_code == 303 and "/login" in r.headers["location"]
