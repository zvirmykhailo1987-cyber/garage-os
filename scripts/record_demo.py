"""Record the demo walkthrough (docs/demo.webm) and the README screenshots.

Dev-only helper, not part of the app or CI. Needs Playwright with Chromium:
    pip install playwright && python -m playwright install chromium
Run from the repository root (any Python with Playwright; the app runs in .venv):
    python scripts/record_demo.py

It starts the app on a throw-away SQLite database with the synthetic demo data,
drives the browser through the DEMO.md story with a caption bar, and writes:
    docs/demo.webm, docs/screenshot-*.png
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "demo-password-2026"          # the public demo password (see DEMO.md)
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
W, H = 1440, 900
PACE = 1.6                               # caption hold multiplier (readable at 1x playback)


def start_app(tmp: Path) -> subprocess.Popen:
    py = str(VENV_PY if VENV_PY.exists() else sys.executable)
    env = {**os.environ, "STO_ENV": "dev", "DEMO_MODE": "1", "STO_SECRET": secrets.token_hex(24),
           "DATABASE_URL": f"sqlite:///{(tmp / 'demo.sqlite').as_posix()}", "PYTHONUTF8": "1"}
    env.pop("DATABASE_ADMIN_URL", None)
    subprocess.run([py, "-m", "app.manage", "init"], cwd=ROOT, env=env, check=True)
    proc = subprocess.Popen([py, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
                            cwd=ROOT, env=env)
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/login", timeout=1)  # noqa: S310 — fixed local URL
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("app did not start")


def caption(page: Page, text: str, hold: float = 2.2) -> None:
    """A caption bar drawn over the page (CSSOM from devtools, so the app's CSP is untouched)."""
    page.wait_for_load_state("networkidle")
    page.evaluate("""t => {
        let el = document.getElementById('__cap');
        if (!el) {
            el = document.createElement('div'); el.id = '__cap';
            Object.assign(el.style, {position: 'fixed', left: '50%', bottom: '28px', transform: 'translateX(-50%)',
                zIndex: 99999, background: '#3730a3', color: '#fff', border: '1px solid rgba(255,255,255,.35)',
                padding: '12px 22px', borderRadius: '10px', font: '600 18px/1.35 Inter, system-ui, sans-serif',
                maxWidth: '1100px', textAlign: 'center', boxShadow: '0 8px 30px rgba(0,0,0,.25)', pointerEvents: 'none'});
            document.body.appendChild(el);
        }
        el.textContent = t;
    }""", text)
    page.wait_for_timeout(int(hold * PACE * 1000))


def login(page: Page, email: str) -> None:
    page.context.clear_cookies()                 # a fresh session; logout itself is a CSRF-protected POST
    page.goto(BASE + "/login")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button.btn.pri")
    page.wait_for_load_state("networkidle")


def slow_type(page: Page, sel: str, text: str) -> None:
    page.click(sel)
    page.keyboard.type(text, delay=28)


def story(page: Page) -> None:
    page.goto(BASE + "/login")
    caption(page, "STO Network — multi-tenant workshop management. All data here is synthetic.", 2.8)
    page.fill("#email", "owner@northwind.test")
    page.fill("#password", PASSWORD)
    caption(page, "Sign in as the owner of a 3-branch network", 1.4)
    page.click("button.btn.pri")
    page.wait_for_load_state("networkidle")
    caption(page, "Dashboard: live KPIs and the pipeline for the whole network", 2.8)

    with page.expect_navigation():
        page.select_option("#branch-select", index=1)
    page.wait_for_load_state("networkidle")
    caption(page, "Branch selector re-scopes every list, count and KPI", 2.4)

    page.goto(BASE + "/orders/new")
    caption(page, "New intake: draft a work order from a free-text note", 1.8)
    slow_type(page, "#ai-text", "VW Passat AA 1234 BC, knocking at the front, change brake pads and oil")
    page.click("#ai-draft-btn")
    page.wait_for_selector("#ai-works:not([hidden])", timeout=10000)
    caption(page, "Plate, make and complaint are extracted; jobs are suggested for review", 3.0)
    caption(page, "Nothing is saved until a human presses Create — money is never computed by the model", 2.6)
    page.click("button.btn.pri.lg")
    page.wait_for_load_state("networkidle")

    caption(page, "The new work order", 1.6)
    page.fill("#w-svc", "Front brake pads replacement")
    page.fill("#w-h", "1.5")
    page.locator("#w-h ~ button").click()
    page.wait_for_load_state("networkidle")
    page.fill("#p-sku", "BP-2040")
    page.locator("#p-price ~ button").click()
    page.wait_for_load_state("networkidle")
    page.locator("text=Brake pads (front)").first.scroll_into_view_if_needed()
    caption(page, "Labour and a part from stock — the price comes from the catalogue, totals are recomputed", 3.2)

    page.goto(BASE + "/kanban")
    caption(page, "Board: drag a card to change its status", 1.8)
    src = page.locator(".kb-body[data-status='new'] .kb-card").first
    dst = page.locator(".kb-body[data-status='in_progress']")
    src.drag_to(dst)
    page.wait_for_timeout(900)
    page.evaluate("window.scrollTo(0, 0)")
    caption(page, "Saved with optimistic locking — a stale card snaps back instead of overwriting", 2.8)

    page.goto(BASE + "/orders")
    caption(page, "Work orders: keyset pagination, filters — p50 under 80 ms on 200 000 orders", 2.6)
    slow_type(page, "#q", "KYI-0010")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    caption(page, "Search by number, plate or customer (indexed prefix search)", 2.4)
    page.goto(BASE + "/orders?status=paid")
    with page.expect_navigation():
        page.locator("table a[href^='/orders/']").first.click()
    caption(page, "A paid order: money fields are locked; fiscal receipt and invoice", 2.6)
    href = page.locator("a[href$='/invoice']").first.get_attribute("href")
    if href:
        page.goto(BASE + href)
        caption(page, "Printable invoice", 2.0)

    page.goto(BASE + "/clients")
    caption(page, "Customers and their cars, searchable", 2.0)
    page.goto(BASE + "/fleet")
    caption(page, "Fleet contracts for corporate customers", 2.0)

    page.goto(BASE + "/settings")
    if page.locator("text=API tokens").count():
        page.locator("text=API tokens").first.scroll_into_view_if_needed()
    caption(page, "Settings: team, branches, scoped API tokens and per-device sync tokens (shown once, stored hashed)", 3.2)

    login(page, "mechanic.kyiv@northwind.test")
    page.goto(BASE + "/orders")
    with page.expect_navigation():
        page.locator("table a[href^='/orders/']").first.click()
    caption(page, "A mechanic sees the job but no discount, payment or fiscal controls — the server returns 403 too", 3.2)

    login(page, "owner@halted.test")
    caption(page, "A suspended plan: read-only banner, every write is refused", 2.6)

    login(page, "operator@platform.test")
    caption(page, "The platform operator manages tenants and plans — counts only, never tenant data", 3.0)

    login(page, "owner@northwind.test")
    page.click("#theme-toggle")
    page.wait_for_timeout(500)
    caption(page, "Dark theme", 1.6)
    page.goto(BASE + "/lang/uk?next=/")
    page.wait_for_load_state("networkidle")
    caption(page, "Ukrainian locale (English is the default)", 2.2)
    page.goto(BASE + "/lang/en?next=/")
    page.click("#theme-toggle")
    caption(page, "Postgres row-level security, argon2id, CSRF, strict CSP · GitHub Actions: ruff, mypy, bandit, pytest", 3.4)


def screenshots(browser) -> None:
    def shot(email: str, path: str, file: str, dark: bool = False, width: int = W, height: int = H,
             full: bool = False) -> None:
        ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                                  color_scheme="dark" if dark else "light")
        p = ctx.new_page()
        login(p, email)
        p.goto(BASE + path)
        p.wait_for_load_state("networkidle")
        p.wait_for_timeout(300)
        p.screenshot(path=str(DOCS / file), full_page=full)
        ctx.close()

    shot("owner@northwind.test", "/", "screenshot-dashboard-light.png")
    shot("owner@northwind.test", "/kanban", "screenshot-board-dark.png", dark=True)
    shot("manager.kyiv@northwind.test", "/orders", "screenshot-orders-light.png")
    shot("owner@northwind.test", "/", "screenshot-mobile-dark.png", dark=True, width=390, height=844)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sto-demo-"))
    proc = start_app(tmp)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp / "video"),
                                      record_video_size={"width": W, "height": H}, color_scheme="light")
            page = ctx.new_page()
            story(page)
            video = page.video.path() if page.video else None
            ctx.close()
            if video:
                shutil.copy(video, DOCS / "demo.webm")
            screenshots(browser)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)
    print("written:", DOCS / "demo.webm")


if __name__ == "__main__":
    main()
