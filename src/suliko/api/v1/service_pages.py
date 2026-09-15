"""Service pages and site strings — the marketing-site CMS.

Admin-only (`cms.manage`). Nothing here touches order data; it exists so a
bureau can edit its own public pages without a developer.

## Why `slug` is immutable after creation

A published slug is a URL somebody has linked to. Changing it silently breaks
those links and loses the page's search ranking. To rename one, create the new
slug and redirect from the old — a decision for whoever runs the site, not a
side effect of an edit form.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.models.cms import PageStatus, ServicePage, SiteString
from suliko.security.permissions import Permission

router = APIRouter(prefix="/service-pages", tags=["cms"])
strings_router = APIRouter(prefix="/site-strings", tags=["cms"])

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: The locales the marketing site is published in. Adding one here is the only
#: place it needs to be added.
LOCALES = ("ka", "en")


# ── Service pages ───────────────────────────────────────────────────────────


class PageBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=500)
    body: str = Field(default="", max_length=200_000)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=500)
    status: PageStatus = PageStatus.DRAFT
    sort_order: int = Field(default=0, ge=0, le=9999)


class PageCreate(PageBase):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=2, max_length=5)

    @field_validator("slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        slug = value.strip().lower()
        if not SLUG_PATTERN.match(slug):
            raise ValueError(
                "Slug must be lowercase words separated by single hyphens, e.g. "
                "'notarised-translation'."
            )
        return slug

    @field_validator("locale")
    @classmethod
    def _known_locale(cls, value: str) -> str:
        locale = value.strip().lower()
        if locale not in LOCALES:
            raise ValueError(f"Locale must be one of: {', '.join(LOCALES)}")
        return locale


class PageUpdate(BaseModel):
    """Everything except slug and locale — see the module docstring."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=200_000)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=500)
    status: PageStatus | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class PageSummary(BaseModel):
    id: int
    slug: str
    locale: str
    title: str
    summary: str | None
    status: PageStatus
    sort_order: int
    published_at: datetime | None
    updated_at: datetime


class PageDetail(PageSummary):
    body: str
    meta_title: str | None
    meta_description: str | None
    updated_by_user_id: int | None


class PageList(BaseModel):
    items: list[PageSummary]
    meta: PageMeta
    #: Slugs that exist in one locale but not the other — the gap the editor
    #: needs to see, and the reason this is not just a flat list.
    missing_translations: list[str] = Field(default_factory=list)


@router.get("", response_model=PageList)
async def list_pages(
    db: Db,
    _: Annotated[object, Depends(require(Permission.CMS_MANAGE))],
    locale: Annotated[str | None, Query(max_length=5)] = None,
    status: Annotated[PageStatus | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageList:
    def filtered(stmt: Select[Any]) -> Select[Any]:
        if locale:
            stmt = stmt.where(ServicePage.locale == locale.lower())
        if status is not None:
            stmt = stmt.where(ServicePage.status == status)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(ServicePage.title.ilike(pattern), ServicePage.slug.ilike(pattern))
            )
        return stmt

    total = await db.scalar(filtered(select(func.count()).select_from(ServicePage))) or 0
    rows = (
        (
            await db.execute(
                filtered(select(ServicePage))
                .order_by(ServicePage.sort_order, ServicePage.slug, ServicePage.locale)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # Which slugs are not present in every locale.
    pairs = (await db.execute(select(ServicePage.slug, ServicePage.locale))).all()
    by_slug: dict[str, set[str]] = {}
    for slug, page_locale in pairs:
        by_slug.setdefault(slug, set()).add(page_locale)
    missing = sorted(slug for slug, locales in by_slug.items() if len(locales) < len(LOCALES))

    return PageList(
        items=[PageSummary.model_validate(r, from_attributes=True) for r in rows],
        meta=PageMeta(total=int(total), limit=limit, offset=offset),
        missing_translations=missing,
    )


@router.get("/{page_id}", response_model=PageDetail)
async def get_page(
    page_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.CMS_MANAGE))],
) -> PageDetail:
    row = await db.get(ServicePage, page_id)
    if row is None:
        raise NotFoundError("Page not found.")
    return PageDetail.model_validate(row, from_attributes=True)


@router.post("", response_model=PageDetail, status_code=http_status.HTTP_201_CREATED)
async def create_page(
    payload: PageCreate,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.CMS_MANAGE))],
) -> PageDetail:
    existing = (
        (
            await db.execute(
                select(ServicePage).where(
                    ServicePage.slug == payload.slug,
                    ServicePage.locale == payload.locale,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise ConflictError(f"A {payload.locale} page with slug {payload.slug!r} already exists.")

    row = ServicePage(
        **payload.model_dump(),
        updated_by_user_id=session.user_id,
        published_at=datetime.now(UTC) if payload.status is PageStatus.PUBLISHED else None,
    )
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="service_page.created",
        entity_type="service_page",
        entity_id=row.id,
        # The body is excluded deliberately: it is long, and the audit log is
        # read by more people than the CMS is.
        after=payload.model_dump(mode="json", exclude={"body"}),
    )
    return PageDetail.model_validate(row, from_attributes=True)


@router.patch("/{page_id}", response_model=PageDetail)
async def update_page(
    page_id: int,
    payload: PageUpdate,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.CMS_MANAGE))],
) -> PageDetail:
    row = await db.get(ServicePage, page_id)
    if row is None:
        raise NotFoundError("Page not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes if k != "body"}

    for field, value in changes.items():
        setattr(row, field, value)

    # Stamped on the first publish only. Re-publishing after an edit keeps the
    # original date, because that is the date the page went live.
    if row.status is PageStatus.PUBLISHED and row.published_at is None:
        row.published_at = datetime.now(UTC)

    row.updated_by_user_id = session.user_id
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="service_page.updated",
        entity_type="service_page",
        entity_id=row.id,
        before=before,
        after={
            k: v
            for k, v in payload.model_dump(mode="json", exclude_unset=True).items()
            if k != "body"
        },
    )
    return PageDetail.model_validate(row, from_attributes=True)


@router.delete("/{page_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.CMS_MANAGE))],
) -> None:
    row = await db.get(ServicePage, page_id)
    if row is None:
        raise NotFoundError("Page not found.")

    if row.status is PageStatus.PUBLISHED:
        raise ConflictError(
            "Unpublish the page before deleting it — a live URL should stop serving "
            "content deliberately, not as a side effect."
        )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="service_page.deleted",
        entity_type="service_page",
        entity_id=row.id,
        before={"slug": row.slug, "locale": row.locale, "title": row.title},
    )
    await db.delete(row)


# ── Site strings ────────────────────────────────────────────────────────────


class StringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.-]+$")
    locale: str = Field(min_length=2, max_length=5)
    value: str = Field(max_length=5000)
    context_note: str | None = Field(default=None, max_length=255)

    @field_validator("locale")
    @classmethod
    def _known_locale(cls, value: str) -> str:
        locale = value.strip().lower()
        if locale not in LOCALES:
            raise ValueError(f"Locale must be one of: {', '.join(LOCALES)}")
        return locale


class StringOut(BaseModel):
    id: int
    key: str
    locale: str
    value: str
    group_name: str
    context_note: str | None
    updated_at: datetime


class StringList(BaseModel):
    items: list[StringOut]
    meta: PageMeta
    groups: list[str]


def _group_of(key: str) -> str:
    """First dotted segment, or `general` for a flat key."""
    head, _, rest = key.partition(".")
    return head if rest else "general"


@strings_router.get("", response_model=StringList)
async def list_strings(
    db: Db,
    _: Annotated[object, Depends(require(Permission.CMS_MANAGE))],
    locale: Annotated[str | None, Query(max_length=5)] = None,
    group: Annotated[str | None, Query(max_length=60)] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    untranslated: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StringList:
    def filtered(stmt: Select[Any]) -> Select[Any]:
        if locale:
            stmt = stmt.where(SiteString.locale == locale.lower())
        if group:
            stmt = stmt.where(SiteString.group_name == group)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(or_(SiteString.key.ilike(pattern), SiteString.value.ilike(pattern)))
        if untranslated:
            # An empty value is a key someone added but never filled in — the
            # thing this screen exists to find.
            stmt = stmt.where(func.trim(SiteString.value) == "")
        return stmt

    total = await db.scalar(filtered(select(func.count()).select_from(SiteString))) or 0
    rows = (
        (
            await db.execute(
                filtered(select(SiteString))
                .order_by(SiteString.group_name, SiteString.key, SiteString.locale)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    groups = (
        (await db.execute(select(SiteString.group_name).distinct().order_by(SiteString.group_name)))
        .scalars()
        .all()
    )

    return StringList(
        items=[StringOut.model_validate(r, from_attributes=True) for r in rows],
        meta=PageMeta(total=int(total), limit=limit, offset=offset),
        groups=list(groups),
    )


@strings_router.put("", response_model=list[StringOut])
async def upsert_strings(
    payload: Annotated[list[StringIn], Field(max_length=500)],
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.CMS_MANAGE))],
) -> list[StringOut]:
    """Create or overwrite strings in bulk.

    A PUT over a list, not per-key PATCHes: the editor is a grid, and someone
    fixing twenty labels should produce one request and one audit entry rather
    than twenty of each.
    """
    if not payload:
        raise ValidationError("Nothing to save.")

    seen: set[tuple[str, str]] = set()
    for item in payload:
        pair = (item.key, item.locale)
        if pair in seen:
            raise ValidationError(f"{item.key!r} ({item.locale}) appears twice in one request.")
        seen.add(pair)

    saved: list[SiteString] = []
    for item in payload:
        row = (
            (
                await db.execute(
                    select(SiteString).where(
                        SiteString.key == item.key,
                        SiteString.locale == item.locale,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = SiteString(
                key=item.key,
                locale=item.locale,
                value=item.value,
                group_name=_group_of(item.key),
                context_note=item.context_note,
                updated_by_user_id=session.user_id,
            )
            db.add(row)
        else:
            row.value = item.value
            if item.context_note is not None:
                row.context_note = item.context_note
            row.updated_by_user_id = session.user_id
        saved.append(row)

    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="site_strings.updated",
        entity_type="site_string",
        # Keys only. The values are the content itself and would bloat every
        # entry for no investigative value.
        after={"keys": [f"{item.key}:{item.locale}" for item in payload]},
    )
    return [StringOut.model_validate(r, from_attributes=True) for r in saved]


@strings_router.delete("/{string_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_string(
    string_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.CMS_MANAGE))],
) -> None:
    row = await db.get(SiteString, string_id)
    if row is None:
        raise NotFoundError("String not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="site_string.deleted",
        entity_type="site_string",
        entity_id=row.id,
        before={"key": row.key, "locale": row.locale},
    )
    await db.delete(row)


# ── Public read surface ─────────────────────────────────────────────────────


@router.get("/public/{locale}", response_model=list[PageSummary])
async def published_pages(
    locale: Literal["ka", "en"],
    db: Db,
    _: Annotated[object, Depends(require(Permission.CMS_MANAGE))],
) -> list[PageSummary]:
    """Published pages in one locale, in display order.

    Still behind `cms.manage`: this is the preview the editor uses. The
    genuinely public feed belongs on a separate unauthenticated surface with
    its own caching, which is not built yet — putting it on this router would
    mean an anonymous endpoint inside the authenticated API.
    """
    rows = (
        (
            await db.execute(
                select(ServicePage)
                .where(
                    ServicePage.locale == locale,
                    ServicePage.status == PageStatus.PUBLISHED,
                )
                .order_by(ServicePage.sort_order, ServicePage.title)
            )
        )
        .scalars()
        .all()
    )
    return [PageSummary.model_validate(r, from_attributes=True) for r in rows]
