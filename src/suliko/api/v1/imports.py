"""Importing a bureau's own spreadsheet of clients or translators.

Three steps, and the file travels with each, so the server keeps nothing between
them:

1. ``analyse``: read the file, return its columns and a guess at which is which.
2. ``preview``: with the chosen mapping, say what each row would do. Nothing is
   saved.
3. ``commit``: do it. Only the rows that are fine are saved, in one transaction,
   and the import is recorded so the last one can be undone.

Undo deletes what the last import created, and only while none of it is in use:
a client with an order, or a translator on a document, stays and the undo is
refused with a sentence that says so.

For owners and admins (``settings.manage``): an import can add thousands of
records at once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._files import read_upload
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.cities import pack
from suliko.domain.imports import (
    FIELDS,
    KINDS,
    MAX_BYTES,
    MAX_ROWS,
    ExistingPerson,
    Judged,
    RowResult,
    Sheet,
    cell_text,
    check_mapping,
    judge_sheet,
    read_sheet,
    suggest_mapping,
)
from suliko.domain.order_files import safe_file_name
from suliko.models.directory import Client, ClientType, Translator, TranslatorLanguagePair
from suliko.models.finance import ClientPayment, TranslatorPayment
from suliko.models.imports import ImportBatch, ImportBatchItem
from suliko.models.order import Order, OrderDocument
from suliko.models.reference import Language
from suliko.security.permissions import Permission

router = APIRouter(prefix="/imports", tags=["imports"])

Importer = Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))]

#: Rows listed in full in a preview; the rest are counted. Problems are listed
#: in full up to a larger cap, since they are what the person has to act on.
SAMPLE_ROWS = 25
PROBLEM_ROWS = 300


# ── Schemas ─────────────────────────────────────────────────────────────────


class FieldOut(BaseModel):
    key: str
    required: bool


class ColumnOut(BaseModel):
    index: int
    header: str
    #: The first few values under it, so the person can tell which column is which.
    sample: list[str]


class AnalysisOut(BaseModel):
    file_name: str
    total_rows: int
    max_rows: int
    columns: list[ColumnOut]
    fields: list[FieldOut]
    #: field key -> column index, as guessed from the headers.
    suggested: dict[str, int]


class RowOut(BaseModel):
    row: int
    status: str
    name: str
    messages: list[str]
    warnings: list[str]
    matched: str | None
    #: What would be saved, as short text for the preview table.
    summary: list[str]


class PreviewOut(BaseModel):
    total: int
    will_create: int
    duplicates: int
    errors: int
    with_warnings: int
    sample: list[RowOut]
    problems: list[RowOut]
    #: More problem rows than are listed.
    problems_truncated: bool


class BatchOut(BaseModel):
    id: int
    kind: str
    file_name: str
    rows_total: int
    created_count: int
    duplicate_count: int
    error_count: int
    created_at: datetime
    undone_at: datetime | None
    #: Only the latest import that has not been undone can be.
    can_undo: bool


# ── Helpers ─────────────────────────────────────────────────────────────────


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise NotFoundError("Unknown import. Choose clients or translators.")
    return kind


def _parse_mapping(text: str) -> dict[str, int]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("The column choices could not be read.") from exc
    if not isinstance(raw, dict):
        raise ValidationError("The column choices could not be read.")
    return {str(key): value for key, value in raw.items() if value is not None}


async def _language_lookup(db: Any) -> Any:
    """A function from a code or a name (English or Georgian) to the bureau's code."""
    rows = (await db.execute(select(Language.code, Language.name_en, Language.name_ka))).all()
    table: dict[str, str] = {}
    for code, name_en, name_ka in rows:
        for word in (code, name_en, name_ka):
            if word:
                table.setdefault(word.strip().casefold(), code)

    def lookup(word: str) -> str | None:
        return table.get(word.strip().casefold())

    return lookup


async def _existing(db: Any, kind: str) -> list[ExistingPerson]:
    if kind == "clients":
        rows = (
            await db.execute(
                select(
                    Client.number,
                    Client.name,
                    Client.phone,
                    Client.email,
                    Client.personal_number,
                )
            )
        ).all()
        return [
            ExistingPerson(
                label=f"#{number} {name}", phone=phone, email=email, personal_number=number_id
            )
            for number, name, phone, email, number_id in rows
        ]
    rows = (await db.execute(select(Translator.name, Translator.phone, Translator.email))).all()
    return [
        ExistingPerson(label=name, phone=phone, email=email, name=name)
        for name, phone, email in rows
    ]


def _summary(kind: str, values: dict[str, Any]) -> list[str]:
    if not values:
        return []
    parts: list[str] = []
    if kind == "clients":
        parts.append(values["client_type"])
        for key in ("phone", "email", "personal_number"):
            if values.get(key):
                parts.append(str(values[key]))
        return parts
    for key in ("phone", "email"):
        if values.get(key):
            parts.append(str(values[key]))
    if values.get("language_pairs"):
        parts.append(", ".join(f"{a}-{b}" for a, b in values["language_pairs"]))
    if values.get("cities"):
        parts.append(", ".join(values["cities"]))
    if values.get("can_notarize"):
        parts.append("notary")
    return parts


def _row_out(kind: str, result: RowResult) -> RowOut:
    return RowOut(
        row=result.row,
        status=result.status,
        name=result.name,
        messages=result.messages,
        warnings=result.warnings,
        matched=result.matched,
        summary=_summary(kind, result.values),
    )


async def _read(
    db: Any, kind: str, file: UploadFile, mapping_text: str
) -> tuple[Sheet, dict[str, int], Judged]:
    content = await read_upload(file, MAX_BYTES)
    sheet = read_sheet(content, file.filename or "")
    mapping = check_mapping(kind, _parse_mapping(mapping_text), len(sheet.headers))
    judged = judge_sheet(
        kind,
        sheet,
        mapping,
        await _existing(db, kind),
        await _language_lookup(db) if kind == "translators" else None,
    )
    return sheet, mapping, judged


# ── Step 1: the file ────────────────────────────────────────────────────────


@router.post("/{kind}/analyse", response_model=AnalysisOut)
async def analyse(
    kind: str,
    file: Annotated[UploadFile, File()],
    _: Importer,
) -> AnalysisOut:
    """The file's columns, a few values from each, and a guess at the mapping."""
    kind = _kind(kind)
    content = await read_upload(file, MAX_BYTES)
    sheet = read_sheet(content, file.filename or "")
    columns = [
        ColumnOut(
            index=index,
            header=header or f"({index + 1})",
            sample=[
                text
                for _, cells in sheet.rows[:50]
                if index < len(cells) and (text := cell_text(cells[index]))
            ][:3],
        )
        for index, header in enumerate(sheet.headers)
    ]
    return AnalysisOut(
        file_name=safe_file_name(file.filename),
        total_rows=len(sheet.rows),
        max_rows=MAX_ROWS,
        columns=columns,
        fields=[FieldOut(key=f.key, required=f.required) for f in FIELDS[kind]],
        suggested=suggest_mapping(kind, sheet.headers),
    )


# ── Step 2: what would happen ───────────────────────────────────────────────


@router.post("/{kind}/preview", response_model=PreviewOut)
async def preview(
    kind: str,
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str, Form()],
    db: Db,
    _: Importer,
) -> PreviewOut:
    """Judge every row against the bureau's records. Saves nothing."""
    kind = _kind(kind)
    _, _, judged = await _read(db, kind, file, mapping)
    results = judged.results
    problems = [r for r in results if r.status != "ok" or r.warnings]
    return PreviewOut(
        total=len(results),
        will_create=judged.count("ok"),
        duplicates=judged.count("duplicate"),
        errors=judged.count("error"),
        with_warnings=sum(1 for r in results if r.status == "ok" and r.warnings),
        sample=[_row_out(kind, r) for r in results[:SAMPLE_ROWS]],
        problems=[_row_out(kind, r) for r in problems[:PROBLEM_ROWS]],
        problems_truncated=len(problems) > PROBLEM_ROWS,
    )


# ── Step 3: do it ───────────────────────────────────────────────────────────


@router.post("/{kind}/commit", response_model=BatchOut)
async def commit(
    kind: str,
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str, Form()],
    db: Db,
    session: CurrentSession,
    _: Importer,
) -> BatchOut:
    """Save the rows that are fine, and record the import."""
    kind = _kind(kind)
    _sheet, _mapping, judged = await _read(db, kind, file, mapping)
    good = [r for r in judged.results if r.status == "ok"]
    if not good:
        raise ValidationError("Nothing to import: every row is a duplicate or has an error.")

    batch = ImportBatch(
        kind=kind,
        file_name=safe_file_name(file.filename)[:255],
        rows_total=len(judged.results),
        created_count=len(good),
        duplicate_count=judged.count("duplicate"),
        error_count=judged.count("error"),
        created_by_user_id=session.user_id,
    )
    db.add(batch)
    await db.flush()

    created: list[tuple[str, int]] = []
    if kind == "clients":
        rows = [
            Client(**{**r.values, "client_type": ClientType(r.values["client_type"])}) for r in good
        ]
        db.add_all(rows)
        await db.flush()
        created = [("client", row.id) for row in rows]
    else:
        for r in good:
            values = dict(r.values)
            pairs = values.pop("language_pairs")
            cities = values.pop("cities")
            translator = Translator(**values, cities=pack(cities))
            db.add(translator)
            await db.flush()
            for source, target in pairs:
                db.add(
                    TranslatorLanguagePair(
                        translator_id=translator.id, source_language=source, target_language=target
                    )
                )
            created.append(("translator", translator.id))
        await db.flush()

    db.add_all(
        ImportBatchItem(batch_id=batch.id, entity_type=entity, entity_id=entity_id)
        for entity, entity_id in created
    )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="import.committed",
        entity_type="import",
        entity_id=batch.id,
        after={
            "kind": kind,
            "file": batch.file_name,
            "created": batch.created_count,
            "duplicates": batch.duplicate_count,
            "errors": batch.error_count,
        },
    )
    return _batch_out(batch, can_undo=True)


# ── History, and undo ───────────────────────────────────────────────────────


def _batch_out(row: ImportBatch, *, can_undo: bool) -> BatchOut:
    return BatchOut(
        id=row.id,
        kind=row.kind,
        file_name=row.file_name,
        rows_total=row.rows_total,
        created_count=row.created_count,
        duplicate_count=row.duplicate_count,
        error_count=row.error_count,
        created_at=row.created_at,
        undone_at=row.undone_at,
        can_undo=can_undo and row.undone_at is None,
    )


@router.get("", response_model=list[BatchOut])
async def history(db: Db, _: Importer) -> list[BatchOut]:
    """The latest imports, newest first."""
    rows = (
        (await db.execute(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(20)))
        .scalars()
        .all()
    )
    latest = next((r.id for r in rows if r.undone_at is None), None)
    return [_batch_out(r, can_undo=r.id == latest) for r in rows]


@router.post("/{batch_id}/undo", response_model=BatchOut)
async def undo(batch_id: int, db: Db, session: CurrentSession, _: Importer) -> BatchOut:
    """Delete what the latest import created, unless any of it is already in use."""
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFoundError("Import not found.")
    if batch.undone_at is not None:
        raise ConflictError("That import was already undone.")
    latest = await db.scalar(
        select(func.max(ImportBatch.id)).where(ImportBatch.undone_at.is_(None))
    )
    if latest != batch.id:
        raise ConflictError("Only the latest import can be undone. Undo the newer one first.")

    items = (
        (await db.execute(select(ImportBatchItem).where(ImportBatchItem.batch_id == batch.id)))
        .scalars()
        .all()
    )
    ids = [item.entity_id for item in items]

    if batch.kind == "clients":
        used = await db.scalar(
            select(func.count(func.distinct(Order.client_id))).where(Order.client_id.in_(ids))
        )
        paid = await db.scalar(
            select(func.count(func.distinct(ClientPayment.client_id))).where(
                ClientPayment.client_id.in_(ids)
            )
        )
        in_use = max(used or 0, paid or 0)
        if in_use:
            raise ConflictError(
                f"{in_use} of the imported clients already have orders or payments, so this "
                "import can no longer be undone."
            )
        await db.execute(delete(Client).where(Client.id.in_(ids)))
    else:
        on_documents = await db.scalar(
            select(func.count(func.distinct(OrderDocument.translator_id))).where(
                OrderDocument.translator_id.in_(ids)
            )
        )
        paid_out = await db.scalar(
            select(func.count(func.distinct(TranslatorPayment.translator_id))).where(
                TranslatorPayment.translator_id.in_(ids)
            )
        )
        in_use = max(on_documents or 0, paid_out or 0)
        if in_use:
            raise ConflictError(
                f"{in_use} of the imported translators are already on orders or have payments, "
                "so this import can no longer be undone."
            )
        await db.execute(
            delete(TranslatorLanguagePair).where(TranslatorLanguagePair.translator_id.in_(ids))
        )
        await db.execute(delete(Translator).where(Translator.id.in_(ids)))

    await db.execute(delete(ImportBatchItem).where(ImportBatchItem.batch_id == batch.id))
    batch.undone_at = datetime.now(UTC)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="import.undone",
        entity_type="import",
        entity_id=batch.id,
        after={"kind": batch.kind, "removed": len(ids)},
    )
    return _batch_out(batch, can_undo=False)
