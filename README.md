# Suliko CRM — API

FastAPI backend for `app.suliko.ge`. Multi-tenant translation-bureau CRM.

Specs are one level up in [`../docs/`](../docs/); the build plan is
[`../docs/BUILD-WITH-FASTAPI.md`](../docs/BUILD-WITH-FASTAPI.md). This README is how to run it.

```bash
docker compose up -d                  # Postgres 17 + Redis (or use a native install)
uv venv --python 3.13
uv pip install -e ".[dev]"
cp .env.example .env                  # then set ENCRYPTION_MASTER_KEY

python -m suliko.cli bootstrap        # creates DB, migrates, seeds, makes a superuser
python -m suliko.cli check            # verifies config + connectivity

uvicorn suliko.main:app --reload      # http://localhost:8000/docs
```

The CLI also has discrete commands — `create-database`, `migrate`,
`create-tenant`, `seed-reference`, `create-superuser`, `check`. A superuser can
only be made here, never through the API: there is no sign-up path and no
"first user becomes admin" rule.

```bash
pytest -q                             # 330 tests
ruff check . && ruff format --check .
mypy src
```

## Status

Phase 1 (foundations) complete. Auth, tenancy, RBAC, 2FA, audit and the pricing engine are
built and tested. One resource router (`clients`) exists as the reference implementation; the
rest follow it. See the build doc for what is and is not done.

**The initial migration has never run against a live Postgres** — Docker was unavailable when
it was written. Verify it, then write `tests/test_rls.py` before trusting row-level security.

## Translator portal (suliko.ge)

Translators sign in to **suliko.ge**, not to this API. suliko.ge's Next.js server checks their
login with its own .NET backend, then calls `/api/v1/portal/*` with a signed assertion saying
which suliko.ge user is acting (`security/portal_tokens.py`, key `PORTAL_SHARED_SECRET`, the same
value as suliko-front's `SULIKO_PORTAL_SECRET`).

- **Admin** (`/api/v1/portal-admin/*`, suliko.ge admins only): mark a suliko.ge account as a
  translator, link it to any number of bureaus — each link points at that bureau's own
  `translators` row, existing or new — and record each bureau's Google Shared Drive.
- **Assigned orders**: a document shows up in the translator's Orders tab when staff set its
  `translator_id` to a linked row (`PATCH /api/v1/orders/{id}/documents/{doc_id}`). Translators
  see order id, client name, due date and **only their own documents** — no prices.
- **Files**: `Suliko Orders/#123 · Client/Document 456 · en → ka/{Source,Translation}` in the
  bureau's Shared Drive. Staff drop sources straight into Drive; translators upload translations.
  Browsers transfer files directly with short-lived tickets, because Vercel caps function bodies
  at 4.5 MB.
- **Personal orders**: a translator's own orders, visible to no bureau; files in the database.

Drive setup: create a Google service account, download its JSON key, point
`GOOGLE_SERVICE_ACCOUNT_FILE` at it, and have each bureau add the account's email to a Shared
Drive as **Content manager**. A service account has no storage quota of its own, so a folder in
someone's My Drive will not work.

`portal_translators`, `portal_translator_links` and `personal_*` are deliberately platform-level
(no RLS): the portal must find a translator's bureaus before any tenant is bound. Everything read
*inside* a bureau still goes through `tenant_scope`. See `models/portal.py`.

## Stack

Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · asyncpg · Alembic ·
PostgreSQL 17 · Redis · argon2-cffi · pyotp · structlog. Tooling: uv, ruff, mypy strict, pytest.

## Layout

```
src/suliko/
├── config.py          settings; refuses to start insecurely in production
├── main.py            app factory
├── db/
│   ├── tenancy.py     ★ tenant isolation — read this first
│   ├── base.py        declarative base, TenantScoped mixin
│   └── session.py     async engine; sets the RLS GUC per transaction
├── models/            25 models, all tenant-scoped except tenants + audit_log
├── domain/
│   ├── pricing.py     ★ the pricing engine — ported from the PHP
│   └── statuses.py    the status registry — ported verbatim
├── security/
│   ├── passwords.py   Argon2id, transparent bcrypt upgrade
│   ├── totp.py        TOTP with replay protection
│   ├── permissions.py 5 roles as permission bundles
│   └── sessions.py    opaque server-side sessions
├── core/              errors, crypto, ratelimit, audit
└── api/
    ├── deps.py        ★ the dependency chain IS the security model
    └── v1/            auth.py, clients.py (the reference router)
```

## Things that will bite you

**Tenant isolation is three layers, all of them load-bearing.** A ContextVar bound from the
session, an ORM event that filters every query and stamps every insert, and PostgreSQL RLS.
`tenant_id` comes from the session and *never* from user input — not a header, not a query
param, not a body field, not a subdomain. Read `db/tenancy.py` before touching anything.

**Never write `WHERE tenant_id = …` by hand.** The ORM adds it. Writing it manually implies
forgetting is possible, which is the belief this design exists to remove.

**Dependency order in `api/deps.py` is deliberate.** `get_current_session` binds the tenant
*before* `get_db` opens a connection, because `session_scope` writes the `suliko.tenant_id`
GUC that RLS reads. Reverse them and RLS silently sees no tenant.

**A resource in another tenant is 404, never 403.** 403 confirms it exists.

**Money is `Decimal` end to end** and quantised with `ROUND_HALF_UP` — half *away from zero*,
matching PHP's `number_format`. Python's `round()` is banker's rounding and would disagree with
every existing invoice on exact halves.

**Status values are stored verbatim**, including the `payed` misspelling and the
space-separated ones. `domain/statuses.py` is the only place a label or colour exists, and
`tests/test_parity.py` asserts it matches the TypeScript copy.

**`bypass_tenant_scope()` is a loaded gun.** Platform code and migrations only, always with an
audit entry. If you reach for it in a feature handler, the design is wrong.

## Verified

117 tests, `ruff` clean, `ruff format --check` clean, `mypy --strict` clean on 35 files.
The app boots: `/health` returns 200, an unauthenticated `/api/v1/clients` returns 401.

Not verified: anything needing a live database — the migration, RLS, and the auth endpoints
end to end.
