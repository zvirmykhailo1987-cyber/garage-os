"""Postgres row-level security: policies, grants and the runtime role check.

Applied by ``python -m app.manage init`` with the OWNER connection. The web process
connects as a separate, non-owner, non-superuser role, so the policies bind it.
RLS is ENABLEd (not FORCEd): the owner stays exempt, which is what allows the narrow
``SECURITY DEFINER`` aggregate used by the operator console.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

from .models import TENANT_MODELS, Branch, Membership, SyncConflict

_ORG = "NULLIF(current_setting('app.current_org', true), '')::int"


# Identity/structure rows that are only read once the caller's tenant is known.
# (User, ApiToken and SyncDevice stay outside RLS on purpose: login and token lookups happen
# BEFORE a tenant is known and are keyed by an e-mail or an unguessable token hash.)
EXTRA_MODELS = [Branch, Membership, SyncConflict]


def tenant_tables() -> list[str]:
    return [m.__tablename__ for m in [*TENANT_MODELS, *EXTRA_MODELS]]  # type: ignore[attr-defined]


def policy_sql() -> list[str]:
    stmts: list[str] = []
    for t in tenant_tables():
        stmts += [
            f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY',
            f'DROP POLICY IF EXISTS tenant_isolation ON "{t}"',
            f'CREATE POLICY tenant_isolation ON "{t}" '
            f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})",
        ]
    # Operator console: per-org counts without reading any tenant row content.
    stmts.append("DROP FUNCTION IF EXISTS platform_order_counts()")
    stmts.append(
        "CREATE OR REPLACE FUNCTION platform_org_counts() "
        "RETURNS TABLE(org_id int, branches bigint, orders bigint) LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = public AS "
        "$$ SELECT o.id, (SELECT count(*) FROM branch b WHERE b.org_id = o.id), "
        "(SELECT coalesce(sum(n), 0) FROM statuscount c WHERE c.org_id = o.id) FROM organization o $$")
    stmts.append("REVOKE ALL ON FUNCTION platform_org_counts() FROM PUBLIC")
    return stmts


def grant_sql(app_role: str) -> list[str]:
    role = '"' + app_role.replace('"', '') + '"'
    return [
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        f"GRANT EXECUTE ON FUNCTION platform_org_counts() TO {role}",
    ]


def apply(admin: Engine, app_url: str) -> None:
    app_role = make_url(app_url).username or ""
    with admin.begin() as conn:
        for stmt in policy_sql():
            conn.execute(text(stmt))
        if app_role and app_role != make_url(str(admin.url)).username:
            for stmt in grant_sql(app_role):
                conn.execute(text(stmt))


def assert_runtime_role_is_restricted(eng: Engine) -> None:
    """Fail fast if the web process could bypass RLS (superuser, BYPASSRLS or table owner)."""
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT r.rolsuper, r.rolbypassrls, "
            "EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' "
            "        AND tablename='workorder' AND tableowner = current_user) "
            "FROM pg_roles r WHERE r.rolname = current_user")).one()
    if any(row):
        raise RuntimeError(
            "DATABASE_URL role can bypass row-level security (superuser, BYPASSRLS or "
            "table owner). Use a dedicated restricted role — see docker-compose.yml.")
