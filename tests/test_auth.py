"""Passwords (argon2id + legacy rehash), sessions (expiry, revocation), lockout, rate limits."""
from datetime import timedelta

from sqlmodel import select

from app import config
from app.models import User, UserSession, utcnow
from app.security import SESSION_COOKIE, legacy_hash, verify_password
from tests.conftest import PASSWORD, Browser, admin_session, as_user


def test_login_sets_hardened_session_cookie(world):
    b = Browser()
    r = b.login("owner@a.test")
    cookie = r.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=" in cookie
    with admin_session() as s:
        sess = s.exec(select(UserSession)).one()
        raw = b.c.cookies.get(SESSION_COOKIE)
        assert raw and sess.id != raw and len(sess.id) == 64        # only the hash is stored
        assert sess.expires_at > utcnow()


def test_passwords_are_argon2id(world):
    with admin_session() as s:
        u = s.exec(select(User).where(User.email == "owner@a.test")).one()
        assert u.password_hash.startswith("$argon2id$")


def test_legacy_sha256_hash_is_rehashed_on_login(world):
    with admin_session() as s:
        u = s.exec(select(User).where(User.email == "desk@a.test")).one()
        u.password_hash = legacy_hash(PASSWORD, config.SECRET)
        s.add(u)
        s.commit()
    as_user("desk@a.test")
    with admin_session() as s:
        u = s.exec(select(User).where(User.email == "desk@a.test")).one()
        assert u.password_hash.startswith("$argon2id$")
        assert verify_password(u.password_hash, PASSWORD) == (True, False)


def test_wrong_password_and_unknown_user_look_identical(world):
    b = Browser()
    r1 = b.login("owner@a.test", "wrong-password-1", expect_ok=False)
    r2 = b.login("nobody@a.test", "wrong-password-1", expect_ok=False)
    assert r1.status_code == r2.status_code == 401
    assert "Wrong e-mail or password" in r1.text and "Wrong e-mail or password" in r2.text


def test_account_lockout_after_repeated_failures(world):
    b = Browser()
    for _ in range(config.LOGIN_MAX_FAILURES):
        assert b.login("owner@a.test", "nope-nope-nope", expect_ok=False).status_code == 401
    r = b.login("owner@a.test", PASSWORD, expect_ok=False)       # correct password, still locked
    assert r.status_code == 401 and "sto_session" not in r.headers.get("set-cookie", "")
    # the locked answer is identical to "wrong password" — it does not reveal that the e-mail exists
    unknown = Browser().login("ghost@a.test", "nope-nope-nope", expect_ok=False)
    assert unknown.status_code == r.status_code and unknown.text.count("err") == r.text.count("err")


def test_ip_rate_limit_on_login(world):
    b = Browser()
    codes = [b.login("x@a.test", "bad-bad-bad", expect_ok=False).status_code for _ in range(12)]
    assert codes[-1] == 429 and 401 in codes


def test_logout_revokes_server_session(world):
    b = as_user("owner@a.test")
    raw = b.c.cookies.get(SESSION_COOKIE)
    assert b.post("/logout").status_code == 303
    thief = Browser()
    thief.c.cookies.set(SESSION_COOKIE, raw)                   # replay the stolen cookie
    assert thief.get("/").status_code == 303                    # → login, session is revoked


def test_logout_requires_csrf(world):
    b = as_user("owner@a.test")
    assert b.post("/logout", csrf=False).status_code == 403
    assert b.get("/").status_code == 200


def test_expired_and_idle_sessions_are_rejected(world):
    b = as_user("owner@a.test")
    with admin_session() as s:
        sess = s.exec(select(UserSession)).one()
        sess.expires_at = utcnow() - timedelta(seconds=1)
        s.add(sess)
        s.commit()
    assert b.get("/").status_code == 303
    b2 = as_user("owner@a.test")
    with admin_session() as s:
        for sess in s.exec(select(UserSession)).all():
            sess.last_seen_at = utcnow() - timedelta(minutes=config.SESSION_IDLE_MINUTES + 1)
            s.add(sess)
        s.commit()
    assert b2.get("/").status_code == 303


def test_disabled_user_loses_live_session(world):
    b = as_user("desk@a.test")
    owner = as_user("owner@a.test")
    assert owner.post(f"/settings/users/{world['desk']}/toggle").status_code == 303
    assert b.get("/").status_code == 303


def test_login_rotates_session_token(world):
    b = Browser()
    b.c.cookies.set(SESSION_COOKIE, "attacker-chosen-value")
    r = b.login("owner@a.test")
    issued = r.headers["set-cookie"].split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]
    assert issued and issued != "attacker-chosen-value"


def test_open_redirect_is_blocked(world):
    import re
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
        b = Browser()
        token = re.search(r'name="csrf_token" value="([^"]+)"', b.get("/login").text).group(1)
        r = b.c.post("/login", data={"email": "owner@a.test", "password": PASSWORD, "csrf_token": token,
                                     "next": evil})
        assert r.headers["location"] == "/", evil
    assert Browser().get("/lang/uk?next=//evil.example").headers["location"] == "/"


def test_signup_creates_isolated_trial_org(world):
    b = Browser()
    page = b.get("/signup")
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    data = {"org_name": "New Garage", "admin_name": "Nina", "email": "nina@new.test",
            "password": "short", "branch_name": "Main", "csrf_token": token}
    assert b.c.post("/signup", data=data).status_code == 400            # password policy
    data["password"] = "long-enough-password"
    r = b.c.post("/signup", data=data)
    assert r.status_code == 303
    home = b.get("/")
    assert home.status_code == 200 and "New Garage" in home.text
    assert "Client A" not in b.get("/clients").text                   # sees nothing of other tenants
    data["email"] = "owner@a.test"
    assert Browser().c.post("/signup", data=data).status_code == 403   # no CSRF from a new browser
