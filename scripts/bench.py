# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""HTTP page benchmark on a large synthetic database.

Measures whole pages through the ASGI stack (routing → session lookup → tenant scoping →
SQL → Jinja rendering), authenticated as a network owner — not isolated query fragments.
Network latency is excluded (in-process transport); pass --url to hit a running server.

    python scripts/bench.py --orders 200000            # writes docs/benchmarks.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=200_000)
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--out", default=str(ROOT / "docs" / "benchmarks.json"))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sto-bench-"))
    os.environ.update({"STO_ENV": "test", "DATA_DIR": str(tmp), "STO_SECRET": "bench-" + "x" * 40,
                       "DATABASE_URL": f"sqlite:///{tmp / 'bench.sqlite'}"})

    from fastapi.testclient import TestClient
    from sqlalchemy import event, insert, text
    from sqlmodel import Session

    from app.db import create_schema, engine
    from app.main import app
    from app.manage import seed_plans
    from app.models import Branch, Car, Client, Membership, Organization, User, WorkLine, WorkOrder, new_uid
    from app.security import hash_password

    create_schema(engine)
    rng = random.Random(7)
    now = datetime.now(UTC)
    t0 = time.perf_counter()
    with Session(engine) as s:
        seed_plans(s)
        org = Organization(name="Bench Network", slug="bench", plan_code="network", status="active")
        s.add(org)
        s.flush()
        branches = [Branch(org_id=org.id, name=f"Branch {i}", order_prefix=f"B{i}") for i in range(4)]
        s.add_all(branches)
        s.flush()
        u = User(org_id=org.id, email="bench@bench.test", name="Bench", password_hash=hash_password("test-bench-password"))
        s.add(u)
        s.flush()
        s.add(Membership(org_id=org.id, user_id=u.id, role="net_admin"))
        s.commit()
        n_clients = max(1000, args.orders // 10)
        base = {"created_at": now, "updated_at": now, "srv_at": now, "version": 1, "sync_base": 0, "org_id": org.id}
        clients = [{**base, "uid": new_uid(), "name": f"Client {i}", "phone": f"+38000{i:07d}", "kind": "private",
                    "discount": 0.0, "tax_id": "", "note": "", "tg_code_hash": "",
                    "branch_id": branches[i % 4].id} for i in range(n_clients)]
        s.execute(insert(Client), clients)
        cars = [{**base, "uid": new_uid(), "client_id": i + 1, "plate": f"AA{i:06d}", "vin": "", "make": "Skoda",
                 "model": "Octavia", "engine": "diesel", "color": "", "branch_id": branches[i % 4].id}
                for i in range(n_clients)]
        s.execute(insert(Car), cars)
        statuses = ["new", "estimate", "in_progress", "waiting_parts", "done", "paid"]
        orders, lines = [], []
        counters = [1001] * 4
        for i in range(args.orders):
            b = i % 4
            cid = rng.randrange(n_clients) + 1
            ts = now - timedelta(minutes=args.orders - i)
            orders.append({**base, "uid": new_uid(), "created_at": ts, "updated_at": ts, "srv_at": ts,
                           "number": f"B{b}-{counters[b]:06d}", "status": statuses[i % 6], "car_id": cid,
                           "client_id": cid, "box": "", "complaint": "bench", "accepted_at": ts,
                           "discount_pct": 0.0, "vat_pct": 20.0, "pay_method": "none", "paid": i % 6 == 5,
                           "paid_at": ts if i % 6 == 5 else None,
                           "source": "desk", "fiscal_no": "", "works_total": 900.0, "parts_total": 600.0,
                           "discount_total": 0.0, "vat_total": 250.0, "total": 1500.0,
                           "branch_id": branches[b].id})
            counters[b] += 1
            lines.append({**base, "uid": new_uid(), "order_id": i + 1, "service": "Brake pads", "norm_hours": 2.0,
                          "rate": 450.0, "deleted": False, "branch_id": branches[b].id})
        for chunk in range(0, len(orders), 20_000):
            s.execute(insert(WorkOrder), orders[chunk:chunk + 20_000])
            s.execute(insert(WorkLine), lines[chunk:chunk + 20_000])
        s.commit()
        from app.counters import recount
        recount(s)                       # bulk insert bypasses the ORM hooks
        s.exec(text("ANALYZE"))          # planner statistics, as `manage init` does in a deployment
    seed_s = time.perf_counter() - t0

    c = TestClient(app, follow_redirects=False)
    page = c.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert c.post("/login", data={"email": "bench@bench.test", "password": "test-bench-password",
                                  "csrf_token": token}).status_code == 303

    queries = {"n": 0}
    event.listen(engine, "before_cursor_execute", lambda *a, **k: queries.__setitem__("n", queries["n"] + 1))
    mid = args.orders // 2
    pages = {"dashboard": "/", "orders list": "/orders", "orders page 1000": "/orders?page=1000",
             "orders filtered": "/orders?status=waiting_parts", "orders search": f"/orders?q=AA{mid % n_clients:06d}",
             "kanban board": "/kanban", "clients": "/clients", "order card": f"/orders/{mid}",
             "invoice": f"/orders/{mid}/invoice"}
    results = {}
    for name, url in pages.items():
        for _ in range(3):
            assert c.get(url).status_code == 200, url
        times, qs, size = [], [], 0
        for _ in range(args.runs):
            queries["n"] = 0
            t = time.perf_counter()
            r = c.get(url)
            times.append((time.perf_counter() - t) * 1000)
            qs.append(queries["n"])
            size = len(r.content)
        times.sort()
        results[name] = {"url": url, "p50_ms": round(statistics.median(times), 1),
                         "p95_ms": round(times[int(len(times) * 0.95) - 1], 1),
                         "max_ms": round(times[-1], 1), "sql_queries": max(qs), "bytes": size}
        print(f"{name:18} p50 {results[name]['p50_ms']:7.1f} ms   p95 {results[name]['p95_ms']:7.1f} ms   "
              f"{results[name]['sql_queries']} SQL")

    out = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": "in-process ASGI (Starlette TestClient): full authenticated HTTP request incl. session "
                  "lookup, tenant scoping, SQL and template rendering; network excluded",
        "database": "SQLite (WAL) on local disk",
        "dataset": {"orders": args.orders, "clients": n_clients, "vehicles": n_clients, "branches": 4,
                    "seed_seconds": round(seed_s, 1)},
        "runs_per_page": args.runs,
        "machine": {"python": platform.python_version(), "platform": platform.platform(),
                    "cpu_count": os.cpu_count()},
        "pages": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
