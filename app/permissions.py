"""Role → permission matrix. Deny by default: a permission not listed here is refused.

The matrix is enforced on the ROUTE (server side). Templates only mirror it to hide
buttons the user could not use anyway.
"""
from __future__ import annotations

from .models import ROLE_BRANCH_MANAGER as BM
from .models import ROLE_MASTER as MS
from .models import ROLE_NET_ADMIN as NA
from .models import ROLE_RECEPTIONIST as RC

ALL = {NA, BM, RC, MS}

PERMISSIONS: dict[str, set[str]] = {
    "orders.view": ALL,
    "orders.create": {NA, BM, RC},
    "orders.edit": ALL,                    # status, labour and part lines, vehicle data
    "orders.pricing": {NA, BM, RC},        # discount, payment method, paid flag
    "orders.notify": {NA, BM, RC},         # message the customer (Tier 3)
    "orders.fiscal": {NA, BM},             # fiscal receipt (Tier 3, irreversible)
    "clients.view": {NA, BM, RC},
    "clients.edit": {NA, BM, RC},
    "parts.view": ALL,
    "parts.edit": {NA, BM},
    "catalog.edit": {NA, BM},              # mechanics and services
    "fleet.view": {NA, BM, RC},
    "fleet.acts": {NA, BM},
    "ai.use": ALL,
    "billing.view": {NA, BM},
    "team.manage": {NA},                   # users, branches
    "tokens.manage": {NA},                 # API tokens, sync devices
    "migration.run": {NA},
}

# Permissions that change data — refused while the subscription is suspended/expired.
WRITE_PERMISSIONS = {p for p in PERMISSIONS if not p.endswith(".view")} - {"ai.use", "tokens.manage"}


def role_allows(role: str | None, perm: str) -> bool:
    return bool(role) and role in PERMISSIONS.get(perm, set())
