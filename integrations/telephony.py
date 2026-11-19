"""Telephony (CTI). The stub logs the incoming call and recognises the caller among the
tenant's own clients. A paid PBX webhook is a seam."""
from __future__ import annotations

from typing import Any

from sqlmodel import select

from app import config
from app.models import CallEvent, Client

from .base import SeamNotConnected, TelephonyPort


class TelephonyStub:
    provider = "stub"

    def on_incoming(self, session: Any, phone: str, org_id: int,
                    branch_id: int | None) -> dict:
        phone = (phone or "").strip()[:40]
        stmt = select(Client).where(Client.org_id == org_id, Client.phone == phone)
        if branch_id is not None:
            stmt = stmt.where(Client.branch_id == branch_id)
        client = session.exec(stmt).first()
        ev = CallEvent(direction="in", phone=phone, org_id=org_id, branch_id=branch_id,
                       client_id=client.id if client else None, provider="stub")
        session.add(ev)
        session.commit()
        return {"event_uid": ev.uid, "client_id": client.id if client else None,
                "client_name": client.name if client else None, "provider": "stub"}


class PbxProvider:
    provider = "pbx"

    def on_incoming(self, session: Any, phone: str, org_id: int,
                    branch_id: int | None) -> dict:
        raise SeamNotConnected("PBX webhook credentials are not configured")


def get_telephony() -> TelephonyPort:
    return PbxProvider() if config.TELEPHONY_PROVIDER == "pbx" else TelephonyStub()
