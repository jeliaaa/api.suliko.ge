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
pytest -q                             # 117 tests
ruff check . && ruff format --check .
mypy src
```

## Status

Phase 1 (foundations) complete. Auth, tenancy, RBAC, 2FA, audit and the pricing engine are
built and tested. One resource router (`clients`) exists as the reference implementation; the
rest follow it. See the build doc for what is and is not done.

**The initial migration has never run against a live Postgres** — Docker was unavailable when
it was written. Verify it, then write `tests/test_rls.py` before trusting row-level security.

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
