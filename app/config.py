"""Runtime configuration — everything comes from environment variables.

Nothing secret lives in the code. ``STO_ENV=production`` turns every unsafe default
into a startup error (fail fast instead of running with a guessable secret).
"""
from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


ENV = os.environ.get("STO_ENV", "dev").strip().lower()
IS_PROD = ENV in ("prod", "production")
IS_TEST = ENV == "test"

DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE / "data")))


class ConfigError(RuntimeError):
    pass


# ── secrets ───────────────────────────────────────────────────────────────────
SECRET = os.environ.get("STO_SECRET", "")
if not SECRET:
    if IS_PROD:
        raise ConfigError("STO_SECRET is required when STO_ENV=production "
                          "(python -c \"import secrets;print(secrets.token_urlsafe(32))\")")
    SECRET = _secrets.token_urlsafe(32)        # dev/test: random per process, never static
elif IS_PROD and len(SECRET) < 32:
    raise ConfigError("STO_SECRET must be at least 32 characters in production")

# ── database ──────────────────────────────────────────────────────────────────
# DATABASE_URL is what the web process uses. On Postgres it must be a NON-owner,
# NON-superuser role so that row-level security applies to it.
# DATABASE_ADMIN_URL (owner) is used only by `python -m app.manage` (schema + RLS + seed).
DATABASE_URL = os.environ.get("DATABASE_URL", "") or f"sqlite:///{DATA_DIR / 'sto.sqlite'}"
DATABASE_ADMIN_URL = os.environ.get("DATABASE_ADMIN_URL", "") or DATABASE_URL
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

# ── demo mode ─────────────────────────────────────────────────────────────────
# Demo accounts and synthetic data exist ONLY when DEMO_MODE=1. Production refuses it.
DEMO_MODE = _flag("DEMO_MODE")
if IS_PROD and DEMO_MODE:
    raise ConfigError("DEMO_MODE=1 is not allowed with STO_ENV=production")

# ── HTTP / cookies ────────────────────────────────────────────────────────────
COOKIE_SECURE = _flag("COOKIE_SECURE", "1" if IS_PROD else "0")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "12"))     # absolute lifetime
SESSION_IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "120"))
ENABLE_API_DOCS = _flag("ENABLE_API_DOCS", "0") and not IS_PROD          # /docs closed by default
LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", "15"))
DEFAULT_LOCALE = os.environ.get("STO_LOCALE", "en")

# ── integrations (free offline stubs by default; paid providers are seams) ────
PLATE_PROVIDER = os.environ.get("PLATE_PROVIDER", "stub")
TELEPHONY_PROVIDER = os.environ.get("TELEPHONY_PROVIDER", "stub")
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "stub")
FISCAL_PROVIDER = os.environ.get("FISCAL_PROVIDER", "stub")

# ── LLM (vendor-agnostic, OpenAI-compatible). "stub" = deterministic extractor ─
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "stub")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30"))

# ── offline-first sync (only on edge copies; the hub leaves HUB_URL empty) ────
HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
SYNC_DEVICE_TOKEN = os.environ.get("SYNC_DEVICE_TOKEN", "")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "45"))
# Offline copies number orders in their own space (PREFIX-TAG00001) so an order created
# offline can never collide with one created on the hub (unique branch + number).
EDGE_TAG = (__import__("hashlib").sha256(SYNC_DEVICE_TOKEN.encode()).hexdigest()[:4].upper()
            if HUB_URL and SYNC_DEVICE_TOKEN else "")
if IS_PROD and HUB_URL and not SYNC_DEVICE_TOKEN:
    raise ConfigError("SYNC_DEVICE_TOKEN is required when HUB_URL is set in production")

APP_TITLE = "STO Network"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
