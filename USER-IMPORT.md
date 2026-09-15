# Importing existing users

Status: **not built.** Written 2026-09-15, during the first production deploy.

There is currently no way to create a user other than a platform superuser.
This note records what is missing, what already exists to build on, and the
decisions already made, so whoever picks this up does not have to re-derive it.

---

## 1. What is missing

| Path | create user | list users | change password | reset password |
|---|---|---|---|---|
| HTTP API (`api/v1`) | ✗ | ✗ | ✗ | ✗ |
| CLI (`suliko.cli`) | superuser only | ✗ | ✗ | ✗ |

`api/v1` contains exactly two routers: `auth.py` (login, mfa/verify, logout,
session) and `clients.py`. The CLI's only user command is `create-superuser`,
which hard-codes `Role.SUPERUSER` and prompts interactively via `getpass`.

**Do not bulk-create accounts with `create-superuser`.** Every one of them
would hold platform-wide reach across all tenants, which is the exact thing
`cli.py`'s module docstring exists to prevent.

## 2. What already exists to build on

Reusable as-is:

- `suliko.security.passwords` — `hash_password`, `validate_password_strength`
  (12-char minimum, returns a list of problems).
- `suliko.db.tenancy` — `tenant_scope(tenant_id)` stamps `tenant_id` onto new
  rows automatically. **`install_tenant_filter()` must have been called first**
  or every insert lands with `tenant_id` NULL; `cli.main()` now does this.
- `suliko.models.user.User` — `username`, `email`, `full_name`,
  `password_hash`, `role`, `is_active`.

Roles: `superuser`, `owner`, `admin`, `manager`, `staff`.

MFA is mandatory only for `superuser`, `owner`, `admin`
(`permissions.MFA_REQUIRED_ROLES`). `manager` and `staff` are exempt, so a
bulk import of ordinary staff does **not** need TOTP enrolment per user.

Login already returns `mfa_enrolment_required` when a user's role demands a
second factor and none is confirmed, so admin-tier users can enrol on first
login rather than at creation time.

## 3. The blocker to resolve first

**There is no change-password or reset-password endpoint.** Whatever credential
a user is given at import, they cannot rotate it themselves.

Handing 300 people a permanent password they cannot change is not acceptable
for real accounts. Build change-password (and ideally an admin-triggered reset)
*before* the import, not after.

## 4. Where the existing users probably live

Not yet located. Candidates on this host, in rough order of likelihood:

- The **native PostgreSQL 16** Windows service listening on `127.0.0.1:5432`
  (service `postgresql-x64-16`). This is *not* the Suliko database — ours is
  the Docker container on `5433`. Check here first.
- The legacy PHP application's own database. The PHP tree also contains a
  hardcoded SMSOffice API key (see the deploy runbook §8) which must be rotated
  regardless.

Whatever the source, the password hashes almost certainly will not be in
Argon2 form. Decide deliberately between:

- **Re-hashing on first login** — import the legacy hash into a separate
  column, verify against it once, then upgrade to `hash_password` and discard.
  Preserves existing passwords; needs a legacy-verify path in `login`.
- **Fresh credentials for everyone** — simpler and cleaner, but requires
  distributing 300 new passwords, which needs §3 solved first.

## 5. Decisions already taken

Agreed during the deploy, for whenever this is built:

- CSV import, default role `staff` when the column is absent.
- A username already present in the tenant is **skipped with a warning**; the
  remaining rows continue. A re-run after a partial failure is therefore safe.

Suggested input columns — `username` and `email` required, rest optional:

```csv
username,email,full_name,role
nino,nino@suliko.ge,Nino Beridze,staff
giorgi,giorgi@suliko.ge,Giorgi Kapanadze,manager
```

Sketch of the command:

```
python -m suliko.cli import-users --tenant suliko --from-csv users.csv \
    [--default-role staff] [--dry-run] [--write-passwords out.csv]
```

`--dry-run` should validate the whole file — duplicate usernames within the
file, invalid roles, malformed emails, collisions with existing rows — and
report without writing anything. For 300 rows, finding out halfway through is
much worse than a slow check up front.

If passwords are generated, write them to a separate output file, never to
stdout, and treat that file as a secret to be destroyed after distribution.
The `.env` generator used during deployment follows the same rule and is a
reasonable model.
