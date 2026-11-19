"""Offline sync: per-device tokens, tenant-scoped export, optimistic versioning with 409."""
import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import create_schema, make_engine
from app.main import app
from app.models import Branch, Client, Organization, SyncConflict, WorkLine, WorkOrder
from app.sync.client import sync_once
from app.sync.engine import dirty_rows
from tests.conftest import _TMP, admin_session, as_user, make_device


def _edge(name: str):
    path = _TMP / f"edge-{name}.sqlite"
    if path.exists():
        path.unlink()
    eng = make_engine(f"sqlite:///{path}")
    create_schema(eng)
    with Session(eng) as s:
        s.add(Organization(name="edge copy", slug="edge"))
        s.commit()
    return eng


def _sync(eng, token):
    return sync_once(TestClient(app)._transport, eng=eng, hub_url="http://testserver", token=token)


def _raw_sync(token, body):
    return TestClient(app).post("/sync", json=body, headers={"Authorization": f"Bearer {token}"})


def test_sync_requires_a_valid_device_token(world):
    c = TestClient(app)
    assert c.post("/sync", json={}).status_code == 401
    assert c.post("/sync", json={}, headers={"Authorization": "Bearer dev_wrong"}).status_code == 401
    assert c.post("/sync", json={}, headers={"X-Sync-Token": "anything"}).status_code == 401
    revoked = make_device(world["orgA"], active=False)
    assert _raw_sync(revoked, {}).status_code == 401


def test_pull_exports_only_own_tenant_and_never_users(world):
    """Regression: /sync used to return the whole database including password hashes."""
    tok = make_device(world["orgA"])
    r = _raw_sync(tok, {"since": None, "changes": {}})
    assert r.status_code == 200
    body = r.text
    assert "password_hash" not in body and "argon2" not in body and '"users"' not in body
    data = r.json()["changes"]
    names = {c["name"] for c in data["clients"]}
    assert names == {"Client A", "Client A2"}
    assert all("org_id" not in row for rows in data.values() for row in rows)


def test_edge_round_trip_push_and_pull(world):
    tok = make_device(world["orgA"])
    edge = _edge("rt")
    assert _sync(edge, tok) == (True, "ok")
    with Session(edge) as s:
        assert {c.name for c in s.exec(select(Client)).all()} == {"Client A", "Client A2"}
        assert dirty_rows(s, 1)["clients"] == []                    # clean after first sync
        c = s.exec(select(Client).where(Client.name == "Client A")).one()
        c.name, c.version = "Client A (edited offline)", c.version + 1
        s.add(c)
        line = s.exec(select(WorkLine).where(WorkLine.service == "Brake pads", WorkLine.deleted == False)).first()  # noqa: E712
        line.deleted, line.version = True, line.version + 1        # an offline delete
        s.add(line)
        s.commit()
    assert _sync(edge, tok) == (True, "ok")
    with admin_session() as s:
        hub = s.get(Client, world["clientA"])
        assert hub.name == "Client A (edited offline)" and hub.version == 2
        assert s.get(WorkLine, world["wlA"]).deleted is True
    with Session(edge) as s:
        assert dirty_rows(s, 1)["clients"] == []


def test_concurrent_edit_returns_409_and_is_recorded_not_overwritten(world):
    tok = make_device(world["orgA"])
    edge = _edge("conflict")
    assert _sync(edge, tok)[0]
    owner = as_user("owner@a.test")                                 # hub edit after the edge pulled
    assert owner.post(f"/orders/{world['orderA']}/edit", {"expected_version": "1", "box": "HUB"}).status_code == 303
    with Session(edge) as s:                                        # edge edits the same order offline
        o = s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one()
        o.box, o.version = "EDGE", o.version + 1
        s.add(o)
        s.commit()
    assert _sync(edge, tok) == (False, "conflict")
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderA"]).box == "HUB"      # hub value kept
        conflict = s.exec(select(SyncConflict)).one()
        assert conflict.entity == "orders" and conflict.base_version == 1 and conflict.hub_version == 2
        assert json.loads(conflict.incoming)["box"] == "EDGE"      # edge edit preserved for review
    with Session(edge) as s:
        o = s.exec(select(WorkOrder).where(WorkOrder.number == world["numberA"])).one()
        assert o.box == "HUB" and o.version == o.sync_base == 2     # edge adopted the hub version
        assert s.exec(select(SyncConflict)).one().row_uid == o.uid
    assert _sync(edge, tok) == (True, "ok")                         # converges afterwards


def test_stale_push_is_rejected_atomically(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        c = s.get(Client, world["clientA"])
        c2 = s.get(Client, world["clientA2"])
        branch_uid = s.get(Branch, world["a1"]).uid
        rows = [{"uid": c.uid, "name": "stale", "base_version": 0, "branch_id__uid": branch_uid},
                {"uid": c2.uid, "name": "fresh", "base_version": c2.version, "branch_id__uid": branch_uid}]
    r = _raw_sync(tok, {"changes": {"clients": rows}})
    assert r.status_code == 409 and r.json()["conflicts"][0]["uid"] == rows[0]["uid"]
    with admin_session() as s:
        assert s.get(Client, world["clientA2"]).name == "Client A2"   # nothing applied


def test_push_cannot_write_into_another_tenant(world):
    tok = make_device(world["orgA"])
    with admin_session() as s:
        b_branch = s.get(Branch, world["b1"]).uid
        a_branch = s.get(Branch, world["a1"]).uid
        b_client = s.get(Client, world["clientB"])
    r = _raw_sync(tok, {"changes": {"clients": [{"uid": "f" * 32, "name": "x", "base_version": 0,
                                                 "branch_id__uid": b_branch}]}})
    assert r.status_code == 400
    # Another tenant's uid: SQLite sees the row and refuses (400); on Postgres RLS hides it,
    # so the stale base_version conflicts (409) — either way nothing is written.
    r = _raw_sync(tok, {"changes": {"clients": [{"uid": b_client.uid, "name": "pwned",
                                                 "base_version": b_client.version}]}})
    assert r.status_code in (400, 409)
    r = _raw_sync(tok, {"changes": {"clients": [{"uid": b_client.uid, "name": "pwned", "base_version": 0,
                                                 "branch_id__uid": a_branch}]}})
    assert r.status_code == 400
    r = _raw_sync(tok, {"changes": {"branches": [{"uid": "e" * 32, "name": "x"}]}})
    assert r.status_code == 400
    r = _raw_sync(tok, {"changes": {"users": [{"uid": "d" * 32}]}})
    assert r.status_code == 400
    with admin_session() as s:
        assert s.get(Client, world["clientB"]).name == "Client B"
