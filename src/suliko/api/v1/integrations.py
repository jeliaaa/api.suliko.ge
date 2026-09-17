"""Integration credentials — the Settings → Integrations tab.

Ports the PHP app's integration forms (`settings.php`, `manage_*_credentials`),
with one rule the PHP did not have: **a stored secret is never returned**. Not
in full, not masked, not the last four characters. The screen shows whether a
secret is set and when it was last changed; to change it you type a new one.

That rule is why this router looks asymmetric — rich on the way in, sparse on
the way out. It is deliberate. A masked secret is still a secret with its
entropy reduced, and an endpoint that returns one turns a read-only session
hijack into a credential theft.

## Secret vs config

Each provider declares which of its fields are secret. Secrets are encrypted
per tenant (`core.crypto`) into one blob; config is plain JSONB so it can be
displayed, diffed in the audit log, and searched. A reCAPTCHA *site* key is
public by design and lives in config; the *secret* key does not.

## Why `PUT`, and why blank means keep

Saving sends the whole provider, because the form is the whole provider. A
blank secret field means "leave the stored one alone", never "erase it" — the
form cannot prefill what it is not allowed to read, so blank is its resting
state and must be the safe one. Clearing a secret is `DELETE`, which is
explicit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Drive
from suliko.core.crypto import DecryptionError, decrypt_for_tenant, encrypt_for_tenant
from suliko.core.errors import (
    NotFoundError,
    PermissionDeniedError,
    UpstreamUnavailableError,
    ValidationError,
)
from suliko.domain.order_files import (
    DriveLinkError,
    drive_verification_name,
    get_drive_settings,
    resolve_shared_drive,
    save_drive_link,
    verify_drive_ownership,
)
from suliko.domain.plans import allows_provider, providers_for_plan
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── The provider registry ───────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    """One field on a provider's form."""

    key: str
    label: str
    #: Secret fields are write-only and encrypted. Never returned.
    secret: bool = False
    required: bool = True
    #: Rendered as a textarea rather than an input — service-account JSON.
    multiline: bool = False
    help: str | None = None
    placeholder: str | None = None


@dataclass(frozen=True)
class ProviderSpec:
    provider: IntegrationProvider
    name: str
    summary: str
    #: What stops working when this is off, in one line.
    enables: str
    fields: tuple[FieldSpec, ...]

    @property
    def config_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if not f.secret)

    @property
    def secret_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.secret)


PROVIDERS: dict[IntegrationProvider, ProviderSpec] = {
    IntegrationProvider.GOOGLE_DRIVE: ProviderSpec(
        provider=IntegrationProvider.GOOGLE_DRIVE,
        name="Google Drive",
        summary="Per-order folders and document storage.",
        enables="Attaching documents to orders and archiving generated invoices.",
        fields=(
            FieldSpec(
                key="root_folder_id",
                label="Root folder ID",
                required=False,
                help="The Drive folder new order folders are created inside. "
                "Leave blank to use the service account's own My Drive.",
            ),
            FieldSpec(
                key="impersonate_subject",
                label="Impersonate user",
                required=False,
                help="A Workspace address the service account acts as, when "
                "domain-wide delegation is configured.",
                placeholder="office@example.ge",
            ),
            FieldSpec(
                key="service_account_json",
                label="Service account JSON",
                secret=True,
                multiline=True,
                help="The whole key file. It contains a private key — it is "
                "encrypted on save and never shown again.",
            ),
        ),
    ),
    IntegrationProvider.BOG_ECOMMERCE: ProviderSpec(
        provider=IntegrationProvider.BOG_ECOMMERCE,
        name="Bank of Georgia — e-commerce",
        summary="Client payment links, status polling and refunds.",
        enables="Generating a payment link on an order, and recording the result.",
        fields=(
            FieldSpec(key="client_id", label="Client ID"),
            FieldSpec(key="client_secret", label="Client secret", secret=True),
            FieldSpec(
                key="callback_url",
                label="Callback URL",
                required=False,
                help="Where the bank posts the payment result. Must be public "
                "and is signature-verified.",
                placeholder="https://api.suliko.ge/api/v1/payments/bog/callback",
            ),
        ),
    ),
    IntegrationProvider.BOG_BUSINESS: ProviderSpec(
        provider=IntegrationProvider.BOG_BUSINESS,
        name="Bank of Georgia — Business Online",
        summary="Outbound transfers to translators and notaries.",
        enables="Paying a translator or notary directly from a payout.",
        fields=(
            FieldSpec(key="client_id", label="Client ID"),
            FieldSpec(key="client_secret", label="Client secret", secret=True),
            FieldSpec(key="source_iban", label="Source IBAN", help="The account paid from."),
            FieldSpec(key="payer_inn", label="Payer tax ID"),
            FieldSpec(key="payer_name", label="Payer name"),
        ),
    ),
    IntegrationProvider.SMS_OFFICE: ProviderSpec(
        provider=IntegrationProvider.SMS_OFFICE,
        name="SmsOffice",
        summary="SMS to clients on confirmation and pickup.",
        enables="Confirmation and ready-for-pickup texts. Georgian numbers only.",
        fields=(
            FieldSpec(key="sender", label="Sender name", help="The registered sender ID."),
            FieldSpec(key="api_key", label="API key", secret=True),
        ),
    ),
    IntegrationProvider.SMTP: ProviderSpec(
        provider=IntegrationProvider.SMTP,
        name="Email (SMTP)",
        summary="Outbound email: confirmations, documents, invoices.",
        enables="Every email the CRM sends. Without it, order confirmations go nowhere.",
        fields=(
            FieldSpec(key="host", label="Server", placeholder="smtp.example.ge"),
            FieldSpec(key="port", label="Port", placeholder="587"),
            FieldSpec(key="username", label="Username"),
            FieldSpec(key="password", label="Password", secret=True),
            FieldSpec(
                key="from_email",
                label="From address",
                help="Must be an address this server is allowed to send as.",
            ),
            FieldSpec(key="from_name", label="From name", required=False),
            FieldSpec(
                key="use_tls",
                label="Use STARTTLS",
                required=False,
                help="Leave on unless the server only accepts implicit TLS on 465.",
            ),
        ),
    ),
    IntegrationProvider.ELEVENLABS: ProviderSpec(
        provider=IntegrationProvider.ELEVENLABS,
        name="ElevenLabs",
        summary="Speech-to-text transcription.",
        enables="The Speech to Text screen.",
        fields=(FieldSpec(key="api_key", label="API key", secret=True),),
    ),
    IntegrationProvider.RECAPTCHA: ProviderSpec(
        provider=IntegrationProvider.RECAPTCHA,
        name="reCAPTCHA",
        summary="Bot protection on public forms.",
        enables="The public order form and the client portal sign-in.",
        fields=(
            FieldSpec(
                key="site_key",
                label="Site key",
                help="Public by design — it is rendered into the page.",
            ),
            FieldSpec(key="secret_key", label="Secret key", secret=True),
        ),
    ),
    IntegrationProvider.API24: ProviderSpec(
        provider=IntegrationProvider.API24,
        name="API24",
        summary="AI document translation (content.api24.ge).",
        enables="Machine translation of an uploaded document.",
        fields=(
            FieldSpec(key="phone", label="Phone", help="The API24 account's login."),
            FieldSpec(
                key="password",
                label="Password",
                secret=True,
                help="Stored encrypted. The PHP app kept this in plain text.",
            ),
        ),
    ),
}


# ── Schemas ─────────────────────────────────────────────────────────────────


class FieldOut(BaseModel):
    key: str
    label: str
    secret: bool
    required: bool
    multiline: bool
    help: str | None
    placeholder: str | None


class IntegrationOut(BaseModel):
    provider: IntegrationProvider
    name: str
    summary: str
    enables: str
    fields: list[FieldOut]
    is_enabled: bool
    #: Non-secret values only.
    config: dict[str, Any]
    #: Which secret fields have a stored value. Never the values themselves.
    secrets_set: list[str]
    secrets_updated_at: datetime | None
    last_check_at: datetime | None
    last_check_ok: bool | None
    last_check_detail: str | None
    #: True when every required field has a value — what the list badge reads.
    is_complete: bool


class IntegrationSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    #: Only the secrets being CHANGED. Omitted or blank keeps the stored one.
    secrets: dict[str, str] = Field(default_factory=dict)


class CheckResult(BaseModel):
    ok: bool
    detail: str


# ── Helpers ─────────────────────────────────────────────────────────────────


#: Providers that exist in the registry but are NOT offered on the screen.
#:
#: Google Drive: the generic card asked each bureau for its own
#: service-account key, and nothing ever read it. Drive runs on ONE Suliko
#: service account (`GOOGLE_SERVICE_ACCOUNT_FILE`); what a bureau actually sets
#: is WHICH Shared Drive, and that has its own endpoints below
#: (`/integrations/drive`) with an ownership check the generic form has no
#: place for. A form that saves a private key and then does nothing with it is
#: worse than no form: people fill it in and wonder why their files never
#: appear.
#:
#: Kept in `PROVIDERS` rather than deleted, because the enum value is baked
#: into a CHECK constraint (revision 0003) and existing rows may hold it.
HIDDEN_PROVIDERS: frozenset[IntegrationProvider] = frozenset({IntegrationProvider.GOOGLE_DRIVE})


def _require_allowed(session: AuthenticatedSession, provider: IntegrationProvider) -> None:
    """Refuse a provider the caller's plan does not include.

    The list endpoint hides them, which is UX. This is the enforcement: the
    provider is named in the URL, so hiding it from a list stops nobody from
    typing it.
    """
    if not allows_provider(session.plan, provider):
        raise PermissionDeniedError(
            f"{provider.value} is not included in the {session.plan.value} plan."
        )


def _spec(provider: IntegrationProvider) -> ProviderSpec:
    spec = PROVIDERS.get(provider)
    if spec is None:  # pragma: no cover — the path enum makes this unreachable
        raise NotFoundError("Unknown integration.")
    return spec


def _stored_secrets(row: IntegrationCredential, tenant_id: int) -> dict[str, str]:
    """Decrypt the secret blob. For merging on save and for outbound calls only.

    A blob that will not decrypt means the master key has changed since it was
    written. Treated as "no secrets" rather than raising, so the screen still
    loads and the fix — re-enter them — is the obvious one.
    """
    if row.secrets is None:
        return {}
    try:
        return dict(json.loads(decrypt_for_tenant(tenant_id, row.secrets)))
    except (DecryptionError, ValueError):
        return {}


def _out(row: IntegrationCredential | None, spec: ProviderSpec, tenant_id: int) -> IntegrationOut:
    config = dict(row.config) if row else {}
    secrets = _stored_secrets(row, tenant_id) if row else {}

    present = {f.key for f in spec.config_fields if str(config.get(f.key, "")).strip()}
    present |= {f.key for f in spec.secret_fields if str(secrets.get(f.key, "")).strip()}
    required = {f.key for f in spec.fields if f.required}

    return IntegrationOut(
        provider=spec.provider,
        name=spec.name,
        summary=spec.summary,
        enables=spec.enables,
        fields=[
            FieldOut(
                key=f.key,
                label=f.label,
                secret=f.secret,
                required=f.required,
                multiline=f.multiline,
                help=f.help,
                placeholder=f.placeholder,
            )
            for f in spec.fields
        ],
        is_enabled=row.is_enabled if row else False,
        config=config,
        # Names only. This is the closest the API ever gets to returning them.
        secrets_set=sorted(k for k in secrets if str(secrets[k]).strip()),
        secrets_updated_at=row.secrets_updated_at if row else None,
        last_check_at=row.last_check_at if row else None,
        last_check_ok=row.last_check_ok if row else None,
        last_check_detail=row.last_check_detail if row else None,
        is_complete=required.issubset(present),
    )


async def _row(db: Db, provider: IntegrationProvider) -> IntegrationCredential | None:
    return (
        (
            await db.execute(
                select(IntegrationCredential).where(IntegrationCredential.provider == provider)
            )
        )
        .scalars()
        .first()
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> list[IntegrationOut]:
    """Every provider, configured or not.

    Returns all of them rather than only the saved ones: the screen is a
    catalogue of what the bureau *could* connect, and an empty list would make
    a fresh tenant think the feature was missing.
    """
    saved = (await db.execute(select(IntegrationCredential))).scalars()
    rows = {row.provider: row for row in saved}
    # Filtered by plan. A freelancer connects Google Drive and nothing else,
    # and listing seven services they cannot save is a worse answer than
    # listing the one they can.
    allowed = providers_for_plan(session.plan)
    return [
        _out(rows.get(p), spec, session.tenant_id) for p, spec in PROVIDERS.items() if p in allowed
    ]


# ── Google Drive ────────────────────────────────────────────────────────────
#
# Registered BEFORE the `/{provider}` routes, which would otherwise try to read
# "drive" as a provider name and 422.


class DriveLinkOut(BaseModel):
    shared_drive_id: str
    #: The name Google reported when it was linked.
    drive_name: str | None


class DriveIntegrationOut(BaseModel):
    #: False until GOOGLE_SERVICE_ACCOUNT_FILE is set on the API server.
    #: Nothing below can work until it is true, and no setting here fixes it.
    server_configured: bool
    #: What the bureau adds to its Shared Drive as a Content manager.
    service_account_email: str | None
    #: The folder the bureau creates at the top of its drive to prove it is
    #: theirs. Fixed per organisation.
    verification_folder: str
    linked: DriveLinkOut | None


class DriveConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A Shared Drive id, or a link pasted from the browser's address bar.
    shared_drive: str = Field(min_length=1, max_length=500)


async def _drive_state(db: Db, drive: Drive, tenant_id: int) -> DriveIntegrationOut:
    settings = await get_drive_settings(db)
    email = drive.service_account_email
    return DriveIntegrationOut(
        server_configured=email is not None,
        service_account_email=email,
        verification_folder=drive_verification_name(tenant_id),
        linked=(
            DriveLinkOut(shared_drive_id=settings.shared_drive_id, drive_name=settings.drive_name)
            if settings
            else None
        ),
    )


@router.get("/drive", response_model=DriveIntegrationOut)
async def get_drive_integration(
    db: Db,
    drive: Drive,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> DriveIntegrationOut:
    """Where this organisation's files go, and what it takes to connect."""
    _require_allowed(session, IntegrationProvider.GOOGLE_DRIVE)
    return await _drive_state(db, drive, session.tenant_id)


@router.put("/drive", response_model=DriveIntegrationOut)
async def connect_drive(
    payload: DriveConnectIn,
    db: Db,
    drive: Drive,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> DriveIntegrationOut:
    """Connect this organisation to a Shared Drive it can prove it owns.

    Three things are checked before anything is saved: that the drive opens
    at all (so Suliko's service account is a member), that no other
    organisation already has it, and that it contains this organisation's
    verification folder. The last is what makes this safe to offer to every
    bureau rather than only to an operator — see `domain/order_files.py`.

    The same flow for everyone, superusers included: a superuser connects the
    drive of the organisation they are signed in to, like anyone else.
    """
    _require_allowed(session, IntegrationProvider.GOOGLE_DRIVE)

    try:
        drive_id, drive_name = await resolve_shared_drive(drive, payload.shared_drive)
        if drive_id is None:  # pragma: no cover — min_length=1 rules out blank
            raise DriveLinkError("Paste the Shared Drive link or id.")
        await verify_drive_ownership(db, drive, drive_id=drive_id, tenant_id=session.tenant_id)
    except DriveLinkError as exc:
        if exc.upstream:
            raise UpstreamUnavailableError(exc.message) from exc
        raise ValidationError(exc.message) from exc

    previous = await save_drive_link(db, drive_id, drive_name)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="integration.drive_connected",
        entity_type="tenant",
        entity_id=session.tenant_id,
        before={"shared_drive_id": previous},
        after={"shared_drive_id": drive_id, "drive_name": drive_name},
    )
    return await _drive_state(db, drive, session.tenant_id)


@router.delete("/drive", response_model=DriveIntegrationOut)
async def disconnect_drive(
    db: Db,
    drive: Drive,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> DriveIntegrationOut:
    """Stop using the connected drive.

    Nothing is deleted in Google Drive. What goes is Suliko's record of where
    each document's folders were, because those ids point into this drive and
    mean nothing in the next one.
    """
    _require_allowed(session, IntegrationProvider.GOOGLE_DRIVE)

    previous = await save_drive_link(db, None, None)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="integration.drive_disconnected",
        entity_type="tenant",
        entity_id=session.tenant_id,
        before={"shared_drive_id": previous},
        after={"shared_drive_id": None},
    )
    return await _drive_state(db, drive, session.tenant_id)


@router.get("/{provider}", response_model=IntegrationOut)
async def get_integration(
    provider: IntegrationProvider,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> IntegrationOut:
    _require_allowed(session, provider)
    return _out(await _row(db, provider), _spec(provider), session.tenant_id)


@router.put("/{provider}", response_model=IntegrationOut)
async def save_integration(
    provider: IntegrationProvider,
    payload: IntegrationSave,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> IntegrationOut:
    _require_allowed(session, provider)
    spec = _spec(provider)

    known_config = {f.key for f in spec.config_fields}
    known_secrets = {f.key for f in spec.secret_fields}

    # Reject unknown keys rather than storing them: config is JSONB, so a typo
    # would be accepted silently and the field would simply never take effect.
    if unknown := set(payload.config) - known_config:
        raise ValidationError(f"Unknown config field(s): {', '.join(sorted(unknown))}")
    if unknown := set(payload.secrets) - known_secrets:
        raise ValidationError(f"Unknown secret field(s): {', '.join(sorted(unknown))}")

    row = await _row(db, provider)
    if row is None:
        row = IntegrationCredential(provider=provider, config={}, is_enabled=False)
        db.add(row)
        await db.flush()

    stored = _stored_secrets(row, session.tenant_id)

    # A blank incoming secret means "keep what is stored". The form cannot
    # prefill a value it is not allowed to read, so blank is its resting state
    # and must not be destructive.
    changed_secrets = {k: v for k, v in payload.secrets.items() if v.strip()}
    merged = {**stored, **changed_secrets}

    row.config = {k: v for k, v in payload.config.items() if k in known_config}
    row.is_enabled = payload.is_enabled

    if changed_secrets:
        row.secrets = encrypt_for_tenant(session.tenant_id, json.dumps(merged))
        row.secrets_updated_at = datetime.now(UTC)
        row.secrets_updated_by_user_id = session.user_id
        # Any previous check result describes credentials that no longer exist.
        row.last_check_at = None
        row.last_check_ok = None
        row.last_check_detail = None

    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="integration.updated",
        entity_type="integration_credential",
        entity_id=row.id,
        # Config values are safe; secrets are recorded as names only, so the
        # audit trail says WHICH credential changed and never what it became.
        after={
            "provider": provider.value,
            "is_enabled": payload.is_enabled,
            "config": row.config,
            "secrets_changed": sorted(changed_secrets),
        },
    )

    return _out(row, spec, session.tenant_id)


@router.post("/{provider}/check", response_model=CheckResult)
async def check_integration(
    provider: IntegrationProvider,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> CheckResult:
    """Verify the credentials are COMPLETE. It does not call the provider yet.

    Named `check` rather than `test connection` on purpose. A button labelled
    "Test connection" that does not connect is worse than no button: it
    reports success on credentials that have never worked, and the first real
    failure then happens in front of a client.

    What it does today is catch the common half of the problem — a required
    field left blank, a provider enabled before it was filled in. Each live
    check lands with the flow that needs it: SMTP with the confirmation email,
    Drive with document upload, BOG with the payment link.
    """
    _require_allowed(session, provider)
    spec = _spec(provider)
    row = await _row(db, provider)

    if row is None:
        return CheckResult(ok=False, detail="Not configured yet.")

    secrets = _stored_secrets(row, session.tenant_id)
    missing = [
        f.label
        for f in spec.fields
        if f.required and not str((secrets if f.secret else row.config).get(f.key, "")).strip()
    ]

    if missing:
        result = CheckResult(ok=False, detail=f"Missing required field(s): {', '.join(missing)}.")
    elif row.secrets is not None and not secrets:
        # The blob exists but would not decrypt — the master key changed.
        result = CheckResult(
            ok=False,
            detail="The stored secrets cannot be decrypted with the current "
            "ENCRYPTION_MASTER_KEY. Re-enter them.",
        )
    else:
        result = CheckResult(
            ok=True,
            detail="All required fields are set. A live connection test lands "
            "with the feature that uses this provider.",
        )

    row.last_check_at = datetime.now(UTC)
    row.last_check_ok = result.ok
    row.last_check_detail = result.detail[:500]
    await db.flush()

    return result


@router.delete("/{provider}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_integration(
    provider: IntegrationProvider,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> None:
    """Remove a provider's credentials entirely.

    The explicit way to clear a secret, since a blank field on save means
    "keep". Deleting the row rather than nulling the blob so nothing is left
    behind that a later bug could decrypt.
    """
    row = await _row(db, provider)
    if row is None:
        raise NotFoundError("Not configured.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="integration.deleted",
        entity_type="integration_credential",
        entity_id=row.id,
        before={"provider": provider.value},
    )
    await db.delete(row)
