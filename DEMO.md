# Demo script (≈3 minutes)

Start: `docker compose up` → open <http://localhost:8000>. All data is synthetic
(phone numbers use the non-existent `+380 00` code, e-mails use `.test`).
Every demo account uses the password **`demo-password-2026`**; the login page lists them
and fills the form with one click.

| Account | Role | What it shows |
|---|---|---|
| `owner@northwind.test` | Network owner, 3 branches | everything inside one tenant |
| `manager.kyiv@northwind.test` | Branch manager (Kyiv) | branch-level admin, fiscal receipts |
| `desk.lviv@northwind.test` | Receptionist (Lviv) | intake, customers; no catalog / money admin |
| `mechanic.kyiv@northwind.test` | Mechanic (Kyiv) | works the order, cannot touch prices |
| `owner@riverside.test` | Owner, plan without AI | plan gating |
| `owner@halted.test` | Owner, suspended plan | read-only mode |
| `operator@platform.test` | Platform operator | tenants and plans, never tenant data |

## 1. The workshop (owner) — 60 s
1. Sign in as **owner@northwind.test**. The dashboard shows live KPIs and the pipeline.
2. Switch the **Branch** selector (top bar) from *Whole network* to *Kyiv · Podil* — every
   list, count and KPI is re-scoped.
3. **New intake** → paste into *Draft from a note*:
   `VW Passat AA 1234 BC, knocking at the front, change brake pads and oil` → **Make a draft**.
   Plate, make and complaint are filled; suggested jobs are listed for review. Nothing is
   saved until you press **Create work order** (human in the loop; money is never computed
   by the model).
4. In the new order card add a labour line and a part from stock (type an SKU, e.g. `BP-2040`,
   the price comes from stock). Totals are recomputed (VAT is included, Ukrainian convention).

## 2. Board and concurrency — 30 s
5. **Board** → drag a card to another column; the status is saved.
6. Open the same order in two tabs, save one, then save the other: the second save is refused
   with a conflict banner (optimistic locking). On the board a stale card snaps back with a
   toast instead of silently overwriting.

## 3. Roles and plans — 40 s
7. Sign in as **mechanic.kyiv@northwind.test**: no discount / payment fields, no fiscal
   receipt, no customer list. The server enforces it too — the routes answer 403.
8. Sign in as **owner@riverside.test**: AI search and drafts are not in this plan.
9. Sign in as **owner@halted.test**: a read-only banner; every write answers 403.

## 4. Tenant isolation — 30 s
10. As the Northwind owner open any order and note its URL, e.g. `/orders/12`. Sign in as
    **owner@riverside.test** and open the same URL: **404**, exactly like a missing page —
    no hint that the row exists. The same holds for the REST API, the assistant, plate lookup
    and the sync endpoint (see `tests/test_isolation.py`).
11. **operator@platform.test** sees organizations, plans and counts, but opening a tenant's
    order is a 404 too. On Postgres the counts come from a single aggregate function while
    tenant rows stay behind row-level security.

## 5. Channels — 20 s
12. **Telegram assistant**: enter an order number in *estimate* status (the board's
    *Estimate* column) → **Approve estimate**. The real bot (`--profile bot`) does the same over
    the REST API, but only for the client whose chat was linked with a one-time code
    (client card → *Generate link code*).
13. **Settings** → create an API token (shown once, stored as a hash) or an offline sync device.

## Optional
* Language: **EN / UK** switch in the sidebar footer. Theme: sun/moon button (follows the OS by default).
* REST API: Settings → API tokens → *Integration* with `orders:read`, then
  `curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/orders/KYI-001001/status`.
* API docs are closed by default; for local exploration run with `ENABLE_API_DOCS=1` (never in production).
