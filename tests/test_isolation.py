"""Tenant isolation: every cross-tenant access answers 404; IDOR on order lines is closed."""
from sqlmodel import select

from app.models import PartLine, WorkLine, WorkOrder
from tests.conftest import Browser, admin_session, as_user, make_token


def test_cross_tenant_detail_pages_are_404(world):
    b = as_user("owner@a.test")
    for url in (f"/orders/{world['orderB']}", f"/orders/{world['orderB']}/invoice",
                f"/clients/{world['clientB']}", f"/fleet/{world['fleetB']}"):
        assert b.get(url).status_code == 404, url


def test_cross_tenant_writes_are_404_and_change_nothing(world):
    b = as_user("owner@a.test")
    oid = world["orderB"]
    posts = [(f"/orders/{oid}/edit", {"expected_version": "1", "status": "done"}),
             (f"/orders/{oid}/work", {"service": "x"}), (f"/orders/{oid}/part", {"name": "x"}),
             (f"/orders/{oid}/work/{world['wlB']}/delete", {}),
             (f"/orders/{oid}/part/{world['plB']}/delete", {}),
             (f"/orders/{oid}/notify", {"confirm": "yes"}), (f"/orders/{oid}/fiscal", {"confirm": "yes"}),
             (f"/clients/{world['clientB']}/telegram-code", {}), (f"/fleet/{world['fleetB']}/act", {"period": "2026-01"})]
    for url, data in posts:
        assert b.post(url, data).status_code == 404, url
    r = b.post_json(f"/orders/{oid}/status", {"status": "done", "expected_version": 1})
    assert r.status_code == 404
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        assert o.status == "estimate" and o.version == 1
        assert not s.get(WorkLine, world["wlB"]).deleted


def test_idor_delete_line_of_another_order_in_same_tenant(world):
    """Regression: order_del_work/part used to delete ANY line id under ANY own order id."""
    b = as_user("owner@a.test")
    other = world["orderA_2"]                                # own order, but the line is not on it
    assert b.post(f"/orders/{other}/work/{world['wlA']}/delete").status_code == 404
    assert b.post(f"/orders/{other}/part/{world['plA']}/delete").status_code == 404
    # cross-tenant line through own order
    assert b.post(f"/orders/{world['orderA']}/work/{world['wlB']}/delete").status_code == 404
    with admin_session() as s:
        assert not s.get(WorkLine, world["wlA"]).deleted
        assert not s.get(PartLine, world["plA"]).deleted
        assert not s.get(WorkLine, world["wlB"]).deleted
    # the legitimate delete still works and the total is recomputed
    assert b.post(f"/orders/{world['orderA']}/work/{world['wlA']}/delete").status_code == 303
    with admin_session() as s:
        assert s.get(WorkLine, world["wlA"]).deleted
        assert s.get(WorkOrder, world["orderA"]).total == 1000.0


def test_branch_user_cannot_reach_sibling_branch(world):
    b = as_user("desk@a.test")                               # branch A1 only
    assert b.get(f"/orders/{world['orderA2']}").status_code == 404
    assert "ATW-001001" not in b.get("/orders").text
    r = b.post("/branch", {"branch_id": str(world["a2"])})   # cannot switch into it either
    assert r.status_code == 303
    assert b.get(f"/orders/{world['orderA2']}").status_code == 404


def test_null_branch_rows_do_not_leak_across_orgs(world):
    """Regression: rows with branch_id=None used to be visible to every organization."""
    page = as_user("owner@a.test").get("/settings").text
    assert "Beta secret service" not in page
    assert "Alpha shared service" in page
    detail = as_user("owner@a.test").get(f"/orders/{world['orderA']}").text
    assert "Beta secret service" not in detail


def test_assistant_lookup_by_number_is_scoped(world):
    """Regression: /assistant (tg-demo) status/approve searched orders by number in all tenants."""
    b = as_user("owner@a.test")
    r = b.post("/assistant/status", {"number": world["numberB"]})
    assert "was not found" in r.text and "Client B" not in r.text
    b.post("/assistant/approve", {"number": world["numberB"]})
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderB"]).status == "estimate"
    r = b.post("/assistant/approve", {"number": world["numberA"]})
    assert "approved" in r.text


def test_same_order_number_in_two_tenants_resolves_to_own(world):
    with admin_session() as s:
        o = s.get(WorkOrder, world["orderB"])
        o.number = world["numberA"]                            # identical human number in org B
        s.add(o)
        s.commit()
    tok = make_token(world["orgB"], "orders:read")
    r = Browser().get(f"/api/v1/orders/{world['numberA']}/status", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["vehicle"] == "Skoda Octavia"
    with admin_session() as s:
        assert s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).all().__len__() == 2


def test_plate_lookup_does_not_leak_other_tenant_vehicle(world):
    b = as_user("owner@a.test")
    assert b.get(f"/integrations/plate?plate={world['plateB']}").json() == {"found": False}
    assert b.get(f"/integrations/plate?plate={world['plateA']}").json()["found"] is True


def test_operator_cannot_read_tenant_data(world):
    op = as_user("op@platform.test")
    assert op.get("/superadmin").status_code == 200
    for url in (f"/orders/{world['orderA']}", "/orders", "/clients", f"/clients/{world['clientA']}"):
        assert op.get(url).status_code in (403, 404), url
    assert "Client A" not in op.get("/orders").text


def test_tenants_cannot_open_operator_console(world):
    assert as_user("owner@a.test").get("/superadmin").status_code == 404
    assert as_user("owner@a.test").post(f"/superadmin/{world['orgB']}", {"plan_code": "trial",
                                                                         "status": "suspended"}).status_code == 404
