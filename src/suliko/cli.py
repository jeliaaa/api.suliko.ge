"""Operational CLI: database bootstrap, tenants, superuser, reference data.

    python -m suliko.cli bootstrap          # everything, interactively
    python -m suliko.cli create-database
    python -m suliko.cli migrate
    python -m suliko.cli create-tenant --slug acme --name "Acme Translations"
    python -m suliko.cli create-superuser --tenant acme
    python -m suliko.cli seed-reference --tenant acme
    python -m suliko.cli check

The superuser is created here and **never through the HTTP API**. There is no
sign-up path, no "first user becomes admin" rule, and no way to escalate into
the role from inside the application — an account with platform-wide reach must
require filesystem access to the server to create.

See docs/03-SECURITY-AND-TENANCY.md §3.1.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import asyncpg
from sqlalchemy import select, text

from suliko.config import get_settings
from suliko.core.crypto import encrypt_for_tenant
from suliko.db.session import dispose_engine, get_sessionmaker
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.models.directory import Client, ClientType  # noqa: F401 — registry
from suliko.models.reference import DocumentType, Language, LanguagePairPrice, TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import MfaMethod, MfaRecoveryCode, Role, User
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
        raise SystemExit("alembic upgrade failed — see the output above")
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
                locale=locale,
            )
            db.add(tenant)
            await db.flush()

            # One settings row per tenant, carrying the pricing knobs.
            db.add(TenantSettings(tenant_id=tenant.id, default_language=locale))
            await db.commit()

            ok(f"created tenant {slug!r} (id {tenant.id})")
            return int(tenant.id)


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

            if password is None:
                password = _prompt_password()

            user = User(
                username=username,
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role=Role.SUPERUSER,
                is_active=True,
            )
            db.add(user)
            await db.flush()

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

        uri = totp_service.provisioning_uri(secret, f"{username}@{tenant_slug}", "Suliko CRM")

        heading("Superuser created")
        say(f"  username : {username}")
        say(f"  tenant   : {tenant_slug}")
        say("")
        say("  Scan this in your authenticator app, or enter the secret by hand:")
        say(f"    secret : {secret}")
        say(f"    uri    : {uri}")
        say("")
        say("  Recovery codes — each works once. Store them somewhere safe;")
        say("  they are shown now and never again:")
        for code in recovery_codes:
            say(f"    {code}")
        say("")
        warn("This output contains the TOTP secret. Clear your scrollback when done.")


# ── Reference data ──────────────────────────────────────────────────────────

#: Language set the bureau actually works in, taken from the PHP's
#: API24_LANG_IDS map in config.php.
LANGUAGES: tuple[tuple[str, str, str], ...] = (
    ("ka", "Georgian", "ქართული"),
    ("en", "English", "ინგლისური"),
    ("ru", "Russian", "რუსული"),
    ("de", "German", "გერმანული"),
    ("fr", "French", "ფრანგული"),
    ("it", "Italian", "იტალიური"),
    ("es", "Spanish", "ესპანური"),
    ("pt", "Portuguese", "პორტუგალიური"),
    ("tr", "Turkish", "თურქული"),
    ("az", "Azerbaijani", "აზერბაიჯანული"),
    ("hy", "Armenian", "სომხური"),
    ("uk", "Ukrainian", "უკრაინული"),
    ("pl", "Polish", "პოლონური"),
    ("ar", "Arabic", "არაბული"),
    ("he", "Hebrew", "ებრაული"),
    ("zh", "Chinese", "ჩინური"),
    ("ja", "Japanese", "იაპონური"),
    ("el", "Greek", "ბერძნული"),
    ("lv", "Latvian", "ლატვიური"),
    ("sl", "Slovenian", "სლოვენური"),
    ("sk", "Slovak", "სლოვაკური"),
    ("sr", "Serbian", "სერბული"),
    ("ur", "Urdu", "ურდუ"),
    ("fi", "Finnish", "ფინური"),
    ("la", "Latin", "ლათინური"),
)

#: Starter document types. Multipliers all 1.0 — set your own in Settings.
DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("Passport", "პასპორტი"),
    ("ID card", "პირადობის მოწმობა"),
    ("Birth certificate", "დაბადების მოწმობა"),
    ("Marriage certificate", "ქორწინების მოწმობა"),
    ("Diploma", "დიპლომი"),
    ("Transcript", "ნიშნების ფურცელი"),
    ("Certificate", "ცნობა"),
    ("Power of attorney", "მინდობილობა"),
    ("Contract", "ხელშეკრულება"),
    ("Letter", "წერილი"),
    ("Technical specifications", "ტექნიკური მახასიათებლები"),
    ("Medical record", "სამედიცინო ჩანაწერი"),
    ("Bank statement", "საბანკო ამონაწერი"),
    ("Court document", "სასამართლო დოკუმენტი"),
    ("Other", "სხვა"),
)

#: Rates read off the production Calculator screen's "Pricing Reference" panel.
#: The panel was scrolled, so this is the visible subset — NOT a complete rate
#: card. Verify every line against your own before quoting a client.
STARTER_RATES: tuple[tuple[str, str, str], ...] = (
    ("az", "da", "100.00"),
    ("ka", "sv", "60.00"),
    ("sv", "ka", "60.00"),
    ("es", "ka", "50.00"),
    ("pt", "ka", "50.00"),
    ("ka", "uk", "45.00"),
    ("pt", "ru", "45.00"),
    ("ru", "pt", "45.00"),
    ("uk", "ka", "45.00"),
    ("en", "es", "40.00"),
    ("en", "fr", "40.00"),
    ("en", "it", "40.00"),
    ("en", "pt", "40.00"),
    ("es", "en", "40.00"),
    ("es", "ru", "40.00"),
    ("fr", "en", "40.00"),
    ("fr", "ru", "40.00"),
    ("it", "en", "40.00"),
    ("it", "ru", "40.00"),
    ("ka", "es", "40.00"),
    ("ka", "pt", "40.00"),
    ("pt", "en", "40.00"),
)


async def seed_reference(tenant_slug: str, with_rates: bool = False) -> None:
    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
            ).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"No tenant with slug {tenant_slug!r}.")

        with tenant_scope(int(tenant.id)):
            existing_codes = set((await db.execute(select(Language.code))).scalars())
            added = 0
            for code, name_en, name_ka in LANGUAGES:
                if code not in existing_codes:
                    db.add(Language(code=code, name_en=name_en, name_ka=name_ka))
                    added += 1
            ok(f"languages: {added} added, {len(existing_codes)} already present")

            existing_types = set((await db.execute(select(DocumentType.name_en))).scalars())
            added = 0
            for name_en, name_ka in DOCUMENT_TYPES:
                if name_en not in existing_types:
                    db.add(
                        DocumentType(
                            name_en=name_en,
                            name_ka=name_ka,
                            price_multiplier=Decimal("1.0"),
                        )
                    )
                    added += 1
            ok(f"document types: {added} added, {len(existing_types)} already present")

            if with_rates:
                existing_pairs = {
                    (src, tgt)
                    for src, tgt in await db.execute(
                        select(
                            LanguagePairPrice.source_language,
                            LanguagePairPrice.target_language,
                        )
                    )
                }
                added = 0
                for src, tgt, price in STARTER_RATES:
                    if (src, tgt) not in existing_pairs:
                        db.add(
                            LanguagePairPrice(
                                source_language=src,
                                target_language=tgt,
                                price_per_page=Decimal(price),
                                is_active=True,
                            )
                        )
                        added += 1
                ok(f"language pair rates: {added} added")
                warn(
                    "Those rates were read off a screenshot of the production "
                    "Calculator and are an INCOMPLETE subset. Verify every line "
                    "in Settings -> Pricing before quoting a client."
                )

            await db.commit()


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
                    warn("no tenants yet — run create-tenant")

                supers = (
                    (await db.execute(select(User).where(User.role == Role.SUPERUSER)))
                    .scalars()
                    .all()
                )
                if supers:
                    ok(f"{len(supers)} superuser(s)")
                else:
                    warn("no superuser yet — run create-superuser")
    except Exception as exc:  # a CLI should report, not traceback
        fail(f"cannot reach the database: {type(exc).__name__}: {exc}")
        problems += 1

    heading("Result")
    if problems:
        fail(f"{problems} problem(s) found")
    else:
        ok("all checks passed")
    return 1 if problems else 0


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="suliko", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="interactive first-time setup")
    sub.add_parser("create-database", help="create the database if missing")
    sub.add_parser("migrate", help="alembic upgrade head")
    sub.add_parser("check", help="verify configuration and connectivity")

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
                case "create-tenant":
                    await create_tenant(args.slug, args.name, args.locale)
                case "create-superuser":
                    await create_superuser(args.tenant, args.username, args.email, args.full_name)
                case "seed-reference":
                    await seed_reference(args.tenant, with_rates=args.with_rates)
            return 0
        finally:
            await dispose_engine()

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
