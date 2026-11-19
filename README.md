# GarageOS

Multi-tenant workshop management for networks of auto service stations — work orders,
a kanban board, customers, stock, fleet contracts, a Telegram channel and offline-first
sync — built as a deliberately small but complete SaaS: tenant isolation enforced twice
(application layer **and** Postgres row-level security), server-side sessions, CSRF on every
form, a strict CSP, role and plan checks on the route, and a test for every boundary.

![Dashboard, light theme](docs/screenshot-dashboard-light.png)

| Board (dark) | Work orders | Phone (dark) |
|---|---|---|
| ![Board, dark theme](docs/screenshot-board-dark.png) | ![Work orders list](docs/screenshot-orders-light.png) | ![Dashboard on a phone, dark theme](docs/screenshot-mobile-dark.png) |

**Demo video (2 min, captioned):** [`docs/demo.webm`](docs/demo.webm) — recorded by
[`scripts/record_demo.py`](scripts/record_demo.py) from a fresh database, so it can be re-made after any change.

`165 tests (159 on SQLite + 6 Postgres-only RLS tests)` · `92.7 % line + branch coverage (gate 92 %)` · `33 of 33 injected bugs caught` · `ruff` · `mypy` · `bandit` · `pip-audit` · MIT

## Quick start

```bash
git clone https://github.com/zvirmykhailo1987-cyber/garage-os.git && cd garage-os
docker compose up            # → http://localhost:8000
```

That starts Postgres 17, runs a one-shot migration (schema, RLS policies, grants, plans and
**synthetic** demo data) as the database owner, then the web app as a restricted role that
cannot bypass row-level security. Sign in with any account listed on the login page —
password `demo-password-2026`. [`DEMO.md`](DEMO.md) is a three-minute walkthrough.

No Docker? `python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, then
`DEMO_MODE=1 uvicorn app.main:app` runs the same app on SQLite.

## What it does

| Area | Highlights |
|---|---|
| Work orders | intake form, labour and part lines, VAT-included totals computed by code, optimistic locking (version + conflict banner), invoice / print view |
| Board | drag-and-drop status changes with optimistic UI that **rolls the card back** on 409 / 403 / network errors |
| Customers & stock | search, visit history, low-stock filter, prices pulled from stock by SKU |
| Fleet contracts | vehicles per contract, mileage-based maintenance due, monthly acts |
| Roles | network owner · branch manager · receptionist · mechanic — a deny-by-default matrix checked on every route |
| Plans | trial / single / network: branch and user limits, AI on/off, suspended = read-only |
| AI (human in the loop) | note → draft work order, plain-language search → visible SQL filters; strict JSON schemas, one retry with the validation error, deterministic offline fallback, every run logged |
| Telegram | bot talks only to the REST API; a chat sees only the client it was linked to with a one-time code |
| Offline sync | per-device tokens, versioned push with **409 + conflict record** instead of last-write-wins |
| Import | 1C/BAF CSV → proposed mapping → explicit confirmation → deterministic import → reconciliation |
| i18n | English (default) and Ukrainian, every key checked by a test |

## Architecture

```
Browser ── Jinja2 pages + 7 KB vanilla JS (no inline script/style → strict CSP)
   │
FastAPI ── web/  (session cookie + CSRF)    api/ (bearer tokens, scopes)    sync/ (device tokens)
   │            └──────────── get_ctx / api_principal / sync_device ───────────┘
   │                          resolve the caller → set_tenant(session, org_id)
   │
tenancy.py ── scoped() / get_scoped() / find_order()   ← the only way to read tenant rows
   │
SQLModel ─── SQLite (dev)  │  Postgres: SET LOCAL app.current_org per transaction
                           │            + RLS policy  org_id = current_setting(...)
```

* **Two isolation layers.** Every query is filtered by `tenancy.scoped()`; on Postgres the
  same session also carries `app.current_org`, and RLS policies on all 21 tenant tables
  (including branches and memberships) refuse anything else. A test proves *no context ⇒ 0 rows* for every table, that a cross-tenant
  `INSERT` fails, and that the setting never leaks through the connection pool.
* **404, never 403, across tenants.** Another organization's order looks exactly like a
  missing one — in the UI, the REST API, the Telegram assistant and plate lookup.
* **The operator console** manages tenants but cannot read tenant rows; on Postgres the
  per-org counts come from a single `SECURITY DEFINER` aggregate.
* **Counters, not scans.** Order counts per status are kept in a `StatusCount` table, updated
  atomically (`INSERT … ON CONFLICT DO UPDATE SET n = n + δ`) by a session hook in the same
  transaction as the order change — navigation badges never count the order table.

## Security model

| Control | Implementation | Regression test |
|---|---|---|
| Passwords | argon2id; legacy `sha256` hashes accepted once and re-hashed on login | `test_auth.py::test_legacy_sha256_hash_is_rehashed_on_login` |
| Sessions | random token in an HttpOnly/SameSite cookie, only its hash stored; absolute + idle expiry; logout revokes | `test_logout_revokes_server_session`, `test_expired_and_idle_sessions_are_rejected` |
| CSRF | synchronizer token on every unsafe request; signed double-submit before login | `test_every_unsafe_web_route_rejects_missing_csrf` (enumerates all POST routes) |
| Brute force | per-IP rate limit + per-account lockout | `test_account_lockout_after_repeated_failures` |
| Headers | CSP without `unsafe-inline`, `frame-ancestors 'none'`, nosniff, HSTS behind TLS | `test_security_headers_and_strict_csp` |
| Authorization | role matrix on the route; plan limits; suspended = read-only | `test_roles_plans.py` |
| Tenant isolation | app filter + Postgres RLS | `test_isolation.py`, `test_rls.py` |
| IDOR | line deletes check the parent order and tenant | `test_idor_delete_line_of_another_order_in_same_tenant` |
| XSS | Jinja autoescaping, no `innerHTML` in JS, CSP | `test_frontend_never_injects_html` |
| Sync | one token per device (a device acts with receptionist rights; fiscalised orders and the money fields of paid orders are locked for devices), tenant-scoped export, no users or hashes, typed payload validation, partial rows never wipe relations | `test_sync.py`, `test_review_regressions.py` |
| Money | mechanics cannot set prices; paid orders need the pricing role; fiscalised orders are frozen; row locks on write paths | `test_mechanic_cannot_set_prices`, `test_paid_and_fiscalised_orders_are_frozen` |
| Request size | bodies capped before anything reads them (4 MB forms, 6 MB sync); anonymous writes are refused before the SQLite write lock | `test_oversized_and_anonymous_writes_are_refused_early` |
| Defaults | production refuses a missing/short secret and `DEMO_MODE`; `/docs` closed | `test_production_refuses_unsafe_config`, `test_api_docs_closed_by_default` |

## Quality gates (CI)

`ruff` · `mypy` · `bandit` · `pip-audit` · `pytest` on **SQLite and on Postgres with RLS**
· line + branch coverage gate 92 % · `docker compose` smoke test that signs in with a demo account.

**Coverage** (branch coverage on): SQLite 94.8 % of lines and 85.2 % of branches (92.7 % combined);
Postgres 95.6 % / 86.5 % (93.7 %). The gate is the lower combined figure, rounded down.
There are no `# pragma: no cover` comments. The one exclusion rule in `pyproject.toml` skips the
18 `raise RuntimeError("invariant: …")` lines (`grep -rn '"invariant:' app` lists them: `app/web/views.py` 5,
`app/demo.py` 4, `app/web/orders.py` 3, `app/web/auth.py` 2, and one each in `app/api/deps.py`,
`app/security.py`, `app/services/orders.py`, `app/web/admin.py`). Reason: each one narrows an `Optional`
for mypy right after a flush or commit has set the value, so it cannot run.

**Mutation check:** 33 of 33 injected bugs caught. [`scripts/mutation_check.py`](scripts/mutation_check.py)
removes one guarantee at a time from a temporary copy (shared-row tenant filter, lookup by id or number
across tenants, the RLS tenant setting, RLS on branches and memberships, the startup check of the database
role, VAT maths, hub-computed totals, prices in an LLM draft, the pricing and fiscal roles, frozen fiscal
orders, optimistic versions on the form, the board and sync, conflict records, device and API token
revocation, scope and tenant, the bot's chat binding, read-only suspended plans, line IDOR, lockout,
session revocation and idle expiry, CSRF before and after login). It runs the whole suite on SQLite
and on Postgres with RLS for each one. Results: [`docs/mutation-results.json`](docs/mutation-results.json).
The first run caught 30 of 33. The three survivors were real gaps, and tests now cover them (the org check
on another tenant's shared rows, the RLS role guard refusing the owner role, and an LLM draft that carries
a price). Run it with `python scripts/mutation_check.py`. It starts and removes a throwaway
`postgres:17-alpine` container, or uses `STO_TEST_DATABASE_URL` / `_ADMIN_URL` if you set them.
It takes about an hour.

## Performance

`scripts/bench.py` loads a synthetic database (**200 000 work orders**, 20 000 clients) and
measures whole authenticated HTTP pages — session lookup, tenant scoping, SQL and template
rendering — not query fragments (in-process ASGI, network excluded; SQLite, Windows laptop,
40 runs per page). Full results: [`docs/benchmarks.json`](docs/benchmarks.json).

| Page | p50 | p95 | SQL statements |
|---|---|---|---|
| Dashboard | 18 ms | 24 ms | 15 |
| Work orders, page 1 | 26 ms | 34 ms | 12 |
| Work orders, page 1000 | 59 ms | 122 ms | 12 |
| Search by plate | 78 ms | 99 ms | 12 |
| Board | 58 ms | 227 ms | 16 |
| Clients | 24 ms | 30 ms | 11 |
| Order card | 19 ms | 24 ms | 17 |

The first run of this benchmark measured 0.6–1.8 s per page; it found three real problems
(a status count scanning every order on every page, an unindexable `LIKE '%q%'` search and
missing planner statistics), all fixed. The SQL count per page does not grow with data size
— `tests/test_perf.py` guards that.

## Project layout

```
app/            FastAPI app: web/, api/, sync/, services/, models/, tenancy, security, rls
ai/             LLM client, schema-validated runner, offline extractors, JSON schemas, prompts
integrations/   plate registry, telephony, messaging, fiscal — free stubs + documented seams
tg_bot/         Telegram bot (aiogram, async httpx → REST API)
templates/ static/   server-rendered UI, design tokens, light/dark, EN/UK
tests/          165 tests; the 6 Postgres-only RLS tests run when STO_TEST_DATABASE_URL is set
deploy/initdb/  creates the restricted database role on first start
```

## Development

```bash
pip install -r requirements-dev.txt
pytest -q --cov                        # SQLite
STO_TEST_DATABASE_URL=postgresql+psycopg://sto_app:...@localhost/sto \
STO_TEST_DATABASE_ADMIN_URL=postgresql+psycopg://sto_owner:...@localhost/sto pytest -q
ruff check . && mypy app ai integrations tg_bot
python scripts/bench.py --orders 200000
python scripts/record_demo.py           # demo video + screenshots (needs Playwright)
```

## Known limitations

* The rate limiter is in-process: correct for the single-container deployment shipped here;
  several replicas would need a shared store (account lockout is already persistent).
* Integrations (plate registry, SMS, PBX, fiscal register) are offline stubs with a
  documented seam — nothing is sent anywhere.
* The offline fallback for AI is a keyword parser, not a language model; set `LLM_PROVIDER`
  to any OpenAI-compatible endpoint for free-form input.
* Edge (offline) copies keep their own user accounts — credentials are intentionally not part
  of the sync protocol — and number their orders in their own space (`KYI-7A3F00001`), so an
  order created offline never collides with one created on the hub.
* SQLite (dev / edge copies) serialises writers with `BEGIN IMMEDIATE`; Postgres uses
  row locks (`SELECT … FOR UPDATE`) on write paths.
* Signup does not verify e-mail addresses.

## License

[MIT](LICENSE) © 2026 Mykhailo Zvir. Keep the copyright notice in copies and derived work. Fonts: Inter and JetBrains Mono under the SIL OFL ([static/fonts/OFL.txt](static/fonts/OFL.txt)).
