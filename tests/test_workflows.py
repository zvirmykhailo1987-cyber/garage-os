"""End-to-end happy paths of the workshop: intake → lines → customer → fiscal; catalog; fleet."""
from sqlmodel import select

from app.models import (
    AuditEntry,
    Branch,
    Car,
    Client,
    MonthlyAct,
    OutboxMessage,
    Part,
    PartLine,
    Service,
    SyncDevice,
    WorkOrder,
)
from tests.conftest import admin_session, as_user


def _in_branch(email, branch_id):
    b = as_user(email)
    b.post("/branch", {"branch_id": str(branch_id)})
    b.refresh_csrf()
    return b


def test_intake_creates_order_client_and_car_and_numbers_increase(world):
    b = as_user("desk@a.test")                                       # single-branch user: branch is implicit
    data = {"plate": "ka 7777 xx", "make": "Kia", "model": "Ceed", "year": "2019", "vin": "kna123",
            "client_name": "Walk-in Customer", "client_phone": "+380001112233", "complaint": "Squeal when braking",
            "master_id": str(world["masterA"]), "box": "Bay 2"}
    r = b.post("/orders/new", data)
    assert r.status_code == 303 and r.headers["location"].endswith("msg.order_created")
    r2 = b.post("/orders/new", {**data, "complaint": "Second visit"})
    with admin_session() as s:
        orders = s.exec(select(WorkOrder).where(WorkOrder.complaint.in_(["Squeal when braking", "Second visit"]))  # type: ignore[attr-defined]
                        .order_by(WorkOrder.id)).all()
        assert [o.number for o in orders] == ["AON-001003", "AON-001004"]     # continues after the seeded max
        assert orders[0].client_id == orders[1].client_id and orders[0].car_id == orders[1].car_id
        car = s.get(Car, orders[0].car_id)
        assert car.plate == "KA7777XX" and car.vin == "KNA123" and car.year == 2019
        assert s.exec(select(AuditEntry).where(AuditEntry.action == "create_order")).all()
    assert r2.status_code == 303
    page = b.get(r.headers["location"].split("?")[0]).text
    assert "Walk-in Customer" in page and "KA7777XX" in page


def test_intake_validation_and_network_scope(world):
    owner = as_user("owner@a.test")                                  # "whole network" → must pick a branch
    r = owner.post("/orders/new", {"plate": "AA1111AA", "complaint": "x"})
    assert r.status_code == 400 and "Pick a specific branch" in r.text
    desk = as_user("desk@a.test")
    r = desk.post("/orders/new", {"plate": "", "make": "", "complaint": ""})
    assert r.status_code == 400 and "Enter a plate or make" in r.text


def test_lines_from_stock_notify_and_fiscal(world):
    b = _in_branch("owner@a.test", world["a1"])
    oid = world["orderA"]
    assert b.post(f"/orders/{oid}/part", {"sku": "SKU-A", "qty": "3"}).status_code == 303     # price from stock
    assert b.post(f"/orders/{oid}/part", {"sku": "", "name": ""}).headers["location"].endswith("err.part_required")
    assert b.post(f"/orders/{oid}/work", {"service": ""}).headers["location"].endswith("err.work_required")
    assert b.post(f"/orders/{oid}/edit", {"expected_version": "2", "discount_pct": "10", "pay_method": "card",
                                          "paid": "true", "status": "paid", "plate": "zz9999zz",
                                          "mileage": "61000", "year": "nope"}).status_code == 303
    with admin_session() as s:
        o = s.get(WorkOrder, oid)
        line = s.exec(select(PartLine).where(PartLine.sku == "SKU-A")).one()
        assert line.price == 100 and line.qty == 3 and line.name == "Part A"
        assert o.status == "paid" and o.paid and o.pay_method == "card"
        assert o.total == round((500 + 1000 + 300) * 0.9, 2)
        car = s.get(Car, o.car_id)
        assert car.plate == "ZZ9999ZZ" and car.mileage == 61000 and car.year is None
    assert b.post(f"/orders/{oid}/notify", {"confirm": "yes", "channel": "fax"}).status_code == 400
    assert b.post(f"/orders/{oid}/notify", {"confirm": "yes", "channel": "viber"}).status_code == 303
    assert b.post(f"/orders/{oid}/fiscal", {"confirm": "yes"}).status_code == 303
    with admin_session() as s:
        msg = s.exec(select(OutboxMessage)).one()
        assert msg.status == "SIMULATED" and msg.channel == "viber" and msg.org_id == world["orgA"]
        assert s.get(WorkOrder, oid).fiscal_no.startswith("SIMULATED-")
    page = b.get(f"/orders/{oid}").text
    assert "SIMULATED" in page
    assert b.post(f"/orders/{world['orderA_2']}/fiscal", {"confirm": "yes"}).headers["location"].endswith("err.zero_total")


def test_notify_without_phone(world):
    with admin_session() as s:
        c = s.get(Client, world["clientA"])
        c.phone = ""
        s.add(c)
        s.commit()
    b = _in_branch("owner@a.test", world["a1"])
    r = b.post(f"/orders/{world['orderA']}/notify", {"confirm": "yes", "channel": "sms"})
    assert r.headers["location"].endswith("err.no_phone")


def test_clients_and_stock_creation(world):
    b = as_user("desk@a.test")
    r = b.post("/clients/new", {"name": "New Company LLC", "phone": "+380009998877", "kind": "company",
                                "discount": "150"})
    assert r.status_code == 303
    assert b.post("/clients/new", {"name": " "}).headers["location"].endswith("err.name_required")
    page = b.get("/clients?q=New+Company").text
    assert "New Company LLC" in page and "Company" in page
    m = as_user("manager@a.test")
    assert m.post("/parts/new", {"sku": "NEW-1", "name": "Wiper", "qty": "1", "min_qty": "4",
                                 "price": "abc"}).status_code == 303
    assert m.post("/parts/new", {"sku": "", "name": ""}).headers["location"].endswith("err.sku_name_required")
    low = m.get("/parts?low=1&q=wiper").text
    assert "Wiper" in low and "row-warn" in low
    with admin_session() as s:
        c = s.exec(select(Client).where(Client.name == "New Company LLC")).one()
        assert c.discount == 100 and c.branch_id == world["a1"]              # clamped to 100 %
        p = s.exec(select(Part).where(Part.sku == "NEW-1")).one()
        assert p.price == 0 and p.org_id == world["orgA"]


def test_catalog_team_branches_devices(world):
    owner = as_user("owner@a.test")
    assert owner.post("/settings/service", {"name": "Detailing", "norm_hours": "2", "shared": "1"}).status_code == 303
    assert owner.post("/settings/service", {"name": "Local only"}).headers["location"].endswith("err.pick_branch")
    assert as_user("manager@a.test").post("/settings/service", {"name": "x", "shared": "1"}).status_code == 403
    assert owner.post("/settings/branches", {"name": "Alpha Three", "region": "Odesa", "prefix": "a-3!"}).status_code == 303
    assert owner.post("/settings/branches", {"name": "", "prefix": ""}).headers["location"].endswith("err.branch_fields")
    assert owner.post("/settings/users", {"name": "x", "email": "bad", "password": "0123456789", "role": "master"}) \
        .headers["location"].endswith("err.user_fields")
    assert owner.post("/settings/users", {"name": "x", "email": "a@b.test", "password": "short", "role": "master",
                                          "branch_id": str(world["a1"])}).headers["location"].endswith("err.password_short")
    assert owner.post("/settings/users", {"name": "x", "email": "owner@b.test", "password": "test-pw-0123456789",
                                          "role": "net_admin"}).headers["location"].endswith("err.email_taken")
    assert owner.post(f"/settings/users/{world['owner']}/toggle").headers["location"].endswith("err.self_disable")
    assert owner.post(f"/settings/users/{world['ownerB']}/toggle").status_code == 404
    page = owner.post("/settings/devices", {"label": "Front desk PC"}).text
    assert "dev_" in page
    assert owner.post("/settings/devices", {"label": ""}).headers["location"].endswith("err.name_required")
    assert owner.post("/settings/tokens", {"label": "", "scopes": []}).headers["location"].endswith("err.token_fields")
    with admin_session() as s:
        svc = s.exec(select(Service).where(Service.name == "Detailing")).one()
        assert svc.branch_id is None and svc.org_id == world["orgA"]
        assert s.exec(select(Branch).where(Branch.name == "Alpha Three")).one().order_prefix == "A3"
        dev = s.exec(select(SyncDevice)).one()
    assert owner.post(f"/settings/devices/{dev.id}/revoke").headers["location"].endswith("msg.revoked")
    assert as_user("owner@b.test").post(f"/settings/devices/{dev.id}/revoke").status_code == 404


def test_fleet_monthly_act(world):
    b = as_user("owner@a.test")
    page = b.get(f"/fleet/{world['fleetA']}").text
    assert "A-1" in page and "Alpha fleet" in page
    assert b.post(f"/fleet/{world['fleetA']}/act", {"period": "2026-13"}).headers["location"].endswith("err.bad_period")
    from app.models import utcnow
    period = utcnow().strftime("%Y-%m")
    assert b.post(f"/fleet/{world['fleetA']}/act", {"period": period}).headers["location"].endswith("msg.act_created")
    with admin_session() as s:                       # unpaid orders are not billed
        assert s.exec(select(MonthlyAct)).one().orders == 0
        o = s.get(WorkOrder, world["orderA"])
        o.paid, o.status = True, "paid"
        s.add(o)
        s.commit()
    b.post(f"/fleet/{world['fleetA']}/act", {"period": period})        # regenerate, not duplicate
    with admin_session() as s:
        act = s.exec(select(MonthlyAct)).one()
        assert act.orders == 1 and act.total == 1500.0 and act.org_id == world["orgA"]
    assert as_user("desk@a.test").post(f"/fleet/{world['fleetA']}/act", {"period": period}).status_code == 403
    list_page = b.get("/fleet").text
    assert "Alpha fleet" in list_page and "Beta fleet" not in list_page


def test_operator_changes_plan_and_status(world):
    op = as_user("op@platform.test")
    page = op.get("/superadmin").text
    assert "Alpha Network" in page and "Beta Garage" in page
    assert op.post(f"/superadmin/{world['orgB']}", {"plan_code": "nope", "status": "active"}).status_code == 400
    assert op.post("/superadmin/99999", {"plan_code": "trial", "status": "active"}).status_code == 404
    assert op.post(f"/superadmin/{world['orgB']}", {"plan_code": "single", "status": "suspended"}).status_code == 303
    owner_b = as_user("owner@b.test")
    assert "read-only" in owner_b.get("/").text
    assert owner_b.post("/clients/new", {"name": "x"}).status_code == 403


def test_branch_switch_and_language(world):
    b = as_user("owner@a.test")
    assert b.post("/branch", {"branch_id": str(world["b1"]), "next": "/orders"}).headers["location"] == "/orders"
    assert "BON-001001" not in b.get("/orders").text                   # foreign branch id is ignored
    b.post("/branch", {"branch_id": str(world["a2"])})
    assert "ATW-001001" in b.get("/orders").text and "AON-001001" not in b.get("/orders").text
    b.refresh_csrf()
    b.post("/branch", {"branch_id": ""})                                # back to the whole network
    assert "AON-001001" in b.get("/orders").text
    r = b.get("/lang/uk?next=/kanban")
    assert r.headers["location"] == "/kanban" and "sto_lang=uk" in r.headers["set-cookie"]
