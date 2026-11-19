# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""REST API: bearer tokens, scopes, tenant scoping, Telegram chat binding."""
from datetime import timedelta

from sqlmodel import select

from app.models import ApiToken, Client, TelegramLink, WorkOrder, utcnow
from tests.conftest import Browser, admin_session, as_user, make_token


def api(path, token=None, method="GET", **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    c = Browser().c
    return c.request(method, path, headers=headers, **kw)


def test_health_is_public_and_minimal():
    assert api("/api/v1/health").json() == {"ok": True}
    assert api("/healthz").json() == {"ok": True}


def test_token_required_and_validated(world):
    assert api(f"/api/v1/orders/{world['numberA']}/status").status_code == 401
    assert api(f"/api/v1/orders/{world['numberA']}/status", "sto_bogus").status_code == 401
    revoked = make_token(world["orgA"], "orders:read", active=False)
    assert api(f"/api/v1/orders/{world['numberA']}/status", revoked).status_code == 401
    expired = make_token(world["orgA"], "orders:read", expires_at=utcnow() - timedelta(days=1))
    assert api(f"/api/v1/orders/{world['numberA']}/status", expired).status_code == 401


def test_scope_enforced(world):
    tok = make_token(world["orgA"], "orders:read")
    assert api(f"/api/v1/estimates/{world['numberA']}/approve", tok, "POST").status_code == 403
    assert api(f"/api/v1/orders/{world['numberA']}/status", tok).status_code == 200


def test_cross_tenant_order_is_404(world):
    """Regression: REST _find_order used to search by number across all tenants."""
    tok = make_token(world["orgA"], "orders:read,estimates:approve")
    assert api(f"/api/v1/orders/{world['numberB']}/status", tok).status_code == 404
    assert api(f"/api/v1/estimates/{world['numberB']}/approve", tok, "POST").status_code == 404
    with admin_session() as s:
        assert s.get(WorkOrder, world["orderB"]).status == "estimate"


def test_branch_bound_token_sees_only_its_branch(world):
    tok = make_token(world["orgA"], "orders:read", branch_id=world["a2"])
    assert api(f"/api/v1/orders/{world['numberA']}/status", tok).status_code == 404
    assert api(f"/api/v1/orders/{world['numberA2']}/status", tok).status_code == 200


def test_bot_token_only_sees_orders_of_linked_chat(world):
    bot = make_token(world["orgA"], "orders:read,estimates:approve,telegram:link", subject="bot")
    url = f"/api/v1/orders/{world['numberA']}/status"
    assert api(url, bot).status_code == 404                                # no chat bound
    assert api(url + "?chat_id=42", bot).status_code == 404
    owner = as_user("owner@a.test")
    page = owner.post(f"/clients/{world['clientA']}/telegram-code").text
    import re
    code = re.search(r"/start ([A-Z0-9]{8})", page).group(1)
    assert api("/api/v1/telegram/link", bot, "POST", json={"code": "WRONG123", "chat_id": "42"}).status_code == 404
    r = api("/api/v1/telegram/link", bot, "POST", json={"code": code, "chat_id": "42"})
    assert r.status_code == 200 and r.json()["linked"] is True
    assert api("/api/v1/telegram/link", bot, "POST", json={"code": code, "chat_id": "43"}).status_code == 404  # one-time
    assert api(url + "?chat_id=42", bot).json()["status"] == "estimate"
    assert api(f"/api/v1/orders/{world['numberA2']}/status?chat_id=42", bot).status_code == 404  # other client
    r = api(f"/api/v1/estimates/{world['numberA']}/approve?chat_id=42", bot, "POST")
    assert r.json() == {"number": world["numberA"], "status": "in_progress"}
    assert api(f"/api/v1/estimates/{world['numberA']}/approve?chat_id=42", bot, "POST").status_code == 409
    with admin_session() as s:
        assert s.exec(select(TelegramLink)).one().client_id == world["clientA"]
        assert s.get(Client, world["clientA"]).tg_code_hash == ""


def test_telegram_code_from_other_org_cannot_be_used(world):
    owner_b = as_user("owner@b.test")
    import re
    code = re.search(r"/start ([A-Z0-9]{8})", owner_b.post(f"/clients/{world['clientB']}/telegram-code").text).group(1)
    bot_a = make_token(world["orgA"], "telegram:link", subject="bot")
    assert api("/api/v1/telegram/link", bot_a, "POST", json={"code": code, "chat_id": "7"}).status_code == 404


def test_fleet_endpoint_scoped(world):
    tok = make_token(world["orgA"], "fleet:read")
    assert api(f"/api/v1/fleet/{world['fleetB']}/vehicles", tok).status_code == 404
    r = api(f"/api/v1/fleet/{world['fleetA']}/vehicles", tok)
    assert r.status_code == 200 and r.json()["vehicles"][0]["unit_no"] == "A-1"


def test_ai_draft_endpoint_validates_body(world):
    tok = make_token(world["orgA"], "ai:draft")
    assert api("/api/v1/ai/work-order-draft", tok, "POST", json={"text": ""}).status_code == 422
    r = api("/api/v1/ai/work-order-draft", tok, "POST", json={"text": "Passat AA 1234 BC brake pads"})
    assert r.status_code == 200 and r.json()["draft"]["vehicle"]["plate"] == "AA 1234 BC"


def test_telephony_webhook_matches_only_own_clients(world):
    tok = make_token(world["orgA"], "telephony:write")
    with admin_session() as s:
        phone_b = s.get(Client, world["clientB"]).phone
        phone_a = s.get(Client, world["clientA"]).phone
    assert api("/api/v1/webhooks/telephony", tok, "POST", json={"phone": phone_b}).json()["client_id"] is None
    assert api("/api/v1/webhooks/telephony", tok, "POST", json={"phone": phone_a}).json()["client_id"] == world["clientA"]


def test_tokens_created_in_ui_are_hashed_and_shown_once(world):
    b = as_user("owner@a.test")
    r = b.post("/settings/tokens", {"label": "erp", "kind": "integration", "scopes": ["orders:read", "bogus:scope"]})
    import re
    raw = re.search(r'class="secret-val">(sto_[^<]+)<', r.text).group(1)
    assert "?secret=" not in r.text
    with admin_session() as s:
        tok = s.exec(select(ApiToken)).one()
        assert tok.token_hash != raw and tok.scopes == "orders:read" and tok.org_id == world["orgA"]
    assert raw not in b.get("/settings").text
    assert api(f"/api/v1/orders/{world['numberA']}/status", raw).status_code == 200
    b.post(f"/settings/tokens/{tok.id}/revoke")
    assert api(f"/api/v1/orders/{world['numberA']}/status", raw).status_code == 401
    assert as_user("owner@b.test").post(f"/settings/tokens/{tok.id}/revoke").status_code == 404
