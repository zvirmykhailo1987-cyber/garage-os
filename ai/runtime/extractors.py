# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Deterministic offline extractors — the free fallback when no LLM is configured.

They emit output valid against the same strict JSON schemas as the LLM path. Grounding:
missing data → ``null`` plus a "needs clarification" note; nothing is invented.
This is a keyword parser, not a language model; matching is on word boundaries so
"to" never matches inside "auto" and "ВАЗ" never matches inside "вазон".
Understands Ukrainian and English input.
"""
from __future__ import annotations

import re

# Ukrainian plate: 2 letters + 4 digits + 2 letters (Cyrillic or look-alike Latin)
_PLATE = re.compile(r"\b[А-ЯІЇЄABEKMHOPCTXY]{2}\s?\d{4}\s?[А-ЯІЇЄABEKMHOPCTXY]{2}\b", re.IGNORECASE)

_MAKES: list[tuple[str, list[str]]] = [
    ("Volkswagen", ["vw", "volkswagen", "фольксваген", "passat", "пасат", "golf", "гольф"]),
    ("Skoda", ["skoda", "škoda", "шкода", "octavia", "октавія"]),
    ("Renault", ["renault", "рено", "megane", "меган"]),
    ("Toyota", ["toyota", "тойота", "camry", "камрі", "corolla", "королла"]),
    ("BMW", ["bmw", "бмв"]),
    ("Audi", ["audi", "ауді"]),
    ("Mercedes-Benz", ["mercedes", "мерседес", "sprinter", "спрінтер"]),
    ("Ford", ["ford", "форд", "transit", "транзит"]),
    ("Opel", ["opel", "опель"]),
    ("Peugeot", ["peugeot", "пежо"]),
    ("Nissan", ["nissan", "нісан"]),
    ("Hyundai", ["hyundai", "хюндай", "хендай"]),
    ("Kia", ["kia", "кіа"]),
    ("Mazda", ["mazda", "мазда"]),
    ("Honda", ["honda", "хонда"]),
    ("Volvo", ["volvo", "вольво"]),
    ("Mitsubishi", ["mitsubishi", "міцубісі"]),
    ("Chevrolet", ["chevrolet", "шевроле"]),
    ("Daewoo", ["daewoo", "деу", "lanos", "ланос"]),
    ("Lada", ["lada", "лада", "ваз"]),
    ("Iveco", ["iveco", "івеко"]),
]

# (stems, canonical service, typical norm hours, part or None)
_SERVICES: list[tuple[list[str], str, float, str | None]] = [
    (["грм", "timing belt", "timing"], "Timing belt kit replacement", 3.5, "Timing belt kit"),
    (["помп", "water pump"], "Water pump replacement", 1.0, "Water pump"),
    (["колодк", "гальм", "brake pad", "brakes", "brake"], "Brake pad replacement", 0.8, "Brake pads"),
    (["диск", "brake disc", "rotor"], "Brake disc replacement", 1.0, "Brake disc"),
    (["зчепленн", "clutch"], "Clutch replacement", 4.0, "Clutch kit"),
    (["масл", "олив", "oil change", "oil"], "Engine oil change", 0.4, "Oil filter"),
    (["антифриз", "охолодж", "coolant"], "Coolant replacement", 0.6, "Coolant"),
    (["свічк", "spark plug"], "Spark plug replacement", 0.5, "Spark plugs"),
    (["стартер", "starter"], "Starter repair", 1.2, "Starter"),
    (["генератор", "alternator"], "Alternator repair", 1.2, "Alternator"),
    (["акумулятор", "акб", "battery"], "Battery replacement", 0.3, "Battery"),
    (["амортизатор", "стійк", "shock", "strut"], "Shock absorber replacement", 1.5, "Shock absorber"),
    (["підшипник", "ступиц", "wheel bearing", "bearing"], "Wheel bearing replacement", 1.5, "Wheel bearing"),
    (["розвал", "сходженн", "alignment"], "Wheel alignment", 1.0, None),
    (["діагност", "ходов", "підвіск", "diagnos", "suspension", "стук", "knock"],
     "Suspension diagnostics", 0.7, None),
    (["шиномонтаж", "шини", "tyre", "tire"], "Tyre fitting", 0.6, None),
    (["кондиціонер", "кондиц", "air con", "a/c"], "Air-conditioning service", 1.0, None),
    (["турбін", "turbo"], "Turbocharger repair", 3.0, "Turbocharger"),
    (["то", "обслуговуванн", "service", "inspection"], "Scheduled maintenance", 1.5, None),
]


def has_word(low: str, stem: str) -> bool:
    """Short stems (≤3 chars) match whole words; longer ones match as word prefixes;
    stems with a space or slash match as plain substrings."""
    stem = stem.strip().lower()
    if not stem:
        return False
    if " " in stem or "/" in stem:
        return stem in low
    if len(stem) <= 3:
        return re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", low) is not None
    return re.search(r"(?<!\w)" + re.escape(stem), low) is not None


def find_make(low: str) -> str | None:
    for canon, syns in _MAKES:
        if any(has_word(low, s) for s in syns):
            return canon
    return None


def extract_work_order(text: str) -> dict:
    t = (text or "").strip()[:2000]
    low = t.lower()
    m = _PLATE.search(t)
    plate = re.sub(r"\s+", " ", m.group(0).upper()) if m else None
    make = find_make(low)
    works, parts, seen = [], [], set()
    for stems, svc, nh, part in _SERVICES:
        if svc not in seen and any(has_word(low, st) for st in stems):
            seen.add(svc)
            works.append({"service": svc, "norm_hours": nh})
            if part:
                parts.append({"name": part, "qty": 1})
    need = []
    if not plate:
        need.append("plate not recognised")
    if not make:
        need.append("confirm make / model")
    need.append("confirm jobs and norm hours" if works else "list the jobs")
    conf = round(min(0.4 + 0.2 * bool(plate) + 0.15 * bool(make) + 0.15 * bool(works), 0.9), 2)
    return {"vehicle": {"plate": plate, "make_model": make}, "customer": None, "complaint": t,
            "works": works[:20], "parts": parts[:40], "confidence": conf,
            "needs_clarification": need}


_STATUS_WORDS = [
    (["оплачен", "paid"], "paid"), (["готов", "ready", "done"], "done"),
    (["в роботі", "in progress", "in work"], "in_progress"),
    (["чекає", "очікує", "waiting"], "waiting_parts"),
    (["естімейт", "кошторис", "estimate"], "estimate"), (["нов", "new"], "new"),
]


def extract_nl_filter(query: str) -> dict:
    q = (query or "").strip()[:300]
    low = q.lower()
    filters: list[dict] = []
    for words, st in _STATUS_WORDS:
        if any(has_word(low, w) for w in words):
            filters.append({"field": "status", "op": "=", "value": st})
            break
    make = find_make(low)
    if make:
        filters.append({"field": "make", "op": "contains", "value": make})
    if any(has_word(low, w) for w in ("дизел", "diesel")):
        filters.append({"field": "engine", "op": "contains", "value": "diesel"})
    if any(has_word(low, w) for w in ("тижд", "week")):
        filters.append({"field": "updated", "op": ">=", "value": "-7d"})
    elif any(has_word(low, w) for w in ("сьогодн", "today")):
        filters.append({"field": "updated", "op": ">=", "value": "-1d"})
    elif any(has_word(low, w) for w in ("місяц", "month")):
        filters.append({"field": "updated", "op": ">=", "value": "-30d"})
    summary = "; ".join(f"{f['field']} {f['op']} {f['value']}" for f in filters) or "all orders"
    return {"filters": filters, "summary": summary}
