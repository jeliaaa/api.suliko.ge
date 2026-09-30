"""A bureau's own additions to its dropdowns.

Each list has built-in values that live in code (translated on the frontend)
and a bureau's extras stored in ``custom_options``. Who may add depends on the
list: anyone who records clients can add an acquisition source from the
client form's "+", while statuses shape every order's workflow and are a
Settings decision. Removing is always a Settings decision.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from suliko.api.deps import CurrentSession, Db
from suliko.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from suliko.domain.statuses import STATUS_DEFINITIONS, normalise
from suliko.models.reference import CustomOption, OptionList
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/options", tags=["options"])

#: (read, add) per list. Delete is always SETTINGS_MANAGE.
LIST_PERMISSIONS: dict[OptionList, tuple[Permission, Permission]] = {
    OptionList.ACQUISITION_SOURCE: (Permission.CLIENTS_READ, Permission.CLIENTS_WRITE),
    OptionList.ORDER_STATUS: (Permission.ORDERS_READ, Permission.SETTINGS_MANAGE),
}

MAX_PER_LIST = 100

ListKey = Annotated[OptionList, Path(alias="list_key")]


class OptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=60)

    @field_validator("value")
    @classmethod
    def _collapse_spaces(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("Enter a value.")
        return collapsed


class OptionOut(BaseModel):
    id: int
    value: str


def _check(session: AuthenticatedSession, permission: Permission) -> None:
    if not session.has(permission):
        raise PermissionDeniedError("You do not have permission to change this list.")


@router.get("/{list_key}", response_model=list[OptionOut])
async def list_options(list_key: ListKey, db: Db, session: CurrentSession) -> list[OptionOut]:
    _check(session, LIST_PERMISSIONS[list_key][0])
    rows = (
        await db.execute(
            select(CustomOption).where(CustomOption.list_key == list_key).order_by(CustomOption.id)
        )
    ).scalars()
    return [OptionOut(id=row.id, value=row.value) for row in rows]


@router.post("/{list_key}", response_model=OptionOut, status_code=http_status.HTTP_201_CREATED)
async def add_option(
    list_key: ListKey, payload: OptionIn, db: Db, session: CurrentSession
) -> OptionOut:
    """Add a value. Adding one that is already there returns it unchanged —
    the "+" beside a dropdown should select it, not report an error."""
    _check(session, LIST_PERMISSIONS[list_key][1])

    if list_key is OptionList.ORDER_STATUS:
        built_in = {normalise(key) for key in STATUS_DEFINITIONS} | {
            normalise(d.label) for d in STATUS_DEFINITIONS.values()
        }
        if normalise(payload.value) in built_in:
            raise ValidationError("That status already exists.")

    existing = (
        await db.execute(
            select(CustomOption).where(
                CustomOption.list_key == list_key,
                func.lower(CustomOption.value) == payload.value.lower(),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return OptionOut(id=existing.id, value=existing.value)

    count = await db.scalar(
        select(func.count()).select_from(CustomOption).where(CustomOption.list_key == list_key)
    )
    if (count or 0) >= MAX_PER_LIST:
        raise ValidationError(f"A list can hold at most {MAX_PER_LIST} values.")

    row = CustomOption(tenant_id=session.tenant_id, list_key=list_key, value=payload.value)
    db.add(row)
    await db.flush()
    return OptionOut(id=row.id, value=row.value)


@router.delete("/{list_key}/{option_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_option(list_key: ListKey, option_id: int, db: Db, session: CurrentSession) -> None:
    """Remove a value from the dropdown. Records already saved with it keep it."""
    _check(session, Permission.SETTINGS_MANAGE)
    row = await db.get(CustomOption, option_id)
    if row is None or row.list_key != list_key:
        raise NotFoundError("Option not found.")
    await db.delete(row)
