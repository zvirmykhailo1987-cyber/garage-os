# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Telegram bot (aiogram 3). It never touches the database — only the REST API, with a
"bot" token whose scope is one organization.

Customers:  /start CODE  — link this chat to the client record (code from the CRM)
            /status NUMBER · /approve NUMBER — only orders of the linked client
Mechanics:  any text or voice note → a draft work order (confirmed in the CRM)

    TG_BOT_TOKEN=... STO_API_TOKEN=sto_... python -m tg_bot.bot
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.i18n import translate

from . import config

log = logging.getLogger("sto.bot")


def t(key: str, **kw: Any) -> str:
    return translate(config.LANG, key, **kw)


class Api:
    """Thin async client for /api/v1."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def call(self, method: str, path: str, **kw: Any) -> tuple[int, dict]:
        r = await self.http.request(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    async def link(self, code: str, chat_id: int) -> tuple[int, dict]:
        return await self.call("POST", "/api/v1/telegram/link", json={"code": code.upper(), "chat_id": str(chat_id)})

    async def status(self, number: str, chat_id: int) -> tuple[int, dict]:
        return await self.call("GET", f"/api/v1/orders/{quote(number[:20], safe='')}/status",
                               params={"chat_id": str(chat_id)})

    async def approve(self, number: str, chat_id: int) -> tuple[int, dict]:
        return await self.call("POST", f"/api/v1/estimates/{quote(number[:20], safe='')}/approve",
                               params={"chat_id": str(chat_id)})

    async def draft(self, text: str, chat_id: int) -> tuple[int, dict]:
        return await self.call("POST", "/api/v1/ai/work-order-draft",
                               json={"text": text[:2000], "chat_id": str(chat_id)})


def format_draft(res: dict) -> str:
    d = res.get("draft", {})
    works = ", ".join(w["service"] for w in d.get("works", [])) or "—"
    v = d.get("vehicle", {})
    return (f"{t('ai.confidence', pct=round(res.get('confidence', 0) * 100))}\n"
            f"🚗 {v.get('make_model') or '—'} {v.get('plate') or ''}\n🔧 {works}\n"
            f"❓ {'; '.join(d.get('needs_clarification', []))}\n\n{t('ai.hitl')}")


async def transcribe(http: httpx.AsyncClient, audio: bytes) -> str:
    if not (config.STT_BASE_URL and config.STT_API_KEY):
        return ""
    r = await http.post(f"{config.STT_BASE_URL}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {config.STT_API_KEY}"},
                        data={"model": config.STT_MODEL, "language": config.STT_LANGUAGE},
                        files={"file": ("voice.ogg", audio, "audio/ogg")}, timeout=60)
    r.raise_for_status()
    return str(r.json().get("text", "")).strip()


async def main() -> None:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import Message

    if config.missing():
        raise SystemExit("missing environment: " + ", ".join(config.missing()))
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    http = httpx.AsyncClient(base_url=config.API_BASE, timeout=config.TIMEOUT,
                             headers={"Authorization": f"Bearer {config.API_TOKEN}"})
    api = Api(http)

    @dp.message(CommandStart())
    async def start(m: Message, command: CommandObject) -> None:
        code = (command.args or "").strip()
        if code:
            code_status, data = await api.link(code, m.chat.id)
            await m.answer("✅ " + data.get("client", "") if code_status == 200 else t("bot.not_found", number=code))
            return
        await m.answer(t("bot.hello"))

    @dp.message(Command("status"))
    async def status(m: Message, command: CommandObject) -> None:
        num = (command.args or "").strip()
        code, d = await api.status(num, m.chat.id)
        await m.answer(t("bot.status", number=d["number"], car=d["vehicle"], status=t(f"status.{d['status']}"),
                         total=f"{d['total']:.2f}") if code == 200 else t("bot.not_found", number=num))

    @dp.message(Command("approve"))
    async def approve(m: Message, command: CommandObject) -> None:
        num = (command.args or "").strip()
        code, _ = await api.approve(num, m.chat.id)
        await m.answer(t("bot.approved", number=num) if code == 200 else t("bot.not_found", number=num))

    @dp.message(F.voice)
    async def voice(m: Message) -> None:
        if m.voice is None:
            return
        f = await bot.download(m.voice.file_id)
        text = await transcribe(http, f.read() if f else b"")
        if not text:
            await m.answer("—")
            return
        code, res = await api.draft(text, m.chat.id)
        await m.answer(format_draft(res) if code == 200 else "—")

    @dp.message(F.text)
    async def text_note(m: Message) -> None:
        code, res = await api.draft(m.text or "", m.chat.id)
        await m.answer(format_draft(res) if code == 200 else "—")

    try:
        await dp.start_polling(bot)
    finally:
        await http.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
