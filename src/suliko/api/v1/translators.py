"""Translators.

Follows `clients.py`. The differences that matter:

- Bank details are masked in the list (an IBAN is a payment credential, and
  list responses end up in exports and screenshots).
- `has_portal_account` is derived, not stored — it drives the Active/None badge
  the production screen shows, and asking "is a username set" is clearer than
  making callers infer it.
- The portal password is never accepted or returned here. Setting a
  translator's portal credentials is a separate, audited action.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta, mask_tail
from suliko.core import mail
from suliko.core.errors import ConflictError, NotFoundError
from suliko.domain.portal import (
    account_matches,
    link_account_to_directory_row,
    normalize_email,
    normalize_phone,
    registration_url,
)
from suliko.models.directory import Translator
from suliko.models.portal import InviteKind, InviteStatus, PortalAccountInvite
from suliko.models.tenant import Tenant
from suliko.security.permissions import Permission

router = APIRouter(prefix="/translators", tags=["translators"])


class TranslatorBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    experience_from: date | None = None
    is_active: bool = True
    default_rate: Decimal | None = Field(default=None, ge=0, le=100000)

    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class TranslatorCreate(TranslatorBase):
    pass


class TranslatorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    experience_from: date | None = None
    is_active: bool | None = None
    default_rate: Decimal | None = Field(default=None, ge=0, le=100000)
    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class TranslatorSummary(BaseModel):
    id: int
    name: str
    phone: str | None
    email: str | None
    is_active: bool
    #: Drives the Active / None account badge.
    has_portal_account: bool
    bank_iban_masked: str | None


class TranslatorDetail(TranslatorSummary):
    office_address: str | None
    comment: str | None
    experience_from: date | None
    default_rate: Decimal | None
    bank_inn: str | None
    bank_code: str | None
    portal_username: str | None


class TranslatorPage(BaseModel):
    items: list[TranslatorSummary]
    meta: PageMeta


def _summary(row: Translator) -> TranslatorSummary:
    return TranslatorSummary(
        id=row.id,
        name=row.name,
        phone=row.phone,
        email=row.email,
        is_active=row.is_active,
        has_portal_account=row.has_portal_account,
        bank_iban_masked=mask_tail(row.bank_iban),
    )


def _detail(row: Translator) -> TranslatorDetail:
    return TranslatorDetail(
        **_summary(row).model_dump(),
        office_address=row.office_address,
        comment=row.comment,
        experience_from=row.experience_from,
        default_rate=row.default_rate,
        bank_inn=row.bank_inn,
        bank_code=row.bank_code,
        portal_username=row.portal_username,
    )


@router.get("", response_model=TranslatorPage)
async def list_translators(
    db: Db,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_READ))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    is_active: bool | None = None,
    sort: Literal["name", "-name", "id", "-id"] = "-id",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TranslatorPage:
    stmt = select(Translator)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Translator.name.ilike(pattern),
                Translator.email.ilike(pattern),
                Translator.phone.ilike(pattern),
            )
        )

    if is_active is not None:
        stmt = stmt.where(Translator.is_active == is_active)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = Translator.name if sort.lstrip("-") == "name" else Translator.id
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return TranslatorPage(
        items=[_summary(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


# ── Inviting a translator, and matching them to their suliko.ge account ─────
#
# The literal "/invites" routes below are registered BEFORE "/{translator_id}"
# on purpose: Starlette matches routes by position, and a request for
# "/translators/invites" would otherwise be caught by the int-typed
# "/{translator_id}" route first and fail its own validation instead of
# reaching this one.


class TranslatorInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    #: An existing directory row to attach the invite to. Null creates one —
    #: exactly the choice the suliko.ge admin's own linking screen offers.
    translator_id: int | None = None


class TranslatorInviteOut(BaseModel):
    translator: TranslatorDetail
    #: 'linked' when exactly one suliko.ge account matched immediately;
    #: 'pending' otherwise — no match, or more than one, either way waiting on
    #: `domain.portal.resolve_pending_invites`.
    invite_status: Literal["linked", "pending"]
    #: Set only when linked immediately: which suliko.ge account it matched.
    matched_display_name: str | None
    email_sent: bool


class TranslatorInviteSummary(BaseModel):
    id: int
    full_name: str
    email: str
    phone: str | None
    translator_id: int
    status: InviteStatus
    created_at: datetime


def _translator_invite_email(
    full_name: str, tenant_name: str, email: str, *, linked: bool, matched_display_name: str | None
) -> tuple[str, str]:
    """Subject and plain-text body — the two outcomes read very differently.

    Order matters in the pending body, and is the one the plan settled on:
    which bureau invited them, that they need a suliko.ge account, the EXACT
    address it must use, the registration link, then the phone-number escape
    hatch — so someone who already has an account under a different-looking
    address still finds their way in.
    """
    if linked:
        matched = f" ({matched_display_name})" if matched_display_name else ""
        body = (
            f"Hello {full_name},\n\n"
            f"{tenant_name} has invited you to translate for them on Suliko, and "
            f"it looks like you already have a suliko.ge account under this "
            f"address{matched}.\n\n"
            "Sign in to suliko.ge and open the Orders tab — the bureau is "
            "already waiting for you there.\n"
        )
        return f"{tenant_name} has invited you on Suliko", body

    url = registration_url()
    body = (
        f"Hello {full_name},\n\n"
        f"{tenant_name} has invited you to translate for them on Suliko.\n\n"
        "To accept, you need a suliko.ge account using THIS email address:\n\n"
        f"  {email}\n\n"
        f"If you don't have one yet, register here: {url}\n\n"
        "If you already have a suliko.ge account, make sure it uses this exact "
        "email address — or the matching phone number. The moment it does, "
        f"{tenant_name} will appear in your Orders tab automatically, with "
        "nothing further for you to do.\n"
    )
    return f"{tenant_name} has invited you on Suliko", body


@router.post("/invite", response_model=TranslatorInviteOut, status_code=status.HTTP_201_CREATED)
async def invite_translator(
    payload: TranslatorInvite,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> TranslatorInviteOut:
    """Invite a translator, and try to connect them to their suliko.ge account.

    Attaches to an existing directory row when `translator_id` is given,
    otherwise creates one — see `directory_matches` / `GET /translators` for
    how the bureau finds a row to attach to instead of creating a duplicate.

    Then tries `domain.portal.account_matches` against the invited email and
    phone. Exactly one match links immediately, through the same
    `link_account_to_directory_row` the suliko.ge admin's manual link uses.
    Zero or several matches leave the invite `pending`: it is not an error —
    the invitee is emailed the suliko.ge registration link and told exactly
    which address to use, and `resolve_pending_invites` finishes the job the
    moment a matching account exists (see that function's docstring for the
    two moments that happens).

    `db` doubles as the platform session here: `portal_translators` and
    `portal_account_invites` carry no row-level security and are not
    `TenantScoped`, so the tenant-scoped ORM filter and the before-flush guard
    both leave them alone — see `db/tenancy.py` and the exemption in
    `tests/test_tenant_isolation.py`.
    """
    email = str(payload.email).strip().lower()
    phone = (payload.phone or "").strip() or None

    if payload.translator_id is not None:
        directory_row = await db.get(Translator, payload.translator_id)
        if directory_row is None:
            raise NotFoundError("Translator not found.")
    else:
        directory_row = Translator(
            name=payload.full_name.strip(), email=email, phone=phone, is_active=True
        )
        db.add(directory_row)
        await db.flush()

    tenant = await db.get(Tenant, session.tenant_id)
    if tenant is None:  # pragma: no cover - a live session always has one
        raise NotFoundError("Organisation not found.")

    @asynccontextmanager
    async def _same_tenant(_tenant_id: int) -> AsyncIterator[AsyncSession]:
        # The invite is always for THIS bureau's own directory row, so there
        # is no other tenant scope to enter — `link_account_to_directory_row`
        # is written for the suliko.ge admin, who has none bound yet.
        yield db

    matches = await account_matches(db, phone=phone, email=email)

    existing = (
        await db.execute(
            select(PortalAccountInvite).where(
                PortalAccountInvite.tenant_id == session.tenant_id,
                PortalAccountInvite.kind == InviteKind.TRANSLATOR,
                PortalAccountInvite.translator_id == directory_row.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        clash = (
            await db.execute(
                select(PortalAccountInvite).where(
                    PortalAccountInvite.tenant_id == session.tenant_id,
                    PortalAccountInvite.kind == InviteKind.TRANSLATOR,
                    PortalAccountInvite.email == email,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ConflictError(
                "That email address is already the invite for a different translator record."
            )
        existing = PortalAccountInvite(
            tenant_id=session.tenant_id,
            kind=InviteKind.TRANSLATOR,
            translator_id=directory_row.id,
            email=email,
            invited_by_user_id=session.user_id,
        )
        db.add(existing)

    existing.full_name = payload.full_name.strip()
    existing.email = email
    existing.phone = phone
    existing.normalized_email = normalize_email(email)
    existing.normalized_phone = normalize_phone(phone)

    matched_display_name: str | None = None
    if len(matches) == 1:
        account, _reason = matches[0]
        # `translator_id` is passed explicitly, so this can only attach the
        # SAME row or raise — never create or reassign one.
        await link_account_to_directory_row(
            db, _same_tenant, account=account, tenant=tenant, translator_id=directory_row.id
        )
        matched_display_name = account.display_name
        existing.portal_translator_id = account.id
        existing.status = InviteStatus.LINKED
        existing.resolved_at = datetime.now(UTC)
    else:
        existing.portal_translator_id = None
        existing.status = InviteStatus.PENDING
        existing.resolved_at = None

    await db.flush()
    await db.refresh(directory_row)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.invited",
        entity_type="translator",
        entity_id=directory_row.id,
        after={
            "email": email,
            "invite_status": existing.status.value,
            "candidate_count": len(matches),
        },
    )

    subject, body = _translator_invite_email(
        directory_row.name,
        session.tenant_name,
        email,
        linked=existing.status is InviteStatus.LINKED,
        matched_display_name=matched_display_name,
    )
    mail_result = await mail.send(email, subject, body)

    return TranslatorInviteOut(
        translator=_detail(directory_row),
        invite_status=existing.status.value,  # type: ignore[arg-type]
        matched_display_name=matched_display_name,
        email_sent=mail_result.delivered,
    )


@router.get("/invites", response_model=list[TranslatorInviteSummary])
async def list_translator_invites(
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_READ))],
) -> list[TranslatorInviteSummary]:
    """Every translator invite this bureau has sent, newest first.

    `tenant_id` is filtered explicitly — `PortalAccountInvite` is a platform
    table with no RLS and no `TenantScoped` mixin, so nothing does this
    automatically. See the isolation test in `tests/test_portal_api.py`.
    """
    rows = (
        (
            await db.execute(
                select(PortalAccountInvite)
                .where(
                    PortalAccountInvite.tenant_id == session.tenant_id,
                    PortalAccountInvite.kind == InviteKind.TRANSLATOR,
                )
                .order_by(PortalAccountInvite.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        TranslatorInviteSummary(
            id=row.id,
            full_name=row.full_name,
            email=row.email,
            phone=row.phone,
            translator_id=row.translator_id,  # type: ignore[arg-type]
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_translator_invite(
    invite_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> None:
    """Stop waiting for a match. The directory row and any existing link are
    untouched — this only cancels the invite record."""
    row = await db.get(PortalAccountInvite, invite_id)
    if row is None or row.tenant_id != session.tenant_id or row.kind is not InviteKind.TRANSLATOR:
        # Another bureau's invite is indistinguishable from a missing one.
        raise NotFoundError("Invite not found.")

    row.status = InviteStatus.CANCELLED
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.invite_cancelled",
        entity_type="translator",
        entity_id=row.translator_id,
        before={"email": row.email},
    )


@router.get("/{translator_id}", response_model=TranslatorDetail)
async def get_translator(
    translator_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_READ))],
) -> TranslatorDetail:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")
    return _detail(row)


@router.post("", response_model=TranslatorDetail, status_code=status.HTTP_201_CREATED)
async def create_translator(
    payload: TranslatorCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> TranslatorDetail:
    row = Translator(**payload.model_dump())
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.created",
        entity_type="translator",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return _detail(row)


@router.patch("/{translator_id}", response_model=TranslatorDetail)
async def update_translator(
    translator_id: int,
    payload: TranslatorUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> TranslatorDetail:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes}

    for field, value in changes.items():
        setattr(row, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.updated",
        entity_type="translator",
        entity_id=row.id,
        before=before,
        after=changes,
    )
    return _detail(row)


@router.delete("/{translator_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_translator(
    translator_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> None:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.deleted",
        entity_type="translator",
        entity_id=row.id,
        before={"name": row.name},
    )
    # order_documents.translator_id is ON DELETE SET NULL, so past work is
    # kept and simply becomes unassigned. Deactivating (is_active=false) is
    # still the better move for someone who has history.
    await db.delete(row)
