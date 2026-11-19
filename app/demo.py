"""Synthetic demo data — only ever loaded when DEMO_MODE=1 (refused in production).

Everything here is generated: names are random combinations, phone numbers use the
non-existent +380 00 operator code, tax ids are zero-padded test values, e-mails use the
reserved ``.test`` domain. Deterministic (fixed seed), so screenshots are reproducible.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sqlmodel import Session, select

from .db import set_tenant
from .models import (
    ORG_ACTIVE,
    ORG_SUSPENDED,
    ROLE_BRANCH_MANAGER,
    ROLE_MASTER,
    ROLE_NET_ADMIN,
    ROLE_RECEPTIONIST,
    ST_PAID,
    WO_STATUSES,
    Branch,
    BranchCounter,
    Car,
    Client,
    FleetContract,
    FleetVehicle,
    MaintenancePlan,
    Master,
    Membership,
    Organization,
    Part,
    PartLine,
    Service,
    User,
    WorkLine,
    WorkOrder,
    order_totals,
    utcnow,
)
from .security import hash_password

# Public demo credential; DEMO_MODE is refused when STO_ENV=production.
DEMO_PASSWORD = "demo-password-2026"  # nosec B105
DEMO_ACCOUNTS = [
    ("owner@northwind.test", "Network owner", "Northwind · all branches"),
    ("manager.kyiv@northwind.test", "Branch manager", "Northwind · Kyiv"),
    ("desk.lviv@northwind.test", "Receptionist", "Northwind · Lviv"),
    ("mechanic.kyiv@northwind.test", "Mechanic", "Northwind · Kyiv"),
    ("owner@riverside.test", "Owner (plan without AI)", "Riverside Garage"),
    ("owner@halted.test", "Owner (suspended plan)", "Halted Motors"),
    ("operator@platform.test", "Platform operator", "SaaS console"),
]

FIRST = ["Olena", "Andrii", "Ivanna", "Taras", "Oksana", "Dmytro", "Mariia", "Serhii", "Yulia",
         "Bohdan", "Kateryna", "Vasyl", "Nadiia", "Ostap", "Sofiia", "Roman", "Halyna", "Petro"]
LAST = ["Demchenko", "Testenko", "Prykladiuk", "Zrazok", "Shablii", "Makety", "Modelenko",
        "Proba", "Etalonko", "Demko", "Sampliuk", "Variantko"]
COMPANIES = ["Demo Logistics LLC", "Sample Freight LLC", "Example Bakery LLC", "Test Couriers LLC"]
CARS = [("Volkswagen", "Passat", "diesel"), ("Skoda", "Octavia", "petrol"), ("Toyota", "Camry", "hybrid"),
        ("Renault", "Megane", "diesel"), ("Ford", "Transit", "diesel"), ("Hyundai", "Tucson", "petrol"),
        ("BMW", "320d", "diesel"), ("Mercedes-Benz", "Sprinter", "diesel"), ("Kia", "Ceed", "petrol"),
        ("Nissan", "Leaf", "electric")]
SERVICES = [("Engine oil change", 0.4), ("Brake pad replacement", 0.8), ("Timing belt kit replacement", 3.5),
            ("Suspension diagnostics", 0.7), ("Wheel alignment", 1.0), ("Air-conditioning service", 1.0),
            ("Clutch replacement", 4.0), ("Scheduled maintenance", 1.5), ("Battery replacement", 0.3),
            ("Shock absorber replacement", 1.5), ("Water pump replacement", 1.0), ("Tyre fitting", 0.6)]
PARTS = [("OF-1001", "Oil filter", "Mann", 180), ("BP-2040", "Brake pads (front)", "Bosch", 1450),
         ("TB-3300", "Timing belt kit", "Gates", 4800), ("WP-4410", "Water pump", "SKF", 2300),
         ("AF-1102", "Air filter", "Mann", 420), ("SP-0700", "Spark plug", "NGK", 260),
         ("SA-5501", "Shock absorber (front)", "Sachs", 3100), ("CL-6000", "Clutch kit", "LuK", 7900),
         ("BT-7400", "Battery 74Ah", "Varta", 4300), ("CO-0005", "Coolant 5 L", "Febi", 650),
         ("WB-8120", "Wheel bearing", "SKF", 1750), ("EO-5W30", "Engine oil 5W-30 4 L", "Castrol", 1350)]
PLATE_LETTERS = "ABCEHIKMOPTX"


def _phone(rng: random.Random) -> str:
    return f"+38000{rng.randint(1000000, 9999999)}"


def _plate(rng: random.Random) -> str:
    return (rng.choice(PLATE_LETTERS) + rng.choice(PLATE_LETTERS) + f"{rng.randint(1000, 9999)}"
            + rng.choice(PLATE_LETTERS) + rng.choice(PLATE_LETTERS))


def _user(s: Session, org: Organization | None, email: str, name: str, pw_hash: str,
          role: str | None = None, branch: Branch | None = None, superadmin: bool = False) -> User:
    u = User(org_id=org.id if org else None, email=email, name=name, password_hash=pw_hash,
             is_superadmin=superadmin)
    s.add(u)
    s.flush()
    if org is not None and role is not None:
        if org.id is None or u.id is None:
            raise RuntimeError("invariant: org.id and u.id are set")
        s.add(Membership(org_id=org.id, user_id=u.id, role=role,
                         branch_id=None if role == ROLE_NET_ADMIN else (branch.id if branch else None)))
    return u


def _fill_branch(s: Session, rng: random.Random, org: Organization, b: Branch, n_orders: int,
                 services: list[Service]) -> list[Car]:
    if org.id is None or b.id is None:
        raise RuntimeError("invariant: org.id and b.id are set")
    now = utcnow()
    masters = [Master(name=f"{rng.choice(FIRST)} {rng.choice(LAST)}", rate=rng.choice([450, 500, 550, 600]),
                      org_id=org.id, branch_id=b.id) for _ in range(4)]
    s.add_all(masters)
    for sku, name, brand, price in PARTS:
        qty = rng.choice([0, 1, 2, 3, 6, 10, 14, 20])
        s.add(Part(sku=sku, name=name, brand=brand, supplier="Demo Parts Hub", qty=qty,
                   min_qty=rng.choice([2, 4, 5]), cost=round(price * 0.7, 2), price=price,
                   location=f"R{rng.randint(1, 6)}-{rng.randint(1, 12)}", org_id=org.id, branch_id=b.id))
    clients, cars = [], []
    for i in range(max(12, n_orders // 2)):
        is_company = i % 9 == 0
        c = Client(name=rng.choice(COMPANIES) if is_company else f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                   phone=_phone(rng), kind="company" if is_company else "private",
                   tax_id=f"0000{rng.randint(1000, 9999)}" if is_company else "",
                   discount=5.0 if is_company else 0.0, org_id=org.id, branch_id=b.id)
        clients.append(c)
    s.add_all(clients)
    s.flush()
    for c in clients:
        make, model, engine = rng.choice(CARS)
        car = Car(client_id=c.id, plate=_plate(rng), make=make, model=model, engine=engine,
                  year=rng.randint(2009, 2024), mileage=rng.randint(20, 290) * 1000,
                  vin=f"DEMO{rng.randint(10**12, 10**13 - 1)}", org_id=org.id, branch_id=b.id)
        cars.append(car)
    s.add_all(cars)
    s.flush()
    seq = 1001
    for i in range(n_orders):
        car = rng.choice(cars)
        age = timedelta(days=rng.uniform(0, 45), hours=rng.uniform(0, 10))
        status = WO_STATUSES[min(5, int(rng.triangular(0, 6, 5)))] if i > 4 else WO_STATUSES[i % 5]
        m = rng.choice(masters)
        o = WorkOrder(number=f"{b.order_prefix}-{seq:06d}", status=status, car_id=car.id,
                      client_id=car.client_id, master_id=m.id, box=f"Bay {rng.randint(1, 5)}",
                      complaint=rng.choice(["Knocking noise from the front suspension",
                                            "Scheduled service, customer asks to check brakes",
                                            "Engine warning light on, rough idle",
                                            "Squeal when braking", "A/C blows warm air",
                                            "Clutch slipping on hills"]),
                      accepted_at=now - age, created_at=now - age, updated_at=now - age * 0.6,
                      paid=status == ST_PAID, pay_method="card" if status == ST_PAID else "none",
                      paid_at=now - age * 0.6 if status == ST_PAID else None,
                      discount_pct=5.0 if i % 11 == 0 else 0.0, org_id=org.id, branch_id=b.id)
        seq += 1
        s.add(o)
        s.flush()
        if o.id is None:
            raise RuntimeError("invariant: o.id is set")
        works = [WorkLine(order_id=o.id, service=svc.name, norm_hours=svc.norm_hours, rate=m.rate,
                          master_id=m.id, org_id=org.id, branch_id=b.id)
                 for svc in rng.sample(services, rng.randint(1, 3))]
        parts = [PartLine(order_id=o.id, sku=sku, name=name, qty=rng.choice([1, 1, 2, 4]), price=price,
                          org_id=org.id, branch_id=b.id)
                 for sku, name, _brand, price in rng.sample(PARTS, rng.randint(0, 3))]
        s.add_all(works)
        s.add_all(parts)
        t = order_totals(works, parts, o.discount_pct, o.vat_pct)
        o.works_total, o.parts_total, o.discount_total = t["works"], t["parts"], t["discount"]
        o.vat_total, o.total = t["vat"], t["total"]
    s.add(BranchCounter(branch_id=b.id, next_seq=seq))
    return cars


def _org(s: Session, name: str, slug: str, plan: str, status: str = ORG_ACTIVE) -> Organization:
    org = Organization(name=name, slug=slug, plan_code=plan, status=status)
    s.add(org)
    s.flush()
    set_tenant(s, org.id)
    return org


def seed_demo(s: Session) -> bool:
    """Idempotent: does nothing if any organization exists."""
    if s.exec(select(Organization)).first() is not None:
        return False
    rng = random.Random(2026)  # nosec B311
    pw = hash_password(DEMO_PASSWORD)

    nw = _org(s, "Northwind Auto Network", "northwind", "network")
    if nw.id is None:
        raise RuntimeError("invariant: nw.id is set")
    kyiv = Branch(org_id=nw.id, name="Kyiv · Podil", region="Kyiv", order_prefix="KYI",
                  address="12 Demo Street", phone=_phone(rng))
    lviv = Branch(org_id=nw.id, name="Lviv · West", region="Lviv", order_prefix="LVI",
                  address="7 Sample Avenue", phone=_phone(rng))
    odesa = Branch(org_id=nw.id, name="Odesa · Port", region="Odesa", order_prefix="ODE",
                   address="3 Example Quay", phone=_phone(rng))
    s.add_all([kyiv, lviv, odesa])
    s.flush()
    services = [Service(name=n, norm_hours=h, org_id=nw.id, branch_id=None) for n, h in SERVICES]
    s.add_all(services)
    _user(s, nw, "owner@northwind.test", "Olena Demchenko", pw, ROLE_NET_ADMIN)
    _user(s, nw, "manager.kyiv@northwind.test", "Taras Prykladiuk", pw, ROLE_BRANCH_MANAGER, kyiv)
    _user(s, nw, "desk.lviv@northwind.test", "Ivanna Zrazok", pw, ROLE_RECEPTIONIST, lviv)
    _user(s, nw, "mechanic.kyiv@northwind.test", "Bohdan Shablii", pw, ROLE_MASTER, kyiv)
    kyiv_cars = _fill_branch(s, rng, nw, kyiv, 70, services)
    _fill_branch(s, rng, nw, lviv, 45, services)
    _fill_branch(s, rng, nw, odesa, 30, services)
    fleet_client = Client(name="Demo Logistics LLC", phone=_phone(rng), kind="fleet", tax_id="00000001",
                          discount=7.0, org_id=nw.id, branch_id=kyiv.id)
    s.add(fleet_client)
    s.flush()
    contract = FleetContract(client_id=fleet_client.id or 0, name="Demo Logistics — service contract",
                             credit_terms_days=14, org_id=nw.id, branch_id=kyiv.id)
    s.add(contract)
    s.flush()
    for i, car in enumerate(kyiv_cars[:6]):
        car.client_id = fleet_client.id
        s.add(FleetVehicle(contract_id=contract.id or 0, car_id=car.id or 0, unit_no=f"DL-{i + 1:03d}",
                           org_id=nw.id, branch_id=kyiv.id))
        last = (car.mileage or 0) - rng.choice([2000, 9000, 14500, 16000])
        s.add(MaintenancePlan(car_id=car.id or 0, interval_km=15000, last_service_km=last,
                              next_due_km=last + 15000, org_id=nw.id, branch_id=kyiv.id))

    rv = _org(s, "Riverside Garage", "riverside", "single")
    river = Branch(org_id=rv.id or 0, name="Riverside · Dnipro", region="Dnipro", order_prefix="RIV")
    s.add(river)
    s.flush()
    rv_services = [Service(name=n, norm_hours=h, org_id=rv.id, branch_id=None) for n, h in SERVICES[:8]]
    s.add_all(rv_services)
    _user(s, rv, "owner@riverside.test", "Roman Etalonko", pw, ROLE_NET_ADMIN)
    _fill_branch(s, rng, rv, river, 25, rv_services)

    hm = _org(s, "Halted Motors", "halted", "trial", status=ORG_SUSPENDED)
    halted = Branch(org_id=hm.id or 0, name="Halted · Kharkiv", region="Kharkiv", order_prefix="HLT")
    s.add(halted)
    s.flush()
    hm_services = [Service(name=n, norm_hours=h, org_id=hm.id, branch_id=None) for n, h in SERVICES[:5]]
    s.add_all(hm_services)
    _user(s, hm, "owner@halted.test", "Halyna Proba", pw, ROLE_NET_ADMIN)
    _fill_branch(s, rng, hm, halted, 8, hm_services)

    set_tenant(s, None)
    _user(s, None, "operator@platform.test", "Platform operator", pw, superadmin=True)
    s.commit()
    return True
