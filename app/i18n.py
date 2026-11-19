# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""UI strings in English (default) and Ukrainian.

Keys are namespaced (``nav.orders``, ``status.done``). A test asserts that every key
used in templates and code exists in both locales, and that both locales have the same
key set. Unknown keys render as the key itself (visible in review, never a crash).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.requests import Request

from . import config
from .i18n_strings import EN, UK

LOCALES = {"en": "English", "uk": "Українська"}
LOCALE_COOKIE = "sto_lang"
MESSAGES: dict[str, dict[str, str]] = {"en": EN, "uk": UK}


def get_locale(request: Request) -> str:
    lang = request.cookies.get(LOCALE_COOKIE, "")
    return lang if lang in LOCALES else (config.DEFAULT_LOCALE if config.DEFAULT_LOCALE in LOCALES else "en")


def translate(lang: str, key: str, **kw: Any) -> str:
    table = MESSAGES.get(lang, EN)
    s = table.get(key) or EN.get(key) or key
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return s
    return s


def has_key(key: str) -> bool:
    return key in EN


def translator(lang: str) -> Callable[..., str]:
    def t(key: str, **kw: Any) -> str:
        return translate(lang, key, **kw)
    return t
