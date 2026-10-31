"""Operational CLI: database bootstrap, tenants, superuser, reference data.

    python -m suliko.cli bootstrap          # everything, interactively
    python -m suliko.cli create-database
    python -m suliko.cli migrate
    python -m suliko.cli create-tenant --slug acme --name "Acme Translations"
    python -m suliko.cli create-superuser --tenant acme
    python -m suliko.cli seed-reference --tenant acme
    python -m suliko.cli import-suliko-users --dry-run
    python -m suliko.cli link-suliko-account --email me@example.com --suliko-login 599123456
    python -m suliko.cli import-tarjimnebi --tenant SLUG --dump FILE [--commit]
    python -m suliko.cli check
    python -m suliko.cli purge-files        # daily: reclaim removed order files
    python -m suliko.cli deletion-requests  # accounts closed by a request to delete them
    python -m suliko.cli cancel-deletion-request --id 7
    python -m suliko.cli reset-account --login me@example.com   # shows the new-user flow again
    python -m suliko.cli purge-accounts     # daily: delete the data of approved requests

The FIRST superuser is created here. There is no sign-up path, no "first user
becomes admin" rule, and no way for a tenant's own roles to escalate into it.
The one way over HTTP is `POST /platform/superusers`, which an existing
superuser uses to let named suliko.ge accounts in (decided 2026-10-13; see
`api/v1/platform.py`).

See docs/03-SECURITY-AND-TENANCY.md §3.1.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy import select, text

from suliko.config import get_settings
from suliko.core.crypto import encrypt_for_tenant
from suliko.core.errors import ValidationError
from suliko.db.session import (
    bind_tenant_guc,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    session_scope,
)
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.account_deletion import (
    DELETION_GRACE_DAYS,
    done_email,
    due_for_purge,
    open_requests,
    purge_account,
    reject_request,
)
from suliko.domain.accounts import UNUSABLE_PASSWORD_HASH, find_account, login_name, normalise_email
from suliko.domain.reference_seed import seed_reference_data
from suliko.domain.suliko_import import import_users, link_by_hand
from suliko.integrations.object_storage import (
    StorageError,
    VaultStorage,
    close_object_storage,
    get_object_storage,
)
from suliko.integrations.suliko_backend import (
    SulikoUnavailableError,
    close_suliko_backend,
    get_suliko_backend,
)
from suliko.models.directory import Client, ClientType  # noqa: F401 — registry
from suliko.models.reference import TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import (
    Account,
    AccountDeletionRequest,
    MfaMethod,
    MfaRecoveryCode,
    Role,
    User,
)
from suliko.security import totp as totp_service
from suliko.security.passwords import hash_password, hash_token, validate_password_strength

ROOT = Path(__file__).resolve().parents[2]


# ── Output helpers ──────────────────────────────────────────────────────────


def say(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201 — this is a CLI


def ok(message: str) -> None:
    say(f"  [ok]   {message}")


def warn(message: str) -> None:
    say(f"  [warn] {message}")


def fail(message: str) -> None:
    say(f"  [FAIL] {message}")


def heading(message: str) -> None:
    say(f"\n{message}\n{'-' * len(message)}")


# ── Database creation ───────────────────────────────────────────────────────


def _dsn_parts() -> tuple[str, str]:
    """Split the configured URL into an admin DSN and the database name.

    Creating a database cannot happen from inside that database, so this
    connects to the maintenance database ``postgres`` instead.
    """
    url = str(get_settings().database_url)
    # asyncpg wants a plain postgresql:// DSN, not SQLAlchemy's +asyncpg form.
    plain = url.replace("postgresql+asyncpg://", "postgresql://")
    base, _, dbname = plain.rpartition("/")
    dbname = dbname.split("?")[0]
    return f"{base}/postgres", dbname


async def create_database() -> bool:
    """Create the database if it does not exist. Returns True if it created one."""
    admin_dsn, dbname = _dsn_parts()

    if not re.fullmatch(r"[A-Za-z0-9_]+", dbname):
        raise SystemExit(f"Refusing to create a database with an unsafe name: {dbname!r}")

    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
        if exists:
            ok(f"database {dbname!r} already exists")
            return False
        # CREATE DATABASE cannot run inside a transaction, which is why this
        # uses a raw asyncpg connection rather than the SQLAlchemy engine.
        # The name is validated above; identifiers cannot be bound.
        await conn.execute(f"CREATE DATABASE \"{dbname}\" ENCODING 'UTF8'")
        ok(f"created database {dbname!r}")
        return True
    finally:
        await conn.close()


def migrate() -> None:
    """Run ``alembic upgrade head`` as a subprocess.

    A subprocess rather than Alembic's Python API: Alembic configures its own
    logging and event loop, and running it in-process inside an async CLI
    causes hard-to-diagnose interference.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("alembic upgrade failed: see the output above")
    ok("migrations applied")


# ── Tenants ─────────────────────────────────────────────────────────────────

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


async def create_tenant(slug: str, display_name: str, locale: str = "ka") -> int:
    if not SLUG_PATTERN.fullmatch(slug):
        raise SystemExit(
            f"Invalid slug {slug!r}: lowercase letters, digits and hyphens, 2-63 characters."
        )

    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            existing = (
                await db.execute(select(Tenant).where(Tenant.slug == slug))
            ).scalar_one_or_none()
            if existing:
                ok(f"tenant {slug!r} already exists (id {existing.id})")
                return int(existing.id)

            tenant = Tenant(
                slug=slug,
                display_name=display_name,
                status=TenantStatus.ACTIVE,
                # A console-created organisation is a bureau set up by the
                # platform, not a self-signup mid-onboarding. With no plan it
                # was enforced as a freelancer — which, among other things,
                # withheld every platform permission from the superuser
                # `bootstrap` creates inside it.
                plan="bureau",
                locale=locale,
            )
            db.add(tenant)
            await db.flush()

            # One settings row per tenant, carrying the pricing knobs.
            db.add(TenantSettings(tenant_id=tenant.id, default_language=locale))
            tenant_id = int(tenant.id)

        # The same starter catalogues a self-signup gets — without them the
        # first "New order" has no document type to choose. Outside the bypass
        # block: under it new rows are not stamped with their tenant.
        await bind_tenant_guc(db, tenant_id)
        with tenant_scope(tenant_id):
            await seed_reference_data(db)
        await db.commit()

        ok(f"created tenant {slug!r} (id {tenant_id}), with starter catalogues")
        return tenant_id


# ── Superuser ───────────────────────────────────────────────────────────────


def _prompt_password() -> str:
    while True:
        password = getpass.getpass("  Password (min 12 chars, not echoed): ")
        problems = validate_password_strength(password)
        if problems:
            for problem in problems:
                fail(problem)
            continue
        if password != getpass.getpass("  Confirm: "):
            fail("Passwords did not match.")
            continue
        return password


async def create_superuser(
    tenant_slug: str,
    username: str,
    email: str,
    full_name: str,
    password: str | None = None,
) -> None:
    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
            ).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"No tenant with slug {tenant_slug!r}. Create it first.")

        tenant_id = int(tenant.id)

        with tenant_scope(tenant_id):
            clash = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if clash:
                raise SystemExit(f"User {username!r} already exists in tenant {tenant_slug!r}.")

            # One password per person: an existing account keeps its own.
            account = await find_account(db, email)
            if account is None:
                if password is None:
                    password = _prompt_password()
                account = Account(
                    email=normalise_email(email),
                    password_hash=hash_password(password),
                    full_name=full_name,
                    # Created from a shell on the server itself.
                    email_verified_at=datetime.now(UTC),
                )
                db.add(account)
                await db.flush()

            user = User(
                account_id=account.id,
                username=username,
                email=normalise_email(email),
                full_name=full_name,
                password_hash=UNUSABLE_PASSWORD_HASH,
                role=Role.SUPERUSER,
                is_active=True,
                email_verified_at=datetime.now(UTC),
            )
            db.add(user)
            await db.flush()

            # With two-factor switched off (MFA_ENFORCED=false) there is no
            # factor to enrol: it would never be asked for. They can add one
            # from their Account screen once it is switched back on.
            if not get_settings().mfa_enforced:
                await db.commit()
                heading("Superuser created")
                say(f"  username : {username}")
                say(f"  tenant   : {tenant_slug}")
                say("")
                warn("Two-factor is switched off (MFA_ENFORCED=false): no factor was enrolled.")
                return

            # Superusers must hold a second factor — enrol it now rather than
            # leaving a window where the most privileged account has none.
            secret = totp_service.generate_secret()
            db.add(
                MfaMethod(
                    user_id=user.id,
                    method_type="totp",
                    secret_encrypted=encrypt_for_tenant(tenant_id, secret),
                    # Confirmed immediately: this enrolment happens on the
                    # server console, which is stronger evidence of identity
                    # than the usual prove-a-code flow over HTTP.
                    # Confirmed immediately: this enrolment runs on the server
                    # console, which is stronger evidence of identity than the
                    # usual prove-a-code-over-HTTP flow.
                    confirmed_at=datetime.now(UTC),
                )
            )

            recovery_codes = totp_service.generate_recovery_codes()
            for code in recovery_codes:
                db.add(
                    MfaRecoveryCode(
                        user_id=user.id,
                        code_hash=hash_token(totp_service.normalise_recovery_code(code)),
                    )
                )

            await db.commit()

        uri = totp_service.provisioning_uri(secret, f"{username}@{tenant_slug}", "Suliko Office")

        heading("Superuser created")
        say(f"  username : {username}")
        say(f"  tenant   : {tenant_slug}")
        say("")
        say("  Scan this in your authenticator app, or enter the secret by hand:")
        say(f"    secret : {secret}")
        say(f"    uri    : {uri}")
        say("")
        say("  Recovery codes: each works once. Store them somewhere safe;")
        say("  they are shown now and never again:")
        for code in recovery_codes:
            say(f"    {code}")
        say("")
        warn("This output contains the TOTP secret. Clear your scrollback when done.")


# ── suliko.ge's people ──────────────────────────────────────────────────────


def _require_suliko_backend() -> Any:
    if not get_settings().suliko_backend_enabled:
        raise SystemExit(
            "SULIKO_API_URL is not set, so there is no suliko.ge to import from. "
            "Set SULIKO_API_URL and SULIKO_API_KEY in .env first."
        )
    return get_suliko_backend()


async def import_suliko_users(dry_run: bool) -> None:
    """Give every person registered on suliko.ge an account here.

    Safe to run again. `--dry-run` writes nothing and lists what a real run
    would do, including which existing accounts would switch to a suliko.ge
    password: read that list before the real run.
    """
    backend = _require_suliko_backend()
    try:
        async with get_sessionmaker()() as db:
            report = await import_users(db, backend, dry_run=dry_run)
            if dry_run:
                await db.rollback()
            else:
                await db.commit()
    except SulikoUnavailableError as exc:
        raise SystemExit(f"Could not read suliko.ge's users: {exc.detail}") from exc
    finally:
        await close_suliko_backend()

    heading("DRY RUN: nothing was written" if dry_run else "Import finished")
    verb = "would be created" if dry_run else "created"
    say(f"  people on suliko.ge     : {report.seen}")
    say(f"  already in Office       : {report.already_linked}")
    say(f"  new accounts {verb:<19}: {report.created}")
    say(f"  existing accounts linked: {len(report.linked_existing)}")
    if report.linked_existing:
        warn("These Office accounts now sign in with their suliko.ge password; their")
        warn("old Office password stops working. Each person can reset it on suliko.ge:")
        for login in sorted(report.linked_existing):
            say(f"    {login}")
    if report.conflicts:
        warn(f"{len(report.conflicts)} sign-in(s) claimed by two different people; left alone:")
        for login in sorted(report.conflicts):
            say(f"    {login}")
    say(f"  Office accounts with no suliko.ge person: {len(report.office_only)}")
    for login in report.office_only:
        say(f"    {login}")
    if report.office_only:
        say("  (They keep their own password. If one of them signs in to suliko.ge with a")
        say("   phone number, link it: link-suliko-account --email … --suliko-login …)")


async def link_suliko_account(email: str, suliko_login: str) -> None:
    """Link one existing Office account to the suliko.ge person with this sign-in."""
    backend = _require_suliko_backend()
    try:
        async with get_sessionmaker()() as db:
            message = await link_by_hand(db, backend, office_email=email, suliko_login=suliko_login)
            await db.commit()
    except SulikoUnavailableError as exc:
        raise SystemExit(f"Could not ask suliko.ge: {exc.detail}") from exc
    except ValidationError as exc:
        raise SystemExit(exc.detail) from exc
    finally:
        await close_suliko_backend()
    ok(message)


# ── Account deletion requests ───────────────────────────────────────────────


async def deletion_requests() -> None:
    """List the requests that are keeping an account closed, oldest first."""
    async with get_sessionmaker()() as db:
        pending = await open_requests(db)
    if not pending:
        say("No account is closed by a deletion request.")
        return
    heading("Accounts closed by a deletion request")
    for item in pending:
        name = item.account.full_name or login_name(item.account)
        filed = item.request.created_at.strftime("%Y-%m-%d %H:%M")
        say(
            f"  {item.request.id:>5}  {item.request.status.value:<9}  {filed}  "
            f"{name} <{login_name(item.account)}>"
        )
    say("\n  To let one back in: cancel-deletion-request --id <number>")


async def cancel_deletion_request(request_id: int) -> None:
    """Cancel a request, because the person wrote to say so or the review said no.

    Nothing is deleted. The account can sign in again, with every membership as
    it was.
    """
    from suliko.core.audit import record

    async with get_sessionmaker()() as db:
        try:
            request = await reject_request(db, request_id)
        except ValidationError as exc:
            raise SystemExit(exc.detail) from exc
        await record(
            db,
            None,
            action="account.deletion_cancelled",
            entity_type="account",
            entity_id=request.account_id,
            after={"status": request.status.value, "request_id": request.id},
        )
        await db.commit()
    ok(f"Request {request_id} is cancelled. The account can sign in again.")


async def reset_account(login: str) -> int:
    """Give a (test) account the new-user experience again. Deletes nothing."""
    from suliko.core.audit import record
    from suliko.domain.accounts import find_account_by_login, reset_to_new

    async with get_sessionmaker()() as db:
        account = await find_account_by_login(db, login)
        if account is None:
            fail(f"No account signs in as {login}.")
            return 1
        try:
            switched_off = await reset_to_new(db, account)
        except ValidationError as exc:
            fail(exc.detail)
            return 1
        await record(
            db,
            None,
            action="account.reset_to_new",
            entity_type="account",
            entity_id=account.id,
            after={"memberships_switched_off": switched_off},
        )
        await db.commit()
    ok(f"{login} is reset: {len(switched_off)} membership(s) switched off, tips cleared.")
    if switched_off:
        ids = ", ".join(str(i) for i in switched_off)
        say(f"  To undo: UPDATE users SET is_active = true WHERE id IN ({ids});")  # noqa: S608
    return 0


async def purge_accounts() -> int:
    """Delete the data of every approved request whose waiting time is over.

    Run daily. Each request is its own transaction, so one that fails does not
    undo or hold up the others; the person is told once their data is gone, at the
    address they had. Exit status 1 if any failed, for the scheduler to notice.
    """
    from suliko.core import mail
    from suliko.core.audit import record

    async with get_sessionmaker()() as db:
        due = [request.id for request in await due_for_purge(db)]
    if not due:
        say(f"Nothing to delete: no approved request is {DELETION_GRACE_DAYS} days old yet.")
        return 0

    failed = 0
    for request_id in due:
        try:
            async with get_sessionmaker()() as db:
                request = await db.get(AccountDeletionRequest, request_id)
                assert request is not None
                address, name = await purge_account(db, request)
                await record(
                    db,
                    None,
                    action="account.deleted",
                    entity_type="account",
                    entity_id=request.account_id,
                    after={"request_id": request_id},
                )
                await db.commit()
        except Exception as exc:  # one failure must not stop the rest
            failed += 1
            fail(f"request {request_id}: {type(exc).__name__}: {exc}")
            continue
        ok(f"request {request_id}: deleted")
        if address:
            message = done_email(name)
            await mail.send(address, message.subject, message.text, message.html)
    return 1 if failed else 0


# ── Reference data ──────────────────────────────────────────────────────────


async def seed_reference(tenant_slug: str, with_rates: bool = False) -> None:
    """Fill an organisation's catalogues. The data lives in domain/reference_seed."""
    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
            ).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"No tenant with slug {tenant_slug!r}.")

        with tenant_scope(int(tenant.id)):
            result = await seed_reference_data(db, with_rates=with_rates)
            await db.commit()

        ok(f"languages: {result.languages_added} added, {result.languages_present} already present")
        ok(
            f"document types: {result.document_types_added} added, "
            f"{result.document_types_present} already present"
        )
        if with_rates:
            ok(f"language pair rates: {result.rates_added} added")
            warn(
                "Those rates were read off a screenshot of the production "
                "Calculator and are an INCOMPLETE subset. Verify every line "
                "in Settings -> Pricing before quoting a client."
            )


async def import_tarjimnebi(
    tenant_slug: str,
    dump_path: str,
    *,
    commit: bool,
    keep_numbers: bool,
    package_tier: int | None = None,
    since: str | None = None,
) -> int:
    """Load a tarjimnebi dump into one organisation. Dry run unless ``commit``.

    With ``since`` (the dump the earlier import was made from) only what is new
    or changed since then is applied.
    """
    import json
    from pathlib import Path

    from suliko.domain.tarjimnebi_import import (
        RefusedError,
        SyncSource,
        import_dump,
        load_dump,
    )

    path = Path(dump_path)
    if not path.exists():
        fail(f"{path} does not exist.")
        return 1
    dump = load_dump(path)
    sync = None
    if since is not None:
        earlier = Path(since)
        if not earlier.exists():
            fail(f"{earlier} does not exist.")
            return 1
        sync = SyncSource(previous=load_dump(earlier))
        say(
            f"earlier dump: {len(sync.previous.clients)} clients, "
            f"{len(sync.previous.orders)} orders"
        )
    say(
        f"dump: {len(dump.clients)} clients, {len(dump.translators)} translators, "
        f"{len(dump.notaries)} notaries, {len(dump.orders)} orders"
    )

    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
            ).scalar_one_or_none()
        if tenant is None:
            fail(f"No tenant with slug {tenant_slug!r}.")
            return 1
        with tenant_scope(int(tenant.id)):
            try:
                report = await import_dump(
                    db, int(tenant.id), dump, keep_numbers=keep_numbers, sync=sync
                )
            except RefusedError as exc:
                await db.rollback()
                fail(str(exc))
                return 1
            for key, n in sorted(report.counts.items()):
                say(f"  {n:6d}  {key}")
            for line in report.warnings[:60]:
                warn(line)
            if len(report.warnings) > 60:
                warn(f"... and {len(report.warnings) - 60} more warnings")
            if package_tier is not None and package_tier > tenant.package_tier:
                say(f"  package: tier {tenant.package_tier} -> {package_tier}")
                tenant.package_tier = package_tier
            if not commit:
                await db.rollback()
                ok("Dry run: everything was inserted and rolled back. Nothing is saved.")
                return 0
            await db.commit()
            if sync is not None:
                ok("Committed.")
                return 0
            out = (
                path.with_suffix(".idmap.json")
                if path.is_file()
                else path / "tarjimnebi.idmap.json"
            )
            out.write_text(json.dumps(report.idmap, indent=1), encoding="utf-8")
            ok(f"Committed. Old-to-new ids written to {out}")
            return 0


# ── Health check ────────────────────────────────────────────────────────────


async def check() -> int:
    """Verify configuration and connectivity. Exit code 0 when healthy."""
    settings = get_settings()
    problems = 0

    heading("Configuration")
    say(f"  environment : {settings.environment}")
    say(f"  debug       : {settings.debug}")

    if settings.encryption_master_key.get_secret_value():
        ok("ENCRYPTION_MASTER_KEY is set")
    else:
        fail("ENCRYPTION_MASTER_KEY is not set")
        problems += 1

    # Shown explicitly because `extra="ignore"` on Settings means an unknown
    # or misspelled variable in .env is dropped in silence. Without this line,
    # "I set MFA_ENFORCED=false and nothing happened" has no cheap answer.
    if not settings.mfa_enforced:
        warn("MFA is DISABLED: every account signs in with a password alone")
    elif settings.mfa_require_enrolment:
        ok("MFA is ENFORCED, and enrolment is required of privileged roles")
    else:
        ok("MFA is ENFORCED: anyone with a factor is challenged for it")
        warn(
            "MFA_REQUIRE_ENROLMENT is false: owners/admins with no factor "
            "sign in on their password alone. Turn it on once enrolment ships."
        )

    if settings.redis_url:
        ok("Redis configured for rate limiting")
    elif settings.rate_limit_single_instance:
        ok("single-instance rate limiting (run exactly ONE worker)")
    else:
        fail("neither REDIS_URL nor RATE_LIMIT_SINGLE_INSTANCE is set")
        problems += 1

    if settings.is_production:
        try:
            settings.validate_for_production()
            ok("production validation passed")
        except RuntimeError as exc:
            fail(str(exc))
            problems += 1

    heading("Order file storage")
    storage = get_object_storage()
    if not storage.configured:
        fail("STORAGE_BACKEND is not set or incomplete: order files cannot be stored")
        problems += 1
    else:
        say(f"  backend     : {storage.description}")
        # The three calls the app makes, on a throwaway key. Proves the
        # credentials AND the permissions — a key that can read but not
        # write passes a mere "can I connect" check.
        key = f"_suliko/healthcheck/{secrets.token_hex(8)}"
        try:
            if isinstance(storage, VaultStorage):
                # The vault cannot be read back by design: prove the key and
                # the connection with a write and a delete. (This leaves one
                # empty "healthcheck" order in the vault's panel.)
                stored = await storage.put_file(
                    key,
                    b"%PDF-1.4 suliko healthcheck",
                    "application/pdf",
                    file_name="healthcheck.pdf",
                    kind="source",
                )
                await storage.delete(stored)
                ok("write and delete work (the vault cannot be read back by design)")
                if storage.keeps_working_copies:
                    # What translators download from while an order is open.
                    await storage.put_working(key, b"ok", "text/plain")
                    copy = b"".join([chunk async for chunk in storage.iter_working(key)])
                    await storage.delete_working(key)
                    if copy == b"ok":
                        ok("working copies: write, read and delete all work")
                    else:
                        fail("working copies read back different bytes than were written")
                        problems += 1
                else:
                    say(
                        "  STORAGE_WORKING_BACKEND is not set: no working copies are made, "
                        "so translators cannot download files"
                    )
            else:
                await storage.put(key, b"ok", "text/plain")
                body = b"".join([chunk async for chunk in storage.iter_get(key)])
                await storage.delete(key)
                if body == b"ok":
                    ok("write, read and delete all work")
                else:
                    fail("read back different bytes than were written")
                    problems += 1
        except StorageError as exc:
            fail(f"storage round trip failed: {exc}")
            problems += 1
        finally:
            await close_object_storage()

    heading("Database")
    try:
        async with get_sessionmaker()() as db:
            with bypass_tenant_scope():
                version = await db.scalar(select(text("version()")))
                ok(f"connected: {str(version).split(',')[0]}")

                tenants = (await db.execute(select(Tenant))).scalars().all()
                if tenants:
                    ok(f"{len(tenants)} tenant(s): {', '.join(t.slug for t in tenants)}")
                else:
                    warn("no tenants yet: run create-tenant")

                supers = (
                    (await db.execute(select(User).where(User.role == Role.SUPERUSER)))
                    .scalars()
                    .all()
                )
                if supers:
                    ok(f"{len(supers)} superuser(s)")
                else:
                    warn("no superuser yet: run create-superuser")
    except Exception as exc:  # a CLI should report, not traceback
        fail(f"cannot reach the database: {type(exc).__name__}: {exc}")
        problems += 1

    heading("Result")
    if problems:
        fail(f"{problems} problem(s) found")
    else:
        ok("all checks passed")
    return 1 if problems else 0


# ── Order file housekeeping ─────────────────────────────────────────────────


async def purge_files() -> int:
    """Delete the bytes of order files removed longer ago than the retention
    period. Safe to run as often as you like; schedule it daily.

    One tenant at a time, each inside its own scope, so row-level security
    applies exactly as it does to a request. Exit code 1 if any object could
    not be deleted — those rows are kept and retried next run.
    """
    from suliko.domain.order_files import purge_removed_files, purge_working_copies

    settings = get_settings()
    storage = get_object_storage()
    if not storage.configured:
        fail("STORAGE_BACKEND is not set: nothing to purge from")
        return 1

    cutoff = datetime.now(UTC) - timedelta(days=settings.file_retention_days)
    say(f"Purging files removed before {cutoff:%Y-%m-%d %H:%M} UTC from {storage.description}")

    async with get_sessionmaker()() as db, db.begin():
        with bypass_tenant_scope():
            tenants = (await db.execute(select(Tenant.id, Tenant.slug))).all()

    # With the Order Vault: also drop the translators' working copies of orders
    # that are closed, of removed files, and of anything older than the cap.
    copies_cutoff = datetime.now(UTC) - timedelta(days=settings.working_copy_max_days)

    purged = purged_bytes = failed = 0
    copies = copy_bytes = 0
    try:
        for tenant_id, slug in tenants:
            with tenant_scope(int(tenant_id)):
                async with session_scope() as db:
                    result = await purge_removed_files(db, storage, cutoff=cutoff)
                    gone = await purge_working_copies(db, storage, older_than=copies_cutoff)
            if result.purged or result.failed or gone.purged or gone.failed:
                say(
                    f"  {slug}: {result.purged} purged, {gone.purged} working copies dropped, "
                    f"{result.failed + gone.failed} failed"
                )
            purged += result.purged
            purged_bytes += result.purged_bytes
            copies += gone.purged
            copy_bytes += gone.purged_bytes
            failed += result.failed + gone.failed
    finally:
        await close_object_storage()

    ok(f"{purged} file(s) purged, {purged_bytes / (1024 * 1024):.1f} MB reclaimed")
    if copies:
        ok(f"{copies} working cop(ies) dropped, {copy_bytes / (1024 * 1024):.1f} MB reclaimed")
    if failed:
        fail(f"{failed} file(s) could not be deleted from storage; they will be retried")
        return 1
    return 0


async def drive_sync() -> int:
    """Copy every order file not yet in Google Drive, for each organisation
    that has Drive connected. For files a request-time copy missed (Google was
    down) and for orders made before Drive was connected. Safe to repeat."""
    from suliko.domain import drive_mirror
    from suliko.models.order import Order, OrderDocument
    from suliko.models.order_file import OrderFile

    storage = get_object_storage()
    if not storage.configured:
        fail("STORAGE_BACKEND is not set: nothing to copy from")
        return 1
    async with get_sessionmaker()() as db, db.begin():
        with bypass_tenant_scope():
            pending = (
                await db.execute(
                    select(Order.tenant_id, Order.id)
                    .join(OrderDocument, OrderDocument.order_id == Order.id)
                    .join(OrderFile, OrderFile.order_document_id == OrderDocument.id)
                    .where(OrderFile.drive_file_id.is_(None), OrderFile.deleted_at.is_(None))
                    .distinct()
                )
            ).all()
    copied = failed = 0
    for tenant_id, order_id in pending:
        result = await drive_mirror.sync_order(tenant_id, order_id, storage)
        copied += result.copied
        failed += result.failed
    say(f"{copied} file(s) copied to Drive, {failed} failed, {len(pending)} order(s) looked at")
    return 1 if failed else 0


# ── Interactive bootstrap ───────────────────────────────────────────────────


async def bootstrap() -> None:
    heading("1. Database")
    await create_database()

    heading("2. Migrations")
    migrate()

    heading("3. Tenant")
    slug = input("  Tenant slug (e.g. suliko): ").strip() or "suliko"
    name = input(f"  Display name [{slug.title()}]: ").strip() or slug.title()
    await create_tenant(slug, name)

    heading("4. Reference data")
    with_rates = input("  Seed starter language-pair rates? [y/N]: ").strip().lower() == "y"
    await seed_reference(slug, with_rates=with_rates)

    heading("5. Superuser")
    username = input("  Username: ").strip()
    email = input("  Email: ").strip()
    full_name = input("  Full name: ").strip()
    await create_superuser(slug, username, email, full_name)

    heading("Done")
    say("  Start the API with:")
    say("    uvicorn suliko.main:app --host 127.0.0.1 --port 8000 --workers 1")


# ── Entry point ─────────────────────────────────────────────────────────────


async def schema_diff() -> int:
    """Report what the database is missing compared with the models.

    The question this answers is the one that actually gets asked when a
    screen returns 500: *which* table or column is absent. Revision 0001
    builds from ``Base.metadata``, so a database created before a model gained
    a column has that column missing with no migration to add it — and the
    only symptom is a 500 on whichever endpoint touches it.

    Exit code 0 when the database matches, 1 when anything is missing.
    """
    from sqlalchemy import inspect

    from suliko.models import Base

    engine = get_engine()
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []

    try:
        conn_ctx = engine.connect()
    except Exception as exc:
        say(f"Could not reach the database: {type(exc).__name__}")
        say("Check DATABASE_URL in .env and that PostgreSQL is running.")
        return 1

    async with conn_ctx as conn:

        def table_names(sync: Any) -> set[str]:
            return set(inspect(sync).get_table_names())

        tables = await conn.run_sync(table_names)

        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                missing_tables.append(table.name)
                continue

            def columns_of(sync: Any, name: str = table.name) -> set[str]:
                return {c["name"] for c in inspect(sync).get_columns(name)}

            actual = await conn.run_sync(columns_of)
            for column in table.columns:
                if column.name not in actual:
                    missing_columns.append((table.name, column.name))

    if not missing_tables and not missing_columns:
        say(f"Schema matches the models ({len(Base.metadata.tables)} tables).")
        return 0

    if missing_tables:
        say(f"Missing {len(missing_tables)} table(s):")
        for name in missing_tables:
            say(f"  - {name}")

    if missing_columns:
        say(f"Missing {len(missing_columns)} column(s):")
        for table_name, column_name in missing_columns:
            say(f"  - {table_name}.{column_name}")

    say("")
    say("Run `alembic upgrade head`. If that reports it is already at head,")
    say("the database predates these models and needs a new revision:")
    say('    alembic revision --autogenerate -m "catch up to models"')
    return 1


def main() -> None:
    # The ORM events that stamp tenant_id onto new rows live behind this call.
    # main.py installs them in the app factory, but the CLI writes tenant-scoped
    # rows too (seed-reference, create-superuser) and gets its sessions straight
    # from the sessionmaker, so without this every insert arrives with
    # tenant_id NULL and trips the NOT NULL constraint.
    install_tenant_filter()

    parser = argparse.ArgumentParser(prog="suliko", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="interactive first-time setup")
    sub.add_parser("create-database", help="create the database if missing")
    sub.add_parser("migrate", help="alembic upgrade head")
    sub.add_parser("check", help="verify configuration and connectivity")
    sub.add_parser("schema-diff", help="report tables and columns the database is missing")
    sub.add_parser(
        "purge-files", help="delete the bytes of order files removed past the retention period"
    )

    sub.add_parser(
        "drive-sync", help="copy order files not yet in Google Drive, for organisations with Drive"
    )

    p = sub.add_parser("create-tenant", help="register a partner bureau")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--locale", default="ka")

    p = sub.add_parser("create-superuser", help="create a platform superuser")
    p.add_argument("--tenant", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--full-name", required=True)

    p = sub.add_parser("seed-reference", help="seed languages and document types")
    p.add_argument("--tenant", required=True)
    p.add_argument(
        "--with-rates",
        action="store_true",
        help="also seed the starter language-pair rates (verify them afterwards)",
    )

    p = sub.add_parser(
        "import-suliko-users", help="give everyone registered on suliko.ge an account here"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write nothing; list what a real run would do",
    )

    p = sub.add_parser(
        "link-suliko-account",
        help="link an existing account to the suliko.ge person with this sign-in",
    )
    p.add_argument("--email", required=True, help="the existing Office account's email")
    p.add_argument("--suliko-login", required=True, help="their suliko.ge email or phone number")

    p = sub.add_parser(
        "preview-emails",
        help="write every platform email as HTML and text files to look at (sends nothing)",
    )
    p.add_argument("--out", required=True, help="the folder to write them to")

    p = sub.add_parser(
        "import-tarjimnebi",
        help="load a dump of app.tarjimnebi.ge (scripts/tarjimnebi) into an organisation",
    )
    p.add_argument("--tenant", required=True)
    p.add_argument("--dump", required=True, help="the JSON file, or the folder of parts")
    p.add_argument("--commit", action="store_true", help="save; without it nothing is kept")
    p.add_argument(
        "--keep-numbers",
        action="store_true",
        help="keep the old order and client numbers (empty organisations only)",
    )
    p.add_argument(
        "--package-tier",
        type=int,
        help="raise the organisation to this order package (never lowers it); 3 is the biggest",
    )
    p.add_argument(
        "--since",
        help="the dump the earlier import was made from: apply only what is new or changed since",
    )

    sub.add_parser("deletion-requests", help="list the accounts closed by a deletion request")
    sub.add_parser(
        "purge-accounts",
        help="delete the data of approved deletion requests whose waiting time is over",
    )

    p = sub.add_parser(
        "reset-account",
        help="give an account the new-user experience again (deletes nothing)",
    )
    p.add_argument(
        "--login", required=True, help="the email address or phone number it signs in with"
    )

    p = sub.add_parser(
        "cancel-deletion-request",
        help="cancel a deletion request: the account can sign in again, nothing is deleted",
    )
    p.add_argument("--id", type=int, required=True, help="the request's number")

    args = parser.parse_args()

    async def run() -> int:
        try:
            match args.command:
                case "bootstrap":
                    await bootstrap()
                case "create-database":
                    await create_database()
                case "migrate":
                    migrate()
                case "check":
                    return await check()
                case "schema-diff":
                    return await schema_diff()
                case "purge-files":
                    return await purge_files()
                case "drive-sync":
                    return await drive_sync()
                case "create-tenant":
                    await create_tenant(args.slug, args.name, args.locale)
                case "create-superuser":
                    await create_superuser(args.tenant, args.username, args.email, args.full_name)
                case "seed-reference":
                    await seed_reference(args.tenant, with_rates=args.with_rates)
                case "import-suliko-users":
                    await import_suliko_users(args.dry_run)
                case "import-tarjimnebi":
                    return await import_tarjimnebi(
                        args.tenant,
                        args.dump,
                        commit=args.commit,
                        keep_numbers=args.keep_numbers,
                        package_tier=args.package_tier,
                        since=args.since,
                    )
                case "link-suliko-account":
                    await link_suliko_account(args.email, args.suliko_login)
                case "preview-emails":
                    from pathlib import Path

                    from suliko.core.email_samples import write_previews

                    # The subjects are Georgian; a Windows console is not UTF-8 by default.
                    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
                    for line in write_previews(Path(args.out)):
                        say(line)
                    say(f"Open {Path(args.out) / 'index.html'} in a browser.")
                case "deletion-requests":
                    await deletion_requests()
                case "cancel-deletion-request":
                    await cancel_deletion_request(args.id)
                case "reset-account":
                    return await reset_account(args.login)
                case "purge-accounts":
                    return await purge_accounts()
            return 0
        finally:
            await dispose_engine()

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
