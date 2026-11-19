# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Bot configuration from the environment. There are no defaults for secrets."""
from __future__ import annotations

import os

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")            # from @BotFather
API_BASE = os.environ.get("STO_API_BASE", "http://web:8000").rstrip("/")
API_TOKEN = os.environ.get("STO_API_TOKEN", "")           # a "bot" token created in Settings → API tokens
LANG = os.environ.get("BOT_LANG", "uk")
TIMEOUT = float(os.environ.get("BOT_HTTP_TIMEOUT", "20"))

# optional speech-to-text (any OpenAI-compatible /audio/transcriptions endpoint)
STT_BASE_URL = os.environ.get("STT_BASE_URL", "").rstrip("/")
STT_API_KEY = os.environ.get("STT_API_KEY", "")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "uk")


def missing() -> list[str]:
    return [name for name, value in (("TG_BOT_TOKEN", BOT_TOKEN), ("STO_API_TOKEN", API_TOKEN)) if not value]
