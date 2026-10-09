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
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta, mask_tail
from suliko.core import mail
from suliko.core.errors import ConflictError, NotFoundError, RateLimitedError
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.domain.portal import (
    account_matches,
    checked_login,
    find_portal_translator,
    link_account_to_directory_row,
    normalize_email,
    normalize_phone,
    registration_url,
)
from suliko.integrations.suliko_backend import SulikoBackend, SulikoUser, get_suliko_backend
from suliko.models.directory import Translator
from suliko.models.portal import (
    InviteKind,
    InviteStatus,
    PortalAccountInvite,
    PortalTranslator,
    PortalTranslatorLink,
)
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
    #: Per-page rate; the order builder pre-fills translator cost from it.
    default_rate: Decimal | None


class TranslatorDetail(TranslatorSummary):
    office_address: str | None
    comment: str | None
    experience_from: date | None
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
        default_rate=row.default_rate,
    )


def _detail(row: Translator) -> TranslatorDetail:
    return TranslatorDetail(
        **_summary(row).model_dump(),
        office_address=row.office_address,
        comment=row.comment,
        experience_from=row.experience_from,
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
    #: Who to invite, by address. May be left out when `suliko_user_id` names
    #: the account: someone who signs in to suliko.ge with a phone number has
    #: no address.
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    #: An existing directory row to attach the invite to. Null creates one —
    #: exactly the choice the suliko.ge admin's own linking screen offers.
    translator_id: int | None = None
    #: The suliko.ge account the bureau picked from `GET /invite/candidates`.
    #: The server looks the person up itself: nothing about them is taken from
    #: the request but this id.
    suliko_user_id: str | None = Field(default=None, min_length=1, max_length=450)

    @model_validator(mode="after")
    def _someone_to_invite(self) -> TranslatorInvite:
        if self.email is None and self.suliko_user_id is None:
            raise ValueError("an email address or a chosen suliko.ge account is required")
        return self


class TranslatorInviteOut(BaseModel):
    translator: TranslatorDetail
    #: 'linked' when exactly one suliko.ge account matched immediately;
    #: 'pending' otherwise — no match, or more than one, either way waiting on
    #: `domain.portal.resolve_pending_invites`.
    invite_status: Literal["linked", "pending"]
    #: Set only when linked immediately: which suliko.ge account it matched.
    matched_display_name: str | None
    email_sent: bool
    #: There was no address to write to (a phone-only account), so nothing was
    #: emailed and nothing needs retrying: a linked translator just sees the
    #: bureau in their Orders tab on suliko.ge.
    no_email: bool = False


class TranslatorCandidate(BaseModel):
    """One suliko.ge account that matches what the bureau searched for."""

    suliko_user_id: str
    full_name: str
    #: Profile contacts, shown to tell people apart. Unverified: a person can
    #: edit them freely, so they help a human recognise someone and prove
    #: nothing.
    email: str | None
    phone: str | None
    #: Already linked to a record in THIS bureau's directory — which one, so
    #: the form can say why they cannot be picked again.
    already_linked: bool = False
    linked_translator_name: str | None = None


class TranslatorInviteSummary(BaseModel):
    id: int
    full_name: str
    email: str | None
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


async def _portal_translator_for(db: AsyncSession, person: SulikoUser) -> PortalTranslator:
    """The portal account of a suliko.ge person, made on first pick.

    Normally a suliko.ge admin marks someone a translator before any bureau can
    link them. Picking an account from the search is the bureau doing that for
    its own purposes, so the row is made here, from what suliko.ge itself says
    about them: the sign-in name (an address or a number, both proven at
    registration), never the editable profile contacts.
    """
    row = await find_portal_translator(db, person.id)
    if row is not None:
        # Whatever the admin recorded stays as it is, including a deactivation.
        return row
    row = PortalTranslator(
        external_user_id=person.id,
        display_name=(person.full_name or person.user_name)[:255],
        email=person.email,
        phone=person.phone,
        is_active=True,
    )
    db.add(row)
    await db.flush()
    return row


async def _refuse_if_linked_elsewhere(
    db: AsyncSession, account: PortalTranslator, tenant_id: int, translator_id: int
) -> None:
    """Picking someone who is already one of this bureau's translators must not
    quietly move them to a different record (`link_account_to_directory_row`
    would re-point the link). The search shows who they are linked to."""
    link = (
        await db.execute(
            select(PortalTranslatorLink).where(
                PortalTranslatorLink.portal_translator_id == account.id,
                PortalTranslatorLink.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if link is not None and link.translator_id != translator_id:
        raise ConflictError(
            "This person is already linked to another translator record in your directory."
        )


@router.post("/invite", response_model=TranslatorInviteOut, status_code=status.HTTP_201_CREATED)
async def invite_translator(
    payload: TranslatorInvite,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    backend: Annotated[SulikoBackend, Depends(get_suliko_backend)],
) -> TranslatorInviteOut:
    """Invite a translator, and try to connect them to their suliko.ge account.

    Attaches to an existing directory row when `translator_id` is given,
    otherwise creates one — see `directory_matches` / `GET /translators` for
    how the bureau finds a row to attach to instead of creating a duplicate.

    Two ways to say who the translator is:

    - **Pick an account** (`suliko_user_id`, from `GET /invite/candidates`).
      The person is linked at once. suliko.ge is asked about them here, so the
      request carries nothing but the id; they need not have been marked a
      translator by an admin first (`_portal_translator_for`). With no address
      to write to, nothing is emailed: they see the bureau in their Orders tab
      the next time they open suliko.ge.
    - **An address** (`email`). `domain.portal.account_matches` looks for
      suliko.ge accounts an admin has marked as translators. Exactly one match
      links immediately, through the same `link_account_to_directory_row` the
      suliko.ge admin's manual link uses. Zero or several leave the invite
      `pending`: it is not an error — the invitee is emailed the suliko.ge
      registration link and told exactly which address to use, and
      `resolve_pending_invites` finishes the job the moment a matching account
      exists (see that function's docstring for the two moments that happens).

    `db` doubles as the platform session here: `portal_translators` and
    `portal_account_invites` carry no row-level security and are not
    `TenantScoped`, so the tenant-scoped ORM filter and the before-flush guard
    both leave them alone — see `db/tenancy.py` and the exemption in
    `tests/test_tenant_isolation.py`.
    """
    typed_email = str(payload.email).strip().lower() if payload.email is not None else None
    typed_phone = (payload.phone or "").strip() or None

    # Asked first, before anything is written: a suliko.ge that cannot be asked,
    # or a person who is gone, must not leave a half-made invite behind.
    person: SulikoUser | None = None
    if payload.suliko_user_id is not None:
        person = await backend.get_user(payload.suliko_user_id)
        if person is None:
            raise NotFoundError("That suliko.ge account no longer exists.")

    # What the invite records and where mail goes: what the bureau typed, else
    # the sign-in name suliko.ge proved. Null only for a phone-only account.
    email = typed_email or (person.email if person else None)
    phone = typed_phone or (person.phone if person else None)

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

    chosen: PortalTranslator | None = None
    matches: list[tuple[PortalTranslator, str]] = []
    if person is not None:
        chosen = await _portal_translator_for(db, person)
        await _refuse_if_linked_elsewhere(db, chosen, session.tenant_id, directory_row.id)
    elif email is not None:
        matches = list(await account_matches(db, phone=phone, email=email))

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
        if email is not None:
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

    # Whoever is to be linked right now: the picked account, or the one
    # account an address matched. Anything else waits.
    to_link = chosen if chosen is not None else (matches[0][0] if len(matches) == 1 else None)
    matched_display_name: str | None = None
    if to_link is not None:
        # `translator_id` is passed explicitly, so this can only attach the
        # SAME row or raise — never create or reassign one.
        await link_account_to_directory_row(
            db, _same_tenant, account=to_link, tenant=tenant, translator_id=directory_row.id
        )
        matched_display_name = to_link.display_name
        existing.portal_translator_id = to_link.id
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
            "picked_account": person is not None,
        },
    )

    if email is None:
        # A phone-only account has no address. Nothing is sent and the quota is
        # not spent: there is nothing to retry either.
        return TranslatorInviteOut(
            translator=_detail(directory_row),
            invite_status=existing.status.value,  # type: ignore[arg-type]
            matched_display_name=matched_display_name,
            email_sent=False,
            no_email=True,
        )

    subject, body = _translator_invite_email(
        directory_row.name,
        session.tenant_name,
        email,
        linked=existing.status is InviteStatus.LINKED,
        matched_display_name=matched_display_name,
    )
    # Same per-tenant quota as staff invites: both send mail from the
    # platform's address with text this bureau wrote.
    tenant_key = f"invite:tenant:{session.tenant_id}"
    if retry := await limiter.check_invite(tenant_key):
        raise RateLimitedError(
            "This organisation has sent too many invitations today. Try again tomorrow.",
            retry_after=retry,
        )
    await limiter.record_invite(tenant_key)
    mail_result = await mail.send(email, subject, body)

    return TranslatorInviteOut(
        translator=_detail(directory_row),
        invite_status=existing.status.value,  # type: ignore[arg-type]
        matched_display_name=matched_display_name,
        email_sent=mail_result.delivered,
    )


def _mask_email(email: str | None) -> str | None:
    """`gela.b@suliko.ge` -> `g***@suliko.ge`: enough to tell two people apart."""
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else f"{email[:1]}***"


def _mask_phone(phone: str | None) -> str | None:
    """`599123456` -> `599***456`: the ends, never the whole number."""
    if not phone:
        return None
    stripped = phone.strip()
    if len(stripped) < 7:
        return f"{stripped[:1]}***"
    return f"{stripped[:3]}***{stripped[-3:]}"


def _local_number(raw: str | None) -> str | None:
    """A phone number without the country code or the trunk zero, as suliko.ge's
    own search compares them: "+995 599 12 34 56" and "0599123456" are one number."""
    number = normalize_phone(raw)
    if number is not None and len(number) == 10 and number.startswith("05"):
        number = number[1:]
    return number


def _candidate_contacts(
    contact_email: str | None, contact_phone: str | None, searched: str
) -> tuple[str | None, str | None]:
    """What a search result may show of someone's contacts.

    The contact that was searched for is shown in full: the bureau typed it, so
    nothing is revealed. The OTHER one is partly hidden, otherwise anyone who
    knows an address could look up its owner's phone number, or the reverse.
    The name and the hidden forms still tell two people apart.
    """
    if "@" in searched:
        same_email = normalize_email(contact_email) == normalize_email(searched)
        shown_email = contact_email if same_email else _mask_email(contact_email)
        return shown_email, _mask_phone(contact_phone)
    wanted = _local_number(searched)
    same_phone = wanted is not None and _local_number(contact_phone) == wanted
    shown_phone = contact_phone if same_phone else _mask_phone(contact_phone)
    return _mask_email(contact_email), shown_phone


@router.get("/invite/candidates", response_model=list[TranslatorCandidate])
async def translator_invite_candidates(
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
    backend: Annotated[SulikoBackend, Depends(get_suliko_backend)],
    q: Annotated[str, Query(min_length=1, max_length=255)],
) -> list[TranslatorCandidate]:
    """Every suliko.ge account whose email or phone number equals `q`.

    For the invite form: the bureau types an address or a number and sees all
    the matches, not just one, so it can tell two people apart by name and
    contacts and pick the right one. Equality, not "contains": the caller must
    already know the address or number, so this cannot be used to browse the
    people on suliko.ge.

    The contacts shown are the profile ones, which a person can edit without
    verification: they help a human recognise someone, and nothing is linked
    until a human picks. The one that was searched for is shown in full and the
    other is partly hidden (`_candidate_contacts`), so this is no way to look
    up someone's phone number from their address, or the reverse. Empty when
    suliko.ge is not connected — the form then falls back to inviting by address.

    Registered before `/{translator_id}`, like the other literal routes here.
    """
    login = checked_login(q)
    if not backend.enabled:
        return []
    contacts = await backend.search_users(login)
    if not contacts:
        return []

    portal_rows = (
        (
            await db.execute(
                select(PortalTranslator).where(
                    PortalTranslator.external_user_id.in_([c.id for c in contacts])
                )
            )
        )
        .scalars()
        .all()
    )
    portal_by_external = {row.external_user_id: row for row in portal_rows}
    links = (
        (
            await db.execute(
                select(PortalTranslatorLink).where(
                    PortalTranslatorLink.tenant_id == session.tenant_id,
                    PortalTranslatorLink.portal_translator_id.in_([r.id for r in portal_rows]),
                )
            )
        )
        .scalars()
        .all()
        if portal_rows
        else []
    )
    record_by_portal = {link.portal_translator_id: link.translator_id for link in links}
    names = (
        {
            row.id: row.name
            for row in (
                await db.execute(
                    select(Translator).where(Translator.id.in_(set(record_by_portal.values())))
                )
            )
            .scalars()
            .all()
        }
        if record_by_portal
        else {}
    )

    candidates = []
    for contact in contacts:
        portal = portal_by_external.get(contact.id)
        record_id = record_by_portal.get(portal.id) if portal else None
        shown_email, shown_phone = _candidate_contacts(contact.email, contact.phone, login)
        candidates.append(
            TranslatorCandidate(
                suliko_user_id=contact.id,
                full_name=contact.full_name or contact.user_name,
                email=shown_email,
                phone=shown_phone,
                already_linked=record_id is not None,
                linked_translator_name=names.get(record_id) if record_id is not None else None,
            )
        )
    return candidates


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
