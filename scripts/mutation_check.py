# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Break the code on purpose and check that the tests notice. Writes docs/mutation-results.json.

Coverage says a line ran. This says a test fails when the line is wrong. Each mutation removes one
guarantee (tenant filter, RLS context, money maths, optimistic lock, token scope, role check,
lockout, CSRF …) from a temporary copy of the project, and the whole suite runs against that copy —
on SQLite and on Postgres with row-level security. A mutation counts as caught as soon as one test
fails on either backend; one that nothing catches is reported as "survived", never hidden.

Postgres: if ``STO_TEST_DATABASE_URL`` / ``STO_TEST_DATABASE_ADMIN_URL`` are set they are used as is;
otherwise a throwaway ``postgres:17-alpine`` container is started on a free local port (55434 and up)
and removed afterwards. Needs Docker in that case.

    python scripts/mutation_check.py            # all mutations
    python scripts/mutation_check.py --only 3   # one mutation, by 1-based index (debugging)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
SKIP = {".venv", "data", ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".coverage",
        "docs", "coverage.xml", "htmlcov"}

# (what breaks, file, exact original text, replacement). Each removes ONE guarantee.
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── tenant isolation, application layer ──────────────────────────────────
    (
        "Organization-wide rows of every tenant are visible (shared-row filter drops org_id)",
        "app/tenancy.py",
        "cond = cond | ((model.org_id == ctx.org_id) & (model.branch_id.is_(None)))",
        "cond = cond | model.branch_id.is_(None)",
    ),
    (
        "A lookup by id returns any tenant's row",
        "app/tenancy.py",
        "    if bid is None:\n        return include_shared\n    return bid in ctx.branch_ids",
        "    if bid is None:\n        return include_shared\n    return True",
    ),
    (
        "can_access no longer compares the row's organization",
        "app/tenancy.py",
        'if obj is None or ctx.org_id is None or getattr(obj, "org_id", None) != ctx.org_id:',
        "if obj is None or ctx.org_id is None:",
    ),
    (
        "Order lookup by number ignores the tenant",
        "app/tenancy.py",
        "    stmt = select(WorkOrder).where(WorkOrder.org_id == org_id,\n"
        "                                   WorkOrder.number == number.strip().upper())",
        "    stmt = select(WorkOrder).where(WorkOrder.number == number.strip().upper())",
    ),
    # ── tenant isolation, Postgres RLS ────────────────────────────────────────
    (
        "The RLS tenant setting is never sent to Postgres",
        "app/db.py",
        '    connection.execute(_SET_ORG, {"org": str(org) if org else ""})',
        '    connection.execute(_SET_ORG, {"org": ""})',
    ),
    (
        "REST requests run without binding the tenant",
        "app/api/deps.py",
        "    set_tenant(s, tok.org_id)\n",
        "",
    ),
    (
        "Branches and memberships are left outside RLS",
        "app/rls.py",
        "EXTRA_MODELS = [Branch, Membership, SyncConflict]",
        "EXTRA_MODELS = [SyncConflict]",
    ),
    (
        "Startup accepts a database role that bypasses RLS",
        "app/rls.py",
        "    if any(row):\n",
        "    if False:\n",
    ),
    # ── money ─────────────────────────────────────────────────────────────────
    (
        "VAT is added on top instead of extracted from the price",
        "app/models/workorder.py",
        "vat = round(total - total / (1 + (vat_pct or 0) / 100.0), 2)",
        "vat = round(total * (vat_pct or 0) / 100.0, 2)",
    ),
    (
        "A device push keeps the totals it was sent instead of recomputing them",
        "app/sync/engine.py",
        "            recompute_totals(s, order)\n",
        "            pass\n",
    ),
    (
        "An LLM draft may carry prices for parts",
        "ai/schemas/work-order-draft.schema.json",
        '"type": "object", "additionalProperties": false,\n        "required": ["name", "qty"],',
        '"type": "object", "additionalProperties": true,\n        "required": ["name", "qty"],',
    ),
    (
        "A mechanic may type a price",
        "app/permissions.py",
        '"orders.pricing": {NA, BM, RC},',
        '"orders.pricing": ALL,',
    ),
    (
        "A fiscalised order's lines can still be changed",
        "app/web/orders.py",
        "    if o.fiscal_no:\n        raise HTTPException(status_code=409, detail=\"err.order_locked\")\n    if money or o.paid:",
        "    if money or o.paid:",
    ),
    # ── optimistic locking and sync conflicts ─────────────────────────────────
    (
        "The order form ignores the expected version",
        "app/web/orders.py",
        "    if osvc.parse_int(expected_version) != o.version:            # optimistic lock",
        "    if False:            # optimistic lock",
    ),
    (
        "A board drag ignores the expected version",
        "app/web/orders.py",
        "    if not isinstance(exp, int) or exp != o.version:",
        "    if not isinstance(exp, int):",
    ),
    (
        "A stale device push overwrites the hub (last write wins)",
        "app/sync/engine.py",
        "(cur is not None and cur.version != base))):",
        "(cur is not None and False))):",
    ),
    (
        "A refused push loses its conflict records",
        "app/sync/engine.py",
        "        s.commit()                                   # keep the conflict records, apply nothing",
        "        s.rollback()                                 # keep the conflict records, apply nothing",
    ),
    (
        "A device may change the money of a paid order",
        "app/sync/engine.py",
        "        return bool(cur.paid and any(k in row and row[k] != getattr(cur, k) for k in MONEY_FIELDS))",
        "        return False",
    ),
    # ── device and API token scope ────────────────────────────────────────────
    (
        "A revoked sync device keeps syncing",
        "app/sync/routes.py",
        "    if dev is None or not dev.active:",
        "    if dev is None:",
    ),
    (
        "A device push may overwrite a row of another tenant by uid",
        "app/sync/engine.py",
        "            if cur is not None and cur.org_id != org_id:\n"
        '                raise SyncError("uid belongs to another tenant")\n',
        "",
    ),
    (
        "A parent uid from another tenant resolves",
        "app/sync/engine.py",
        "            p = s.exec(select(parent).where(parent.uid == puid, parent.org_id == org_id)).first()",
        "            p = s.exec(select(parent).where(parent.uid == puid)).first()",
    ),
    (
        "An API token works outside its scopes",
        "app/api/deps.py",
        "        if scope not in p.scopes:\n",
        "        if False:\n",
    ),
    (
        "A revoked or expired API token keeps working",
        "app/api/deps.py",
        "    if tok is None or not tok.active or (tok.expires_at and tok.expires_at < now):",
        "    if tok is None:",
    ),
    (
        "A bot token reads orders of clients its chat is not linked to",
        "app/api/routes.py",
        "        if link is None or link.client_id != o.client_id:",
        "        if False:",
    ),
    # ── roles and plans ───────────────────────────────────────────────────────
    (
        "A receptionist may issue fiscal receipts",
        "app/permissions.py",
        '"orders.fiscal": {NA, BM},',
        '"orders.fiscal": {NA, BM, RC},',
    ),
    (
        "A suspended subscription can still write",
        "app/tenancy.py",
        "        if perm in WRITE_PERMISSIONS and not self.writable:\n            return False\n",
        "",
    ),
    (
        "A line of another order can be deleted through this order's URL (IDOR)",
        "app/web/orders.py",
        "    if wl is None or wl.order_id != o.id or wl.org_id != o.org_id or wl.deleted:    # IDOR guard",
        "    if wl is None or wl.org_id != o.org_id or wl.deleted:    # IDOR guard",
    ),
    # ── authentication ────────────────────────────────────────────────────────
    (
        "A locked account can still sign in",
        "app/web/auth.py",
        "    if is_locked(user):                      # same answer as a wrong password: no account oracle",
        "    if False:                      # same answer as a wrong password: no account oracle",
    ),
    (
        "Failed logins are not counted towards the lockout",
        "app/web/auth.py",
        "        register_failed_login(user)\n",
        "",
    ),
    (
        "A logged-out (revoked) session keeps working",
        "app/security.py",
        "    if sess is None or sess.revoked_at is not None:",
        "    if sess is None:",
    ),
    (
        "An idle session never expires",
        "app/security.py",
        "    if now >= sess.expires_at or now - sess.last_seen_at > timedelta(minutes=config.SESSION_IDLE_MINUTES):",
        "    if now >= sess.expires_at:",
    ),
    # ── CSRF ──────────────────────────────────────────────────────────────────
    (
        "Signed-in forms accept a missing or forged CSRF token",
        "app/web/deps.py",
        "    if request.method in UNSAFE and not tokens_match(sess.csrf_token, await _submitted_csrf(request)):",
        "    if False:",
    ),
    (
        "Login and sign-up accept a missing or forged CSRF token",
        "app/web/deps.py",
        "        if not pre or not tokens_match(pre_csrf_token(pre), await _submitted_csrf(request)):",
        "        if False:",
    ),
]


def copy_project(target: Path) -> None:
    shutil.copytree(ROOT, target, ignore=lambda _d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        raise SystemExit("docker is not on PATH: start Postgres yourself and set STO_TEST_DATABASE_URL")
    return path


def _free_port(start: int = 55434) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port for the throwaway Postgres")


@contextlib.contextmanager
def postgres() -> Iterator[dict[str, str]]:
    """Use the configured test database, or start (and always remove) a throwaway one."""
    if os.environ.get("STO_TEST_DATABASE_URL"):
        yield {"STO_TEST_DATABASE_URL": os.environ["STO_TEST_DATABASE_URL"],
               "STO_TEST_DATABASE_ADMIN_URL": os.environ.get("STO_TEST_DATABASE_ADMIN_URL",
                                                             os.environ["STO_TEST_DATABASE_URL"])}
        return
    port, name = _free_port(), f"garageos-mutation-{uuid.uuid4().hex[:8]}"
    subprocess.run([_docker(), "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_DB=sto",
                    "-e", "POSTGRES_USER=sto_owner", "-e", "POSTGRES_PASSWORD=mut-owner",
                    "-p", f"127.0.0.1:{port}:5432", "postgres:17-alpine"], check=True, capture_output=True)
    try:
        for _ in range(60):
            ready = subprocess.run([_docker(), "exec", name, "pg_isready", "-U", "sto_owner", "-d", "sto"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        role = "CREATE ROLE sto_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'mut-app';"
        for _ in range(10):          # pg_isready can answer during the init restart
            if subprocess.run([_docker(), "exec", name, "psql", "-U", "sto_owner", "-d", "sto",
                               "-v", "ON_ERROR_STOP=1", "-c", role], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            raise SystemExit("could not create the restricted role in the throwaway Postgres")
        yield {"STO_TEST_DATABASE_URL": f"postgresql+psycopg://sto_app:mut-app@127.0.0.1:{port}/sto",
               "STO_TEST_DATABASE_ADMIN_URL": f"postgresql+psycopg://sto_owner:mut-owner@127.0.0.1:{port}/sto"}
    finally:
        subprocess.run([_docker(), "rm", "-f", name], capture_output=True)


def run_tests(cwd: Path, pg_env: dict[str, str] | None) -> tuple[bool, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STO_TEST_DATABASE")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", **(pg_env or {}))
    done = subprocess.run(
        [PYTHON, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    found = re.findall(r"^FAILED (\S+)", done.stdout, re.M) or re.findall(r"^ERROR (\S+)", done.stdout, re.M)
    return done.returncode == 0, found[0] if found else ""


def run_both(cwd: Path, pg_env: dict[str, str]) -> tuple[bool, str]:
    """The whole suite on SQLite, then on Postgres; stops at the first failing test."""
    for backend, env in (("sqlite", None), ("postgres", pg_env)):
        passed, caught_by = run_tests(cwd, env)
        if not passed:
            return False, f"{backend}: {caught_by}"
    return True, ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--only", type=int, help="run one mutation (1-based index) and do not write results")
    args = ap.parse_args()
    chosen = list(enumerate(MUTATIONS, 1))
    if args.only:
        chosen = [chosen[args.only - 1]]
    results = []
    with tempfile.TemporaryDirectory() as tmp, postgres() as pg_env:
        work = Path(tmp) / "copy"
        copy_project(work)
        for _i, (name, relative, old, _new) in chosen:      # every mutation must still apply
            if old not in (work / relative).read_text(encoding="utf-8"):
                raise SystemExit(f"Mutation no longer applies, fix the script: {name}")
        passed, why = run_both(work, pg_env)
        if not passed:
            raise SystemExit(f"The unmodified copy does not pass ({why}), so nothing here could be trusted.")
        for i, (name, relative, old, new) in chosen:
            path = work / relative
            original = path.read_text(encoding="utf-8")
            path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            started = time.monotonic()
            passed, caught_by = run_both(work, pg_env)
            path.write_text(original, encoding="utf-8", newline="\n")
            results.append({"bug": name, "file": relative, "caught": not passed,
                            "caught_by": caught_by if not passed else None})
            print(f"{i:2d} {'caught  ' if not passed else 'SURVIVED'} {name} - {caught_by} "
                  f"({time.monotonic() - started:.0f}s)", flush=True)
    caught = sum(row["caught"] for row in results)
    print(caught, "of", len(results), "injected bugs caught")
    if args.only:
        return
    summary = {"measured": dt.date.today().isoformat(), "backends": ["sqlite", "postgres-17-rls"],
               "bugs_injected": len(results), "caught": caught, "results": results}
    (ROOT / "docs" / "mutation-results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                                        encoding="utf-8", newline="\n")
    if caught != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
