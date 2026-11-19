# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Sync engine shared by the hub endpoint and the edge client.

Protocol (one round trip, ``POST /sync``)
-----------------------------------------
* Identity of a row is its ``uid``; foreign keys travel as the parent's uid
  (``client_id__uid``) because local integer ids differ between copies.
* ``org_id`` never travels: the hub stamps the device's tenant, the edge stamps its own.
* Push carries only *dirty* edge rows (``version != sync_base``) and, per row, the
  ``base_version`` — the hub version the edit was made on top of.
* The hub applies a row only if ``hub.version == base_version`` (fast-forward) and then
  bumps the version. Otherwise it records a ``SyncConflict`` and the whole push is
  rejected with **409** listing the conflicts plus the hub's current rows, so the edge
  can adopt them; nothing is overwritten silently.
* Pull: rows the hub touched since the edge's cursor (``srv_at > since``).
* Deletes are soft (``deleted`` flag on order lines), so they replicate as updates.
* Users and password hashes are never part of the protocol.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any, get_args

from sqlmodel import Session, select

from ..models import (
    Branch,
    Car,
    Client,
    Master,
    OutboxMessage,
    Part,
    PartLine,
    Service,
    SyncConflict,
    WorkLine,
    WorkOrder,
    utcnow,
)

# (name, model, fk map, mode) — parents before children. mode: lww | append | pull_only
TABLES: list[tuple[str, Any, dict[str, Any], str]] = [
    ("branches", Branch, {}, "pull_only"),
    ("clients", Client, {"branch_id": Branch}, "versioned"),
    ("masters", Master, {"branch_id": Branch}, "versioned"),
    ("services", Service, {"branch_id": Branch}, "versioned"),
    ("parts", Part, {"branch_id": Branch}, "versioned"),
    ("cars", Car, {"branch_id": Branch, "client_id": Client}, "versioned"),
    ("orders", WorkOrder, {"branch_id": Branch, "client_id": Client, "car_id": Car,
                           "master_id": Master}, "versioned"),
    ("work_lines", WorkLine, {"branch_id": Branch, "order_id": WorkOrder, "master_id": Master}, "versioned"),
    ("part_lines", PartLine, {"branch_id": Branch, "order_id": WorkOrder, "part_id": Part}, "versioned"),
    ("outbox", OutboxMessage, {"branch_id": Branch, "order_id": WorkOrder, "client_id": Client}, "append"),
]
TABLE_NAMES = {t[0] for t in TABLES}
_LOCAL_ONLY = {"id", "org_id", "srv_at", "sync_base", "tg_code_hash", "tg_code_expires"}
_DT = {"created_at", "updated_at", "accepted_at", "promised_at"}
MAX_ROWS_PER_PUSH = 5000
# Columns the hub computes or owns — an edge can never write them.
HUB_OWNED = {"orders": {"fiscal_no", "works_total", "parts_total", "discount_total", "vat_total", "total",
                        "paid_at"}}
# set once at creation; a line can never be moved to another order afterwards
INSERT_ONLY = {"orders": {"number"}, "work_lines": {"order_id__uid"}, "part_lines": {"order_id__uid"}}
MONEY_FIELDS = {"discount_pct", "vat_pct", "pay_method", "paid", "status"}
HUB_NUMBER = re.compile(r"^[^-\s]{1,6}-\d{6}$")        # any branch prefix (Cyrillic too)
PARKED_NUMBER = re.compile(r"-DUP\d{3}$")                # a temporary number from _free_local_number


def _rng(data: dict[str, Any], key: str, lo: float, hi: float) -> None:
    v = data.get(key)
    if v is not None and not (lo <= v <= hi):
        raise SyncError(f"{key} out of range")


def _validate_values(name: str, data: dict[str, Any]) -> None:
    """The same business rules the web forms enforce."""
    from ..models import CLIENT_KINDS, PAY_METHODS, WO_STATUSES
    if name == "orders":
        if "status" in data and data["status"] not in WO_STATUSES:
            raise SyncError("unknown status")
        if "pay_method" in data and data["pay_method"] not in PAY_METHODS:
            raise SyncError("unknown pay_method")
        _rng(data, "discount_pct", 0, 100)
        _rng(data, "vat_pct", 0, 100)
    elif name == "clients":
        if "kind" in data and data["kind"] not in CLIENT_KINDS:
            raise SyncError("unknown client kind")
        _rng(data, "discount", 0, 100)
    elif name in ("work_lines", "part_lines", "parts", "masters"):
        for k in ("norm_hours", "rate", "qty", "price", "cost", "min_qty"):
            _rng(data, k, 0, 1e7)


class SyncError(ValueError):
    pass


def _dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError as e:
        raise SyncError(f"bad datetime {v!r}") from e


def _dump(obj: Any, fkmap: dict[str, Any], id2uid: dict[Any, dict[int, str]]) -> dict[str, Any]:
    d = obj.model_dump()
    for k in _LOCAL_ONLY:
        d.pop(k, None)
    for k, v in list(d.items()):
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
    for fk, parent in fkmap.items():
        val = d.pop(fk, None)
        d[fk + "__uid"] = id2uid.get(parent, {}).get(val) if val is not None else None
    return d


def _uid_maps(s: Session, org_id: int, needed: dict[Any, set[int]]) -> dict[Any, dict[int, str]]:
    out: dict[Any, dict[int, str]] = {}
    for parent, ids in needed.items():
        out[parent] = {r.id: r.uid for r in s.exec(select(parent).where(
            parent.org_id == org_id, parent.id.in_(ids))).all()} if ids else {}
    return out


def export_rows(s: Session, org_id: int, rows_by_table: dict[str, list[Any]]) -> dict[str, list[dict]]:
    needed: dict[Any, set[int]] = {}
    for name, _model, fkmap, _ in TABLES:
        for fk, parent in fkmap.items():
            ids = needed.setdefault(parent, set())
            ids.update(getattr(o, fk) for o in rows_by_table.get(name, []) if getattr(o, fk, None))
    id2uid = _uid_maps(s, org_id, needed)
    return {name: [_dump(o, fkmap, id2uid) for o in rows_by_table.get(name, [])]
            for name, _m, fkmap, _ in TABLES}


def pull_delta(s: Session, org_id: int, since: str | None) -> dict[str, list[dict]]:
    """Hub → edge: every row of this tenant the hub touched after ``since``."""
    since_dt = _dt(since) if since else None
    rows: dict[str, list[Any]] = {}
    for name, model, _fk, _mode in TABLES:
        stmt = select(model).where(model.org_id == org_id)
        if since_dt is not None:
            stmt = stmt.where(model.srv_at > since_dt)
        rows[name] = list(s.exec(stmt.order_by(model.id)).all())
    return export_rows(s, org_id, rows)


def dirty_rows(s: Session, org_id: int) -> dict[str, list[dict]]:
    """Edge → hub: rows edited locally since the last successful sync."""
    rows: dict[str, list[Any]] = {}
    for name, model, _fk, mode in TABLES:
        if mode == "pull_only":
            continue
        rows[name] = list(s.exec(select(model).where(model.org_id == org_id,
                                                     model.version != model.sync_base)).all())
    out = export_rows(s, org_id, rows)
    for name, _model, _fk, _m in TABLES:
        by_uid = {o.uid: o for o in rows.get(name, [])}
        for r in out.get(name, []):
            r["base_version"] = by_uid[r["uid"]].sync_base
    return out


def _resolve(s: Session, org_id: int, data: dict[str, Any], fkmap: dict[str, Any],
             cache: dict[tuple[Any, str], int | None], *, update: bool, strict: bool) -> dict[str, Any]:
    """Map ``<fk>__uid`` keys to local ids inside this tenant.

    * update: a key that is ABSENT leaves the existing foreign key untouched (partial rows
      never wipe relations); on insert a missing key means "no parent".
    * strict (hub): every row must name its branch, and a uid that does not resolve inside
      the tenant is an error — never a silent NULL.
    """
    for fk, parent in fkmap.items():
        key = fk + "__uid"
        if key not in data:
            if update:
                continue
            if strict and parent is Branch:
                raise SyncError("row must reference its branch")
            data[fk] = None
            continue
        puid = data.pop(key)
        if not puid:
            if strict and parent is Branch:
                raise SyncError("row must reference its branch")
            data[fk] = None
            continue
        if not isinstance(puid, str) or len(puid) > 32:
            raise SyncError(f"bad {key}")
        ck = (parent, puid)
        if ck not in cache:
            p = s.exec(select(parent).where(parent.uid == puid, parent.org_id == org_id)).first()
            cache[ck] = p.id if p else None
        if cache[ck] is None and (strict or parent is Branch):
            raise SyncError(f"{key} does not exist in this tenant")
        if cache[ck] is None and update:
            continue                     # keep the relation we have rather than writing NULL
        data[fk] = cache[ck]
    return data


def _base_type(annotation: Any) -> tuple[type, ...]:
    args = [a for a in get_args(annotation) if a is not type(None)] or [annotation]
    return tuple(a for a in args if isinstance(a, type))


def _check_types(model: Any, data: dict[str, Any]) -> None:
    """Reject wrongly typed values with a 400 instead of a database error (500)."""
    for k, v in data.items():
        if v is None or k.endswith("__uid") or k in _DT:
            continue
        f = model.model_fields.get(k)
        if f is None:
            continue
        types = _base_type(f.annotation)
        if bool in types:
            ok = isinstance(v, bool)
        elif float in types:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        elif int in types:
            ok = isinstance(v, int) and not isinstance(v, bool)
        elif str in types:
            ok = isinstance(v, str) and len(v) <= (f.metadata[0].max_length if f.metadata and
                                                  hasattr(f.metadata[0], "max_length") else 10_000)
        else:
            ok = True
        if not ok:
            raise SyncError(f"field {k} has a wrong type or is too long")


def _clean(model: Any, row: dict[str, Any], fkmap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep only plain columns. Foreign keys are accepted ONLY as ``<fk>__uid`` (resolved inside
    the tenant by ``_resolve``); a raw integer id would point at whatever row has that id —
    possibly another tenant's — so raw FK keys are dropped, never trusted."""
    allowed = set(model.model_fields) - _LOCAL_ONLY - set(fkmap or {})
    fk_uids = {fk + "__uid" for fk in (fkmap or {})}
    data = {k: v for k, v in row.items() if k in allowed or k in fk_uids}
    for k in _DT & set(data):
        data[k] = _dt(data[k])
    return data


def hub_apply(s: Session, org_id: int, payload: dict[str, Any], device: str, protocol: int = 1) -> dict[str, Any]:
    """Validate the whole push first; apply only if there is no conflict."""
    if not isinstance(payload, dict) or set(payload) - TABLE_NAMES:
        raise SyncError("unknown table in payload")
    total = sum(len(v) for v in payload.values() if isinstance(v, list))
    if total > MAX_ROWS_PER_PUSH:
        raise SyncError("push too large")
    conflicts: list[dict[str, Any]] = []
    current: dict[str, list[Any]] = {}
    plan: list[tuple[str, Any, Any, dict[str, Any], str, Any | None]] = []
    for name, model, fkmap, mode in TABLES:
        rows = payload.get(name) or []
        if not rows:
            continue
        if mode == "pull_only" or not isinstance(rows, list):
            raise SyncError(f"table {name} is not writable from an edge")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("uid"), str) or len(row["uid"]) > 32:
                raise SyncError("row without a valid uid")
            stmt = select(model).where(model.uid == row["uid"])
            if bind_is_postgres(s):
                stmt = stmt.with_for_update()          # serialise against concurrent web edits
            cur = s.exec(stmt).first()
            if cur is not None and cur.org_id != org_id:
                raise SyncError("uid belongs to another tenant")
            base = row.get("base_version", 0)
            if not isinstance(base, int):
                raise SyncError("base_version must be an integer")
            locked = _locked(s, org_id, name, row, cur)
            if locked or (mode == "versioned" and ((cur is None and base != 0) or
                                        (cur is not None and cur.version != base))):
                conflicts.append({"table": name, "uid": row["uid"], "base_version": base,
                                  "hub_version": cur.version if cur else 0})
                if cur is not None:
                    current.setdefault(name, []).append(cur)
                s.add(SyncConflict(org_id=org_id, entity=name, row_uid=row["uid"],
                                   hub_version=cur.version if cur else 0, base_version=base,
                                   incoming=json.dumps(row, default=str)[:8000], device=device[:80]))
                continue
            plan.append((name, model, fkmap, row, mode, cur))
    if conflicts:
        s.commit()                                   # keep the conflict records, apply nothing
        _with_parents(s, org_id, current)           # so the edge can resolve every relation
        return {"conflicts": conflicts, "current": export_rows(s, org_id, current)}

    now = utcnow()
    accepted: dict[str, dict[str, int]] = {}
    cache: dict[tuple[Any, str], int | None] = {}
    touched_orders: set[int] = set()
    renumbered: dict[str, str] = {}
    for name, model, fkmap, row, mode, cur in plan:
        data = _clean(model, row, fkmap)
        for k in HUB_OWNED.get(name, set()) | (INSERT_ONLY.get(name, set()) if cur is not None else set()):
            data.pop(k, None)
        _check_types(model, data)
        _validate_values(name, data)
        data = _resolve(s, org_id, data, fkmap, cache, update=cur is not None, strict=True)
        data.pop("version", None)
        if name == "orders":
            if "paid" in data:
                data["paid_at"] = (cur.paid_at if cur is not None and cur.paid else now) if data["paid"] else None
            if cur is None and _hub_number(s, data):
                renumbered[row["uid"]] = data["number"]
        if cur is None:
            obj = model(**data)
            # protocol 2 devices adopt a hub-assigned number from `renumbered`; an older device only
            # learns it from its next pull, so for it the renumbered row is stored one version ahead
            legacy_bump = name == "orders" and protocol < 2 and row["uid"] in renumbered
            obj.org_id, obj.version, obj.srv_at = org_id, 2 if legacy_bump else 1, now
            s.add(obj)
            s.flush()
            cache[(model, obj.uid)] = obj.id
            accepted.setdefault(name, {})[obj.uid] = 1
            target = obj
        elif mode == "versioned":
            for k, v in data.items():
                if k != "uid":
                    setattr(cur, k, v)
            cur.version, cur.srv_at = cur.version + 1, now
            s.add(cur)
            accepted.setdefault(name, {})[cur.uid] = cur.version
            target = cur
        else:
            continue
        oid = target.id if name == "orders" else getattr(target, "order_id", None)
        if name in ("orders", "work_lines", "part_lines") and oid:
            touched_orders.add(oid)
    # totals are always computed by the hub from the lines — never taken from a device
    from ..services.orders import recompute_totals
    for oid in touched_orders:
        order = s.get(WorkOrder, oid)
        if order is not None and order.org_id == org_id:
            recompute_totals(s, order)
            order.srv_at = now
    s.commit()
    # the device adopts these numbers in mark_accepted, even for rows it edited again meanwhile
    return {"accepted": accepted, "renumbered": renumbered}


def _locked(s: Session, org_id: int, name: str, row: dict[str, Any], cur: Any) -> bool:
    """Rows a device may not change: a fiscalised order and its lines; on a PAID order the money
    fields and the lines (on the web that needs the pricing role, which a device does not have)."""
    if name == "orders" and cur is not None:
        if cur.fiscal_no:
            return True
        return bool(cur.paid and any(k in row and row[k] != getattr(cur, k) for k in MONEY_FIELDS))
    if name in ("work_lines", "part_lines"):
        if cur is not None:                       # the line's CURRENT order decides, not the payload
            order = s.get(WorkOrder, cur.order_id)
        else:
            ouid = row.get("order_id__uid")
            order = s.exec(select(WorkOrder).where(WorkOrder.uid == ouid, WorkOrder.org_id == org_id)).first() \
                if isinstance(ouid, str) else None
        return bool(order is not None and (order.fiscal_no or order.paid))
    return False


def _hub_number(s: Session, data: dict[str, Any]) -> bool:
    """Edges number orders in their own space (PREFIX-TAG00001). A device-supplied number that is
    empty, looks like a hub number or is already taken is replaced by the hub's next number, so
    a push can never poison the branch counter or the unique (branch, number) constraint."""
    num = data.get("number")
    bid = data.get("branch_id")
    taken = isinstance(num, str) and bid is not None and s.exec(
        select(WorkOrder.id).where(WorkOrder.branch_id == bid, WorkOrder.number == num)).first() is not None
    if not isinstance(num, str) or not num or HUB_NUMBER.match(num) or PARKED_NUMBER.search(num) or taken:
        branch = s.get(Branch, bid) if bid is not None else None
        if branch is None:
            raise SyncError("order without a branch")
        from ..services.orders import next_number
        data["number"] = next_number(s, branch)
        return True
    return False


def _with_parents(s: Session, org_id: int, current: dict[str, list[Any]]) -> None:
    """Add the parent rows of conflicting rows to the 409 payload (one level)."""
    for name, _model, fkmap, _mode in reversed(TABLES):
        for obj in list(current.get(name, [])):
            for fk, parent in fkmap.items():
                pid = getattr(obj, fk, None)
                if not pid:
                    continue
                pname = next(n for n, m, _f, _md in TABLES if m is parent)
                if any(p.id == pid for p in current.get(pname, [])):
                    continue
                p = s.get(parent, pid)
                if p is not None and p.org_id == org_id:
                    current.setdefault(pname, []).append(p)


def edge_apply(s: Session, org_id: int, rows: dict[str, list[dict]],
               keep_local: dict[str, set[str]] | None = None) -> int:
    """Adopt hub rows on the edge (the hub is the source of truth).

    * a row whose hub version we already have is skipped (pull windows overlap on purpose);
    * a row listed in ``keep_local`` was edited again while the push was in flight — its
      newer local edit is kept and pushed next cycle;
    * a locally dirty row the hub ALSO changed is saved to ``SyncConflict`` before the hub
      version wins, so no edit disappears silently.
    """
    keep_local = keep_local or {}
    cache: dict[tuple[Any, str], int | None] = {}
    n = 0
    for name, model, fkmap, _mode in TABLES:
        for row in rows.get(name) or []:
            hub_version = int(row.get("version") or 1)
            cur = s.exec(select(model).where(model.uid == row["uid"])).first()
            if cur is not None and (cur.sync_base >= hub_version or row["uid"] in keep_local.get(name, set())):
                continue
            data = _clean(model, row, fkmap)
            if name != "branches":
                data = _resolve(s, org_id, data, fkmap, cache, update=cur is not None, strict=False)
            if name == "orders":
                _free_local_number(s, data, row["uid"])
            if cur is None:
                obj = model(**data)
                obj.org_id, obj.version, obj.sync_base = org_id, hub_version, hub_version
                s.add(obj)
                s.flush()
                cache[(model, obj.uid)] = obj.id
            else:
                if cur.version != cur.sync_base:
                    s.add(SyncConflict(org_id=org_id, entity=name, row_uid=cur.uid,
                                       hub_version=hub_version, base_version=cur.sync_base,
                                       incoming=json.dumps(_dump(cur, fkmap, {}), default=str)[:8000],
                                       device="local"))
                for k, v in data.items():
                    if k != "uid":
                        setattr(cur, k, v)
                cur.version = cur.sync_base = hub_version
                s.add(cur)
            n += 1
    s.commit()
    return n


def _free_local_number(s: Session, data: dict[str, Any], uid: str) -> None:
    """If a different LOCAL order holds the number the hub assigns (e.g. two edges sharing an
    EDGE_TAG), park the local one under a free temporary ``…-DUPnnn`` number instead of failing
    the whole sync on the unique constraint. A never-pushed order is dirty anyway; the hub
    treats a parked number as "assign a real one" and the device adopts it via ``renumbered``.
    The parked row's version is not bumped: that would fake a local edit (and a conflict)."""
    num, bid = data.get("number"), data.get("branch_id")
    if not num or bid is None:
        return
    clash = s.exec(select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.number == num,
                                           WorkOrder.uid != uid)).first()
    if clash is not None:
        clash.number = _free_parked_number(s, bid, str(num))
        s.add(clash)
        s.flush()


def _free_parked_number(s: Session, bid: int, num: str) -> str:
    base = PARKED_NUMBER.sub("", num)[:12]
    for k in range(1, 1000):
        cand = f"{base}-DUP{k:03d}"
        if s.exec(select(WorkOrder.id).where(WorkOrder.branch_id == bid, WorkOrder.number == cand)).first() is None:
            return cand
    raise SyncError("no free temporary number")


def adopt_numbers(s: Session, renumbered: dict[str, str], pushed: set[str]) -> None:
    """Take over the numbers the hub assigned to the orders this device just pushed."""
    for uid, number in (renumbered or {}).items():
        if uid not in pushed or not isinstance(number, str) or not number or len(number) > 20:
            continue
        cur = s.exec(select(WorkOrder).where(WorkOrder.uid == uid)).first()
        if cur is None or cur.number == number:
            continue
        _free_local_number(s, {"number": number, "branch_id": cur.branch_id}, uid)
        cur.number = number
        s.add(cur)
        s.flush()


def mark_accepted(s: Session, accepted: dict[str, dict[str, int]],
                  pushed: dict[str, dict[str, int]]) -> dict[str, set[str]]:
    """Compare-and-set after a successful push. A row edited again while the request was in
    flight (local version moved past what we pushed) only learns its new base and stays
    dirty; it is returned so ``edge_apply`` keeps the newer local edit."""
    moved: dict[str, set[str]] = {}
    for name, model, _fk, _mode in TABLES:
        for uid, ver in (accepted.get(name) or {}).items():
            cur = s.exec(select(model).where(model.uid == uid)).first()
            if cur is None:
                continue
            if cur.version == pushed.get(name, {}).get(uid):
                cur.version = cur.sync_base = ver
            else:
                cur.version = max(cur.version, ver + 1)
                cur.sync_base = ver
                moved.setdefault(name, set()).add(uid)
            s.add(cur)
    s.commit()
    return moved


def bind_is_postgres(s: Session) -> bool:
    return s.get_bind().dialect.name == "postgresql"
