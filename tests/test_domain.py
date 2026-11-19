"""Money maths, AI runtime contract, migration wizard, i18n completeness, demo data, config."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import validate
from sqlmodel import select

from ai.runtime import load_schema, run_skill
from ai.runtime import runner as ai_runner
from ai.runtime.extractors import extract_nl_filter, extract_work_order, has_word
from app.i18n_strings import EN, UK
from app.models import Client, PartLine, WorkLine, order_totals
from tests.conftest import admin_session, as_user

ROOT = Path(__file__).resolve().parent.parent


# ── money ─────────────────────────────────────────────────────────────────────
def test_order_totals_vat_included_and_rounded():
    w = [WorkLine(order_id=1, norm_hours=1.5, rate=450)]
    p = [PartLine(order_id=1, qty=2, price=333.335)]
    t = order_totals(w, p, discount_pct=10, vat_pct=20)
    assert t["works"] == 675.0 and t["parts"] == 666.67 and t["subtotal"] == 1341.67
    assert t["discount"] == 134.17 and t["total"] == 1207.5
    assert t["vat"] == round(1207.5 - 1207.5 / 1.2, 2)


def test_order_totals_empty_and_zero_vat():
    assert order_totals([], [], 0, 0) == {"works": 0, "parts": 0, "subtotal": 0, "discount": 0,
                                          "vat": 0, "total": 0}
    t = order_totals([WorkLine(order_id=1, norm_hours=2, rate=100)], [], 0, 0)
    assert t["vat"] == 0 and t["total"] == 200


def test_order_page_total_matches_lines(world):
    b = as_user("owner@a.test")
    oid = world["orderA"]
    b.post(f"/orders/{oid}/part", {"name": "Filter", "qty": "2", "price": "125,50"})
    page = b.get(f"/orders/{oid}").text
    assert "1 751.00" in page                                     # 500 + 1000 + 2 × 125.50


# ── AI runtime ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,plate,make,service", [
    ("VW Passat ВО 7788 СК, стук спереду, заміна ГРМ і помпи", "ВО 7788 СК", "Volkswagen", "Timing belt kit replacement"),
    ("Toyota Camry AA 1234 BC needs brake pads and an oil change", "AA 1234 BC", "Toyota", "Brake pad replacement"),
])
def test_work_order_extractor_is_schema_valid(text, plate, make, service):
    d = extract_work_order(text)
    validate(d, load_schema("work-order-draft"))
    assert d["vehicle"] == {"plate": plate, "make_model": make}
    assert service in [w["service"] for w in d["works"]]


def test_extractor_matches_on_word_boundaries():
    assert not has_word("авто нове", "то")                        # "то" must not match inside "авто"
    assert has_word("планове то сьогодні", "то")
    d = extract_work_order("привезли авто")
    assert d["works"] == [] and "plate not recognised" in d["needs_clarification"]


def test_nl_filter():
    f = extract_nl_filter("готові дизелі Renault за тиждень")
    validate(f, load_schema("nl-filter"))
    assert {"field": "status", "op": "=", "value": "done"} in f["filters"]
    assert {"field": "engine", "op": "contains", "value": "diesel"} in f["filters"]
    assert {"field": "updated", "op": ">=", "value": "-7d"} in f["filters"]


def test_invalid_llm_output_falls_back_to_extractor(monkeypatch):
    calls = []

    def bad_llm(system, user):
        calls.append(user)
        return {"unexpected": True}
    monkeypatch.setattr(ai_runner.config, "LLM_PROVIDER", "selfhost")
    monkeypatch.setattr(ai_runner, "complete", bad_llm)
    res = run_skill("work-order-draft", "brake pads")
    assert len(calls) == 2 and "Previous answer was invalid" in calls[1]   # one retry with the error
    assert res["fallback"] is True
    validate(res["draft"], load_schema("work-order-draft"))


def test_valid_llm_output_is_used(monkeypatch):
    good = {"vehicle": {"plate": None, "make_model": "Kia Ceed"}, "complaint": "noise", "works": [],
            "parts": [], "confidence": 0.7, "needs_clarification": []}
    monkeypatch.setattr(ai_runner.config, "LLM_PROVIDER", "selfhost")
    monkeypatch.setattr(ai_runner, "complete", lambda s, u: good)
    res = run_skill("work-order-draft", "x")
    assert res["fallback"] is False and res["draft"] == good


def test_llm_client_stub_and_network_failure(monkeypatch):
    from ai.runtime import llm_client
    with pytest.raises(llm_client.LLMUnavailable):
        llm_client.complete("s", "u")
    monkeypatch.setattr(llm_client.config, "LLM_PROVIDER", "selfhost")
    monkeypatch.setattr(llm_client.config, "LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(llm_client.config, "LLM_TIMEOUT", 0.5)
    with pytest.raises(llm_client.LLMUnavailable):
        llm_client.complete("s", "u")


def test_ai_draft_endpoint_and_search(world):
    b = as_user("owner@a.test")
    r = b.c.post("/ai/work-order-draft", data={"text": "Skoda AA 1234 BC brakes"},
                 headers={"X-CSRF-Token": b.csrf, "Accept": "application/json"})
    assert r.status_code == 200 and r.json()["draft"]["vehicle"]["make_model"] == "Skoda"
    page = b.get("/orders/search?q=skoda+estimate").text
    assert world["numberA"] in page and world["numberB"] not in page


# ── migration ─────────────────────────────────────────────────────────────────
def test_migration_preview_confirm_and_reconcile(world):
    b = as_user("owner@a.test")
    b.post("/branch", {"branch_id": str(world["a1"])})
    b.refresh_csrf()
    csv = (ROOT / "migration" / "samples" / "clients_export_sample.csv").read_text(encoding="utf-8")
    page = b.post("/migration/preview", {"csv_text": csv}).text
    assert "tax_id" in page and "phone" in page
    assert b.post("/migration/apply", {"csv_text": csv}).status_code == 428      # Tier 3 without confirm
    page = b.post("/migration/apply", {"csv_text": csv, "confirm": "yes"}).text
    assert "Imported 4, skipped 1 of 5 rows." in page and "Reconciliation passed" in page
    with admin_session() as s:
        imported = s.exec(select(Client).where(Client.name == "ТОВ «Демо Логістика»")).one()
        assert imported.kind == "fleet" and imported.phone == "+380500000101" and imported.org_id == world["orgA"]
    assert b.post("/migration/preview", {"csv_text": "a;b\n" + "x" * 1_020_000}).status_code == 413
    assert b.post("/migration/preview", {"csv_text": "a;b\n" + "x;y\n" * 400_000}).status_code == 400


# ── i18n ──────────────────────────────────────────────────────────────────────
def test_locales_have_identical_keys():
    assert set(EN) == set(UK)
    assert all(v.strip() for v in UK.values())


def test_every_key_used_in_templates_and_code_exists():
    used = set()
    for f in (ROOT / "templates").glob("*.html"):
        used |= set(re.findall(r"\bt\(\s*'([^']+)'\s*[,)]", f.read_text(encoding="utf-8")))
    for f in (ROOT / "app").rglob("*.py"):
        used |= set(re.findall(r"""["']((?:err|msg|sms|bot)\.[a-z_]+)["']""", f.read_text(encoding="utf-8")))
    dynamic = {f"status.{s}" for s in ("new", "estimate", "in_progress", "waiting_parts", "done", "paid")}
    missing = sorted(k for k in used | dynamic if k not in EN)
    assert missing == []


def test_ukrainian_locale_switch(world):
    b = as_user("owner@a.test")
    b.get("/lang/uk")
    b.c.cookies.set("sto_lang", "uk")
    assert "Наряди" in b.get("/orders").text


# ── demo data and configuration ───────────────────────────────────────────────
def test_demo_seed_is_synthetic_idempotent_and_loginable():
    from sqlmodel import Session

    from app.db import admin_engine
    from app.demo import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo
    with Session(admin_engine) as s:
        assert seed_demo(s) is True
        assert seed_demo(s) is False
        phones = [c.phone for c in s.exec(select(Client)).all()]
        assert phones and all(p.startswith("+38000") for p in phones if p)
    from tests.conftest import Browser
    for email, *_ in DEMO_ACCOUNTS:
        Browser().login(email, DEMO_PASSWORD)


def _run(env: dict) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("STO_", "DEMO_", "DATABASE"))}
    base.update(env)
    return subprocess.run([sys.executable, "-c", "import app.config"], cwd=ROOT, env=base,
                          capture_output=True, text=True, timeout=60)


def test_production_refuses_unsafe_config():
    r = _run({"STO_ENV": "production"})
    assert r.returncode != 0 and "STO_SECRET is required" in r.stderr
    r = _run({"STO_ENV": "production", "STO_SECRET": "short"})
    assert r.returncode != 0 and "at least 32" in r.stderr
    r = _run({"STO_ENV": "production", "STO_SECRET": "x" * 40, "DEMO_MODE": "1"})
    assert r.returncode != 0 and "DEMO_MODE=1 is not allowed" in r.stderr
    assert _run({"STO_ENV": "production", "STO_SECRET": "x" * 40}).returncode == 0
