"""Legacy data migration (1C/BAF client export → clients).

1. parse the CSV into a staging preview (nothing is written);
2. propose a column mapping — schema-validated, each item flagged for review if unsure;
3. a human reviews and confirms (Tier 3);
4. the import itself is deterministic code, never the model;
5. reconciliation: imported + skipped must equal the source row count.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from jsonschema import validate
from sqlmodel import Session

from ai.runtime import load_schema

from ..models import Client

MAX_ROWS = 5000
MAX_BYTES = 1_000_000
TARGETS = ("name", "phone", "tax_id", "kind")

# ORDER MATTERS: specific rules before the generic "name" rule
_RULES: list[tuple[tuple[str, ...], str, str, float]] = [
    (("телефон", "тел", "phone", "моб"), "phone", "normalise to +380", 0.9),
    (("едрпоу", "єдрпоу", "edrpou", "tax", "код"), "tax_id", "digits only", 0.88),
    (("вид", "тип", "kind", "type"), "kind", "map to private/company/fleet", 0.86),
    (("наимен", "назв", "клиент", "клієнт", "контрагент", "имя", "ім'я", "name"), "name", "trim", 0.92),
]


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    text = (text or "").strip()
    if not text:
        return [], []
    first = text.splitlines()[0]
    delim = ";" if first.count(";") >= first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    headers = [h.strip()[:120] for h in rows[0]] if rows else []
    data = [r for r in rows[1:] if any(c.strip() for c in r)][:MAX_ROWS]
    return headers, data


def propose_mapping(headers: list[str]) -> list[dict[str, Any]]:
    schema = load_schema("migration-mapping")
    out, used = [], set()
    for h in headers:
        low = h.lower()
        hit = next((r for r in _RULES if r[1] not in used and any(x in low for x in r[0])), None)
        if hit:
            used.add(hit[1])
            m = {"source": h, "target": hit[1], "transform": hit[2], "confidence": hit[3],
                 "rationale": f"header matches {hit[1]}", "needs_review": hit[3] < 0.87}
        else:
            m = {"source": h, "target": "skip", "transform": "-", "confidence": 0.3,
                 "rationale": "not recognised", "needs_review": True}
        validate(m, schema)
        out.append(m)
    return out


def norm_phone(v: str) -> str:
    d = re.sub(r"\D", "", v or "")
    if d.startswith("380") and len(d) == 12:
        return "+" + d
    if len(d) == 10 and d.startswith("0"):
        return "+38" + d
    return (v or "").strip()[:40]


def norm_kind(v: str) -> str:
    low = (v or "").lower()
    if any(k in low for k in ("перев", "автопарк", "fleet", "carrier")):
        return "fleet"
    if any(k in low for k in ("юр", "комп", "тов", "ооо", "фоп", "company", "llc")):
        return "company"
    return "private"


def apply_mapping(s: Session, headers: list[str], rows: list[list[str]], mapping: list[dict],
                  org_id: int, branch_id: int) -> dict[str, Any]:
    col2field: dict[int, str] = {}
    for i, h in enumerate(headers):
        m = next((x for x in mapping if x["source"] == h), None)
        if m and m["target"] in TARGETS:
            col2field[i] = m["target"]
    imported = skipped = 0
    for r in rows:
        vals = {f: (r[i].strip() if i < len(r) else "") for i, f in col2field.items()}
        name = vals.get("name", "")[:160]
        if not name:
            skipped += 1
            continue
        s.add(Client(name=name, phone=norm_phone(vals.get("phone", "")),
                     tax_id=re.sub(r"\D", "", vals.get("tax_id", ""))[:20],
                     kind=norm_kind(vals.get("kind", "")), org_id=org_id, branch_id=branch_id))
        imported += 1
    return {"source_rows": len(rows), "imported": imported, "skipped": skipped,
            "reconciled": imported + skipped == len(rows),
            "mapped_fields": sorted(set(col2field.values()))}


def load_sample() -> str:
    p = Path(__file__).resolve().parent.parent.parent / "migration" / "samples" / "clients_export_sample.csv"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def check_size(text: str) -> str | None:
    return "err.csv_too_large" if len(text.encode("utf-8")) > MAX_BYTES else None
