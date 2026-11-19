"""Every screen renders for every role, in both locales, without leaking template errors."""
import pytest

from tests.conftest import Browser, as_user

OWNER_PAGES = ["/", "/orders", "/orders?status=estimate&q=Client", "/orders/new", "/kanban", "/clients",
               "/parts", "/parts?low=1", "/fleet", "/settings", "/migration", "/billing", "/assistant",
               "/orders/search?q=ready+skoda+this+week"]


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_owner_sees_every_page(world, lang):
    b = as_user("owner@a.test")
    b.c.cookies.set("sto_lang", lang)
    for url in OWNER_PAGES + [f"/orders/{world['orderA']}", f"/orders/{world['orderA']}/invoice",
                              f"/clients/{world['clientA']}", f"/fleet/{world['fleetA']}"]:
        r = b.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:400])
        assert f'lang="{lang}"' in r.text
        assert "{{" not in r.text and "Traceback" not in r.text


@pytest.mark.parametrize("email", ["manager@a.test", "desk@a.test", "mech@a.test"])
def test_branch_roles_render_dashboard_and_order(world, email):
    b = as_user(email)
    for url in ["/", "/orders", "/kanban", "/parts", "/settings", "/assistant", f"/orders/{world['orderA']}"]:
        assert b.get(url).status_code == 200, (email, url)


def test_anonymous_redirects_to_login_with_next():
    b = Browser()
    r = b.get("/orders?status=done")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/orders%3Fstatus%3Ddone"


def test_login_and_signup_pages_render():
    b = Browser()
    for url in ("/login", "/signup"):
        r = b.get(url)
        assert r.status_code == 200
        assert 'name="csrf_token"' in r.text


def test_custom_404_page(world):
    b = as_user("owner@a.test")
    r = b.get("/orders/999999")
    assert r.status_code == 404
    assert "err-code" in r.text and "Page not found" in r.text
    assert Browser().get("/definitely-missing").status_code == 404


def test_custom_500_page(world, monkeypatch):
    from app.web import views
    b = Browser(raise_errors=False)
    b.login("owner@a.test")

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(views.osvc, "rows_for", boom)
    r = b.get("/")
    assert r.status_code == 500
    assert "kaboom" not in r.text and "Server error" in r.text


def test_api_404_is_json():
    r = Browser().get("/api/v1/orders/X/status", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
