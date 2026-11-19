"""Authentication primitives: passwords, sessions, CSRF, tokens, rate limiting.

* Passwords — argon2id (argon2-cffi). Legacy ``sha256(SECRET+pw)`` hashes from v1 are
  still accepted once and transparently re-hashed to argon2id on successful login.
* Sessions — random 256-bit token in an HttpOnly cookie; the DB stores only
  ``sha256(token)`` with absolute + idle expiry and a ``revoked_at`` for logout.
* CSRF — synchronizer token bound to the session (hidden form field or
  ``X-CSRF-Token`` header). Anonymous forms (login/signup) use a signed
  double-submit token tied to a pre-session cookie.
* API / sync tokens — random, shown once, stored as sha256.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlmodel import Session

from . import config
from .models import User, UserSession, utcnow

_ph = PasswordHasher()           # argon2id, library defaults (RFC 9106 low-memory profile)
_LEGACY_PREFIX = "sha256$"

# A real hash of a random string: verifying against it keeps the timing of
# "unknown e-mail" equal to "wrong password" (no user enumeration by timing).
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


# ── passwords ─────────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return _ph.hash(password)


def legacy_hash(password: str, secret: str) -> str:
    """The v1 scheme, kept only to verify and migrate old rows."""
    return _LEGACY_PREFIX + hashlib.sha256((secret + password).encode()).hexdigest()


def verify_password(stored: str, password: str) -> tuple[bool, bool]:
    """Return ``(ok, needs_rehash)``."""
    if not stored:
        burn_password_check(password)
        return False, False
    if stored.startswith(_LEGACY_PREFIX):
        ok = hmac.compare_digest(stored, legacy_hash(password, config.SECRET))
        return ok, ok
    try:
        _ph.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, _ph.check_needs_rehash(stored)


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification (used when the account does not exist)."""
    with contextlib.suppress(VerificationError):
        _ph.verify(_DUMMY_HASH, password)


def check_password_policy(password: str) -> str | None:
    if len(password) < 10:
        return "err.password_short"
    if len(password) > 256:
        return "err.password_long"
    return None


# ── tokens ────────────────────────────────────────────────────────────────────
def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_secret_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


# ── sessions ──────────────────────────────────────────────────────────────────
SESSION_COOKIE = "sto_session"
PRE_COOKIE = "sto_pre"


def create_session(s: Session, user: User, ip: str = "",
                   active_branch_id: int | None = None) -> str:
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    if user.id is None:
        raise RuntimeError("invariant: user.id is set")
    s.add(UserSession(id=sha256_hex(raw), user_id=user.id, csrf_token=secrets.token_urlsafe(32),
                      active_branch_id=active_branch_id, created_at=now, last_seen_at=now,
                      expires_at=now + timedelta(hours=config.SESSION_TTL_HOURS), ip=ip[:64]))
    return raw


def load_session(s: Session, raw: str) -> UserSession | None:
    """Valid, unexpired, unrevoked session — or None. Slides the idle window."""
    if not raw or len(raw) > 128:
        return None
    sess = s.get(UserSession, sha256_hex(raw))
    if sess is None or sess.revoked_at is not None:
        return None
    now = utcnow()
    if now >= sess.expires_at or now - sess.last_seen_at > timedelta(minutes=config.SESSION_IDLE_MINUTES):
        return None
    if now - sess.last_seen_at > timedelta(minutes=1):   # throttle writes
        sess.last_seen_at = now
        s.add(sess)
        s.commit()
    return sess


def revoke_session(s: Session, sess: UserSession) -> None:
    sess.revoked_at = utcnow()
    s.add(sess)
    s.commit()


def set_session_cookie(resp, raw: str) -> None:  # type: ignore[no-untyped-def]
    resp.set_cookie(SESSION_COOKIE, raw, max_age=config.SESSION_TTL_HOURS * 3600,
                    httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/")


def clear_session_cookie(resp) -> None:  # type: ignore[no-untyped-def]
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=config.COOKIE_SECURE,
                       httponly=True, samesite="lax")


# ── anonymous CSRF (login / signup) ───────────────────────────────────────────
def pre_csrf_token(pre_cookie: str) -> str:
    return hmac.new(config.SECRET.encode(), b"pre-csrf:" + pre_cookie.encode(),
                    hashlib.sha256).hexdigest()


def new_pre_cookie() -> str:
    return secrets.token_urlsafe(24)


def tokens_match(expected: str, given: str) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(expected, given)


# ── rate limiting (sliding window, in-process) ────────────────────────────────
class RateLimiter:
    """Small sliding-window limiter. In-process on purpose: one container, no Redis.
    Account lockout (persistent, per user) is separate — see ``User.locked_until``."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = {}
        self._evicted = float("-inf")
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Register one attempt; False if the key is over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000 and now - self._evicted > self.window:
                self._evicted = now              # bounded memory: evict idle keys, at most once a window
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = RateLimiter(limit=10, window_s=60)       # per client IP
signup_limiter = RateLimiter(limit=5, window_s=3600)     # per client IP
api_limiter = RateLimiter(limit=600, window_s=60)        # per token
chat_limiter = RateLimiter(limit=20, window_s=60)        # per (bot token, Telegram chat)


def register_failed_login(user: User) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= config.LOGIN_MAX_FAILURES:
        user.locked_until = utcnow() + timedelta(minutes=config.LOGIN_LOCKOUT_MINUTES)
        user.failed_logins = 0


def is_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > utcnow())
