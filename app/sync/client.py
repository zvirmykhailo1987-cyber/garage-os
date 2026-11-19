"""Edge-side sync loop (runs only where ``HUB_URL`` is set).

Each cycle: push dirty rows → on 200 mark them clean and adopt the pull delta; on 409
record the conflicts, adopt the hub's current rows for them and retry the rest next
cycle. Network errors are logged to ``SyncMeta`` and never crash the app.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
from sqlmodel import Session, col, select

from .. import config
from ..db import engine, set_tenant, write_intent
from ..models import Organization, SyncConflict, SyncMeta, utcnow
from .engine import TABLES, adopt_numbers, dirty_rows, edge_apply, mark_accepted

PROTOCOL = 2          # 2: the device adopts hub-assigned order numbers from `renumbered`

log = logging.getLogger("sto.sync")
_started = False


def _meta(s: Session, key: str) -> str | None:
    m = s.get(SyncMeta, key)
    return m.value if m else None


def _set_meta(s: Session, key: str, value: str) -> None:
    m = s.get(SyncMeta, key) or SyncMeta(key=key)
    m.value = value[:200]
    s.add(m)


def _post(transport: httpx.BaseTransport | None, hub_url: str, token: str, body: dict) -> httpx.Response:
    with httpx.Client(transport=transport, timeout=40) as http:
        return http.post(f"{hub_url}/sync", json=body, headers={"Authorization": f"Bearer {token}"})


def _json(r: httpx.Response) -> Any:
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def _settle_orphans(s: Session, org_id: int, conflicts: list[dict]) -> None:
    """A refused row the hub does not have at all (e.g. a new line on an order the hub has
    since fiscalised) would be pushed — and refused — forever, blocking every other edit.
    Keep the attempt in SyncConflict, take it off the push queue, and soft-delete lines."""
    for c in conflicts:
        if c.get("hub_version"):
            continue
        model = next((m for n, m, _fk, _mode in TABLES if n == c.get("table")), None)
        if model is None:
            continue
        cur = s.exec(select(model).where(model.uid == c.get("uid"))).first()
        if cur is None:
            continue
        s.add(SyncConflict(org_id=org_id, entity=c["table"], row_uid=cur.uid, hub_version=0,
                           base_version=cur.sync_base, device="local",
                           incoming="refused by the hub; kept here for review"))
        if hasattr(cur, "deleted"):
            cur.deleted = True
        cur.sync_base = cur.version
        s.add(cur)


def sync_once(transport: httpx.BaseTransport | None = None, *, eng: Any = None,
              hub_url: str = "", token: str = "") -> tuple[bool, str]:
    """One push/pull cycle. ``transport``/``eng``/``hub_url``/``token`` are injectable for tests.

    The local read transaction is closed BEFORE the network round trip, so the edge's own web
    requests are never blocked by a slow hub; edits made meanwhile are handled by the
    compare-and-set in ``mark_accepted``. Whatever happens to the push, the edge still pulls."""
    hub_url = hub_url or config.HUB_URL
    token = token or config.SYNC_DEVICE_TOKEN
    if not hub_url or not token:
        return False, "sync not configured"
    with Session(eng or engine) as s:
        org = s.exec(select(Organization).order_by(col(Organization.id))).first()
        if org is None or org.id is None:
            return False, "edge has no organization"
        org_id = org.id
        set_tenant(s, org_id)
        changes = dirty_rows(s, org_id)
        pushed = {name: {r["uid"]: r["version"] for r in rows} for name, rows in changes.items()}
        since = _meta(s, "pull_cursor")
        s.commit()
        try:
            r = _post(transport, hub_url, token, {"since": since, "changes": changes, "protocol": PROTOCOL})
        except httpx.HTTPError as e:
            write_intent(s)
            _set_meta(s, "last_error", f"{utcnow().isoformat()} {e}")
            s.commit()
            return False, str(e)
        data: Any = _json(r)
        write_intent(s)
        if r.status_code == 200:
            adopt_numbers(s, data.get("renumbered", {}), set(pushed.get("orders", {})))
            moved = mark_accepted(s, data.get("accepted", {}), pushed)
            edge_apply(s, org_id, data.get("changes", {}), keep_local=moved)
            _set_meta(s, "pull_cursor", data.get("now", ""))
            _set_meta(s, "last_ok", utcnow().isoformat())
            _set_meta(s, "last_error", "")
            s.commit()
            return True, "ok"
        if r.status_code == 409:
            edge_apply(s, org_id, data.get("current", {}))
            _settle_orphans(s, org_id, data.get("conflicts", []))
            outcome = "conflict"
            _set_meta(s, "last_error", f"{len(data.get('conflicts', []))} conflict(s) recorded")
        else:
            outcome = f"HTTP {r.status_code}"
            _set_meta(s, "last_error", f"{outcome}: {str(data.get('detail', ''))[:120]}")
        s.commit()
        try:                                   # keep receiving hub changes even when our push is refused
            pr = _post(transport, hub_url, token, {"since": since, "changes": {}})
            if pr.status_code == 200:
                write_intent(s)
                pdata = _json(pr)
                edge_apply(s, org_id, pdata.get("changes", {}))
                _set_meta(s, "pull_cursor", pdata.get("now", ""))
                s.commit()
        except httpx.HTTPError:
            pass
        return False, outcome


def _loop() -> None:
    while True:
        try:
            sync_once()
        except Exception:                      # a sync failure must never kill the app
            log.exception("sync cycle failed")
        time.sleep(config.SYNC_INTERVAL)


def start() -> None:
    global _started
    if _started or not config.HUB_URL:
        return
    _started = True
    threading.Thread(target=_loop, name="sto-sync", daemon=True).start()
