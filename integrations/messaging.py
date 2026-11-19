"""Customer messages through a transactional outbox.

The stub writes the message with status ``SIMULATED`` (visible in the order card, never
sent). Sending to a customer is Tier 3, so the route asks for explicit confirmation.
"""
from __future__ import annotations

from typing import Any

from app import config
from app.models import MSG_SIMULATED, OutboxMessage

from .base import MessengerPort, SeamNotConnected


class MessengerStub:
    provider = "stub"

    def send(self, session: Any, *, to: str, text: str, channel: str, org_id: int,
             branch_id: int | None, order_id: int | None,
             client_id: int | None) -> dict:
        msg = OutboxMessage(channel=channel, to=to, text=text[:1000], status=MSG_SIMULATED,
                            provider="stub", org_id=org_id, branch_id=branch_id,
                            order_id=order_id, client_id=client_id)
        session.add(msg)
        return {"uid": msg.uid, "status": MSG_SIMULATED, "provider": "stub"}


class SmsGatewayProvider:
    provider = "sms-gateway"

    def send(self, session: Any, *, to: str, text: str, channel: str, org_id: int,
             branch_id: int | None, order_id: int | None,
             client_id: int | None) -> dict:
        raise SeamNotConnected("SMS gateway API key is not configured")


def get_messenger() -> MessengerPort:
    return SmsGatewayProvider() if config.SMS_PROVIDER == "gateway" else MessengerStub()
