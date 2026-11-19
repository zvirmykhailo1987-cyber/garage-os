"""N+1 regression: the number of SQL statements per page must not grow with the data."""
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session

from app.db import admin_engine, engine
from app.models import Car, Client, FleetContract, FleetVehicle, Organization, WorkOrder
from tests.conftest import as_user


@contextmanager
def count_queries():
    n = {"q": 0}

    def _cb(*_a, **_k):
        n["q"] += 1
    event.listen(engine, "before_cursor_execute", _cb)
    try:
        yield n
    finally:
        event.remove(engine, "before_cursor_execute", _cb)


def _grow(world, factor: int):
    with Session(admin_engine) as s:
        for i in range(factor):
            c = Client(name=f"Bulk {i}", org_id=world["orgA"], branch_id=world["a1"])
            s.add(c)
            s.flush()
            car = Car(plate=f"BU{i:04d}LK", client_id=c.id, org_id=world["orgA"], branch_id=world["a1"])
            s.add(car)
            s.flush()
            s.add(WorkOrder(number=f"AON-{5000 + i:06d}", car_id=car.id, client_id=c.id,
                            org_id=world["orgA"], branch_id=world["a1"]))
            fc = FleetContract(client_id=c.id, name=f"Fleet {i}", org_id=world["orgA"], branch_id=world["a1"])
            s.add(fc)
            s.flush()
            s.add(FleetVehicle(contract_id=fc.id, car_id=car.id, unit_no=f"U{i}", org_id=world["orgA"],
                               branch_id=world["a1"]))
            s.add(Organization(name=f"Tenant {i}", slug=f"t{i}"))
        s.commit()


def _measure(b, urls):
    out = {}
    for u in urls:
        with count_queries() as n:
            assert b.get(u).status_code == 200, u
        out[u] = n["q"]
    return out


def test_query_count_is_constant(world):
    owner = as_user("owner@a.test")
    op = as_user("op@platform.test")
    urls = ["/", "/orders", "/kanban", "/clients", "/fleet"]
    small = _measure(owner, urls) | {"op": _measure(op, ["/superadmin"])["/superadmin"]}
    _grow(world, 30)
    big = _measure(owner, urls) | {"op": _measure(op, ["/superadmin"])["/superadmin"]}
    assert big == small, (small, big)
