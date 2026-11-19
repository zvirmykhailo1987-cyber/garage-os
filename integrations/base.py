# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Integration ports.

One pattern per channel: a ``Protocol`` (the port), a free offline ``Stub`` that works
out of the box and records what it would do, and a paid provider class that documents
the seam but refuses to run until it is configured (``SeamNotConnected``).
Every port receives the caller's tenant (``org_id``) — no adapter queries across tenants.
"""
from __future__ import annotations

from typing import Any, Protocol


class SeamNotConnected(RuntimeError):
    """A paid provider is described but not connected (needs a key / contract)."""


class PlateLookupPort(Protocol):
    def lookup(self, session: Any, plate: str, org_id: int,
               branch_ids: list[int] | None = None) -> dict | None: ...


class TelephonyPort(Protocol):
    def on_incoming(self, session: Any, phone: str, org_id: int,
                    branch_id: int | None) -> dict: ...


class MessengerPort(Protocol):
    def send(self, session: Any, *, to: str, text: str, channel: str, org_id: int,
             branch_id: int | None, order_id: int | None,
             client_id: int | None) -> dict: ...


class FiscalPort(Protocol):
    def post_receipt(self, session: Any, order: Any, total: float) -> dict: ...
