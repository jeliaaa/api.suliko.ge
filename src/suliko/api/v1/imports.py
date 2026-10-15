"""Importing a bureau's own spreadsheet of clients, translators, notaries or orders.

Three steps, and the file travels with each, so the server keeps nothing between
them:

1. ``analyse``: read the file, return its columns and a guess at which is which.
2. ``preview``: with the chosen mapping, say what each row would do. Nothing is
   saved.
3. ``commit``: do it. Only the rows that are fine are saved, in one transaction,
   and the import is recorded so the last one can be undone.

``/imports/combined/...`` does the same for a whole workbook, or a flat sheet that
carries everything: it works out what each sheet is, and saves notaries,
translators, clients and then orders, in that order, so an order can point at the
people created just before it.

Undo deletes what the last import created, and only while none of it is in use:
a client with an order, or a translator on a document, stays and the undo is
refused with a sentence that says so.

For owners and admins (``settings.manage``): an import can add thousands of
records at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
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
    read_workbook,
    suggest_mapping,
)
from suliko.domain.imports_combined import (
    Choice,
    Directory,
    SheetPlan,
    WorkbookJudged,
    judge_workbook,
    plan_sheets,
)
from suliko.domain.imports_orders import ExistingOrder, OrdersContext, remember_person
from suliko.domain.order_files import safe_file_name
from suliko.domain.statuses import INITIAL_STATUS, STATUS_DEFINITIONS
from suliko.domain.translator_pairs import Pair, both_ways
from suliko.models.collaboration import OrderComment
from suliko.models.directory import Client, ClientType, Notary, Translator, TranslatorLanguagePair
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    NotaryPaymentAllocation,
    PaymentMethod,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.imports import ImportBatch, ImportBatchItem
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    ServiceType,
    Urgency,
)
from suliko.models.order_file import OrderFile
from suliko.models.reference import CustomOption, DocumentType, Language, OptionList
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


class SkippedOut(BaseModel):
    row: int
    #: "heading" (a title, a month banner, a footnote) or "totals".
    reason: str


class SheetOut(BaseModel):
    """One sheet of the file: what it looks like it is, and its columns."""

    index: int
    name: str
    #: One of the kinds, or None when the sheet is left out.
    kind: str | None
    #: "high", "low" or "none".
    confidence: str
    #: "columns", "name", "unsure", "nothing" or "chosen".
    why: str
    header_row: int | None
    #: Rows under the header that hold something.
    total_rows: int
    #: Rows among them that are not records.
    skipped_count: int
    skipped: list[SkippedOut]
    columns: list[ColumnOut]
    #: The columns as guessed for the kind above.
    mapping: dict[str, int]
    #: The guess for each kind, so the person can change the kind without asking again.
    suggested: dict[str, dict[str, int]]


class CombinedAnalysisOut(BaseModel):
    file_name: str
    max_rows: int
    sheets: list[SheetOut]
    #: The fields of each kind.
    fields: dict[str, list[FieldOut]]


class SheetPreviewOut(BaseModel):
    index: int
    name: str
    kind: str
    total: int
    will_create: int
    duplicates: int
    errors: int
    with_warnings: int
    skipped_count: int
    sample: list[RowOut]
    problems: list[RowOut]
    problems_truncated: bool


class CreatesOut(BaseModel):
    """What the import would add, counting what the orders bring with them."""

    clients: int
    translators: int
    notaries: int
    orders: int


class CombinedPreviewOut(BaseModel):
    sheets: list[SheetPreviewOut]
    total: int
    will_create: int
    duplicates: int
    errors: int
    with_warnings: int
    creates: CreatesOut


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
        raise NotFoundError("Unknown import. Choose clients, translators, notaries or orders.")
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


async def _orders_context(db: Any) -> OrdersContext:
    """What a row of an orders sheet is matched against: the bureau's own lists."""
    lookup = await _language_lookup(db)

    types = (
        await db.execute(
            select(DocumentType.id, DocumentType.name_en, DocumentType.name_ka)
            .where(DocumentType.is_active)
            .order_by(DocumentType.name_en)
        )
    ).all()
    document_types: dict[str, tuple[int, str]] = {}
    for type_id, name_en, name_ka in types:
        for word in (name_en, name_ka):
            if word:
                document_types.setdefault(word.strip().casefold(), (type_id, name_en or name_ka))
    default = (types[0][0], types[0][1] or types[0][2]) if types else None

    context = OrdersContext(language_lookup=lookup)
    for translator_id, name, phone in (
        await db.execute(
            select(Translator.id, Translator.name, Translator.phone).where(Translator.is_active)
        )
    ).all():
        remember_person(
            context.translators, context.translators_by_phone, translator_id, name, phone
        )
    for notary_id, name, phone in (
        await db.execute(select(Notary.id, Notary.name, Notary.phone))
    ).all():
        remember_person(context.notaries, context.notaries_by_phone, notary_id, name, phone)

    statuses: dict[str, str] = {}
    for key, definition in STATUS_DEFINITIONS.items():
        statuses[key.casefold()] = key
        statuses.setdefault(definition.label.casefold(), key)
    for value in (
        await db.execute(
            select(CustomOption.value).where(CustomOption.list_key == OptionList.ORDER_STATUS)
        )
    ).scalars():
        statuses[value.strip().casefold()] = value

    existing = [
        ExistingOrder(
            label=f"order #{number}",
            client_id=client_id,
            order_date=order_date,
            source_language=source,
            target_language=target,
            page_count=pages,
            price=Decimal(str(price)),
            identifier=identifier,
        )
        for number, client_id, order_date, identifier, source, target, pages, price in (
            await db.execute(
                select(
                    Order.number,
                    Order.client_id,
                    Order.order_date,
                    Order.identifier,
                    OrderDocument.source_language,
                    OrderDocument.target_language,
                    OrderDocument.page_count,
                    OrderDocument.price,
                ).join(OrderDocument, OrderDocument.order_id == Order.id)
            )
        ).all()
    ]
    context.document_types = document_types
    context.default_document_type = default
    context.status_lookup = lambda text: statuses.get(text.strip().casefold())
    context.existing_orders = existing
    return context


async def _existing(db: Any, kind: str) -> list[ExistingPerson]:
    if kind == "orders":
        # The bureau's clients, so a row can name one: by phone, email, ID, then name.
        rows = (
            await db.execute(
                select(
                    Client.id,
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
                label=f"#{number} {name}",
                phone=phone,
                email=email,
                personal_number=national_id,
                name=name,
                ref=client_id,
            )
            for client_id, number, name, phone, email, national_id in rows
        ]
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
    if kind == "notaries":
        notaries = (
            await db.execute(select(Notary.id, Notary.name, Notary.phone, Notary.email))
        ).all()
        return [
            ExistingPerson(label=name, phone=phone, email=email, name=name, ref=notary_id)
            for notary_id, name, phone, email in notaries
        ]
    rows = (
        await db.execute(select(Translator.id, Translator.name, Translator.phone, Translator.email))
    ).all()
    return [
        ExistingPerson(label=name, phone=phone, email=email, name=name, ref=translator_id)
        for translator_id, name, phone, email in rows
    ]


def _summary(kind: str, values: dict[str, Any]) -> list[str]:
    if not values:
        return []
    parts: list[str] = []
    if kind == "orders":
        parts.append(values["order_date"].isoformat())
        parts.append(f"{values['source_language']}-{values['target_language']}")
        parts.append(f"{values['document_type_label']} x{values['page_count']}")
        parts.append(f"{values['price']} GEL")
        if values["paid"]:
            parts.append(f"paid {values['paid']}")
        if values["client"]["new"] is not None:
            parts.append("new client")
        if isinstance(values["translator_id"], str):
            parts.append("new translator")
        if isinstance(values["notary_id"], str):
            parts.append("new notary")
        return parts
    if kind == "clients":
        parts.append(values["client_type"])
        for key in ("phone", "email", "personal_number"):
            if values.get(key):
                parts.append(str(values[key]))
        return parts
    for key in ("phone", "email"):
        if values.get(key):
            parts.append(str(values[key]))
    if kind == "notaries":
        for key in ("registration_number", "bank_iban"):
            if values.get(key):
                parts.append(str(values[key]))
        if not values.get("bank_iban"):
            parts.append("no IBAN")
        return parts
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
        orders=await _orders_context(db) if kind == "orders" else None,
    )
    return sheet, mapping, judged


# ── One file, several sheets ────────────────────────────────────────────────
#
# Declared before the ``/{kind}/...`` routes: "combined" would match ``{kind}``.


def _parse_choices(text: str) -> dict[int, Choice]:
    """What the person changed about each sheet: its kind (or that it is left out), columns."""
    unreadable = ValidationError("The choices for the sheets could not be read.")
    try:
        raw = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise unreadable from exc
    if not isinstance(raw, list):
        raise unreadable
    choices: dict[int, Choice] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            raise unreadable
        kind = item.get("kind")
        if kind is not None and kind not in KINDS:
            raise ValidationError(f"Unknown kind: {kind}.")
        mapping = item.get("mapping")
        if mapping is not None:
            if not isinstance(mapping, dict):
                raise unreadable
            mapping = {str(key): value for key, value in mapping.items() if value is not None}
        header_row = item.get("header_row")
        if header_row is not None and (
            not isinstance(header_row, int) or isinstance(header_row, bool)
        ):
            raise unreadable
        choices[item["index"]] = Choice(
            skip=bool(item.get("skip")), kind=kind, header_row=header_row, mapping=mapping
        )
    return choices


async def _directory(db: Any) -> Directory:
    return Directory(
        clients=await _existing(db, "orders"),
        translators=await _existing(db, "translators"),
        notaries=await _existing(db, "notaries"),
    )


async def _judge_file(db: Any, file: UploadFile, choices_text: str) -> WorkbookJudged:
    content = await read_upload(file, MAX_BYTES)
    sheets = read_workbook(content, file.filename or "")
    plans = plan_sheets(sheets, _parse_choices(choices_text))
    context = await _orders_context(db)
    return judge_workbook(plans, await _directory(db), context, context.language_lookup)


def _sheet_out(plan: SheetPlan) -> SheetOut:
    columns = [
        ColumnOut(
            index=index,
            header=header or f"({index + 1})",
            sample=[
                text
                for _, cells in plan.rows[:50]
                if index < len(cells) and (text := cell_text(cells[index]))
            ][:3],
        )
        for index, header in enumerate(plan.headers)
    ]
    return SheetOut(
        index=plan.index,
        name=plan.name,
        kind=plan.kind,
        confidence=plan.confidence,
        why=plan.why,
        header_row=plan.header_row,
        total_rows=len(plan.rows) + len(plan.skipped),
        skipped_count=len(plan.skipped),
        skipped=[SkippedOut(row=row, reason=reason) for row, reason in plan.skipped[:10]],
        columns=columns,
        mapping=plan.mapping,
        suggested={kind: suggest_mapping(kind, plan.headers) for kind in KINDS},
    )


def _creates(judged: WorkbookJudged) -> CreatesOut:
    orders = judged.ok("orders")
    return CreatesOut(
        clients=len(judged.ok("clients"))
        + sum(1 for r in orders if r.values["client"]["new"] is not None),
        translators=len(judged.ok("translators")) + len(judged.ctx.new_translators),
        notaries=len(judged.ok("notaries")) + len(judged.ctx.new_notaries),
        orders=len(orders),
    )


def _ref(result: RowResult) -> str:
    if result.ref is None:
        raise ValueError("A row of a combined import has no ref.")
    return result.ref


@router.post("/combined/analyse", response_model=CombinedAnalysisOut)
async def analyse_combined(
    file: Annotated[UploadFile, File()],
    _: Importer,
) -> CombinedAnalysisOut:
    """Every sheet of the file: what it looks like it holds, and its columns."""
    content = await read_upload(file, MAX_BYTES)
    plans = plan_sheets(read_workbook(content, file.filename or ""))
    return CombinedAnalysisOut(
        file_name=safe_file_name(file.filename),
        max_rows=MAX_ROWS,
        sheets=[_sheet_out(plan) for plan in plans],
        fields={
            kind: [FieldOut(key=f.key, required=f.required) for f in FIELDS[kind]] for kind in KINDS
        },
    )


@router.post("/combined/preview", response_model=CombinedPreviewOut)
async def preview_combined(
    file: Annotated[UploadFile, File()],
    db: Db,
    _: Importer,
    choices: Annotated[str, Form()] = "[]",
) -> CombinedPreviewOut:
    """Judge every sheet that is a kind, against the bureau's records and each other."""
    judged = await _judge_file(db, file, choices)
    sheets = []
    for sheet in judged.sheets:
        kind = sheet.plan.kind or ""
        results = sheet.results
        problems = [r for r in results if r.status != "ok" or r.warnings]
        sheets.append(
            SheetPreviewOut(
                index=sheet.plan.index,
                name=sheet.plan.name,
                kind=kind,
                total=len(results),
                will_create=sheet.count("ok"),
                duplicates=sheet.count("duplicate"),
                errors=sheet.count("error"),
                with_warnings=sum(1 for r in results if r.status == "ok" and r.warnings),
                skipped_count=len(sheet.plan.skipped),
                sample=[_row_out(kind, r) for r in results[:SAMPLE_ROWS]],
                problems=[_row_out(kind, r) for r in problems[:PROBLEM_ROWS]],
                problems_truncated=len(problems) > PROBLEM_ROWS,
            )
        )
    return CombinedPreviewOut(
        sheets=sheets,
        total=judged.total,
        will_create=judged.count("ok"),
        duplicates=judged.count("duplicate"),
        errors=judged.count("error"),
        with_warnings=sum(s.with_warnings for s in sheets),
        creates=_creates(judged),
    )


@router.post("/combined/commit", response_model=BatchOut)
async def commit_combined(
    file: Annotated[UploadFile, File()],
    db: Db,
    session: CurrentSession,
    _: Importer,
    choices: Annotated[str, Form()] = "[]",
) -> BatchOut:
    """Save what is fine, notaries first and orders last, as one import."""
    judged = await _judge_file(db, file, choices)
    if not judged.count("ok"):
        raise ValidationError("Nothing to import: every row is a duplicate or has an error.")

    batch = ImportBatch(
        kind="combined",
        file_name=safe_file_name(file.filename)[:255],
        rows_total=judged.total,
        created_count=judged.count("ok"),
        duplicate_count=judged.count("duplicate"),
        error_count=judged.count("error"),
        created_by_user_id=session.user_id,
    )
    db.add(batch)
    await db.flush()

    # The people the orders introduce are saved with the ones the sheets list.
    context, refs = judged.ctx, Refs()
    created = await _add_notaries(
        db,
        refs,
        [(_ref(r), r.values) for r in judged.ok("notaries")] + list(context.new_notaries.items()),
    )
    created += await _add_translators(
        db,
        refs,
        [(_ref(r), r.values) for r in judged.ok("translators")]
        + list(context.new_translators.items()),
    )
    created += await _add_clients(db, refs, [(_ref(r), r.values) for r in judged.ok("clients")])
    created += await _create_orders(db, session, judged.ok("orders"), refs)
    return await _finish(db, session, batch, created)


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

    refs = Refs()
    created: list[tuple[str, int]] = []
    if kind == "orders":
        created = await _create_orders(db, session, good, refs)
    elif kind == "clients":
        created = await _add_clients(db, refs, [(None, r.values) for r in good])
    elif kind == "notaries":
        created = await _add_notaries(db, refs, [(None, r.values) for r in good])
    else:
        created = await _add_translators(db, refs, [(None, r.values) for r in good])

    return await _finish(db, session, batch, created)


async def _finish(
    db: Any, session: Any, batch: ImportBatch, created: list[tuple[str, int]]
) -> BatchOut:
    """List what the import made, so undo can find it, and log it."""
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
            "kind": batch.kind,
            "file": batch.file_name,
            "created": batch.created_count,
            "duplicates": batch.duplicate_count,
            "errors": batch.error_count,
        },
    )
    return _batch_out(batch, can_undo=True)


@dataclass
class Refs:
    """The ids of what an import has created so far, by the ref later rows name them with."""

    clients: dict[str, int] = field(default_factory=dict)
    translators: dict[str, int] = field(default_factory=dict)
    notaries: dict[str, int] = field(default_factory=dict)


#: What to save for a record, and the ref the rest of the import knows it by (if any).
Spec = tuple[str | None, dict[str, Any]]


async def _add_clients(db: Any, refs: Refs, specs: list[Spec]) -> list[tuple[str, int]]:
    clients = [
        (ref, Client(**{**values, "client_type": ClientType(values["client_type"])}))
        for ref, values in specs
    ]
    db.add_all(client for _, client in clients)
    await db.flush()
    for ref, client in clients:
        if ref is not None:
            refs.clients[ref] = client.id
    return [("client", client.id) for _, client in clients]


async def _add_notaries(db: Any, refs: Refs, specs: list[Spec]) -> list[tuple[str, int]]:
    notaries = []
    for ref, values in specs:
        # What the orders know of a notary they introduce is a name and a phone.
        notaries.append((ref, Notary(**{k: v for k, v in values.items() if k != "pairs"})))
    db.add_all(notary for _, notary in notaries)
    await db.flush()
    for ref, notary in notaries:
        if ref is not None:
            refs.notaries[ref] = notary.id
    return [("notary", notary.id) for _, notary in notaries]


async def _add_translators(db: Any, refs: Refs, specs: list[Spec]) -> list[tuple[str, int]]:
    """Translators and their pairs. A sheet row says ``language_pairs``, an order ``pairs``."""
    made: list[tuple[str | None, Translator, list[tuple[str, str]]]] = []
    for ref, values in specs:
        data = dict(values)
        pairs = data.pop("language_pairs", None) or []
        pairs = data.pop("pairs", None) or pairs
        cities = data.pop("cities", None) or []
        made.append((ref, Translator(**data, cities=pack(cities)), pairs))
    db.add_all(translator for _, translator, _pairs in made)
    await db.flush()
    for ref, translator, pairs in made:
        if ref is not None:
            refs.translators[ref] = translator.id
        # A pair works both ways, as when it is typed into the form.
        for pair in both_ways(Pair(source, target) for source, target in pairs):
            db.add(
                TranslatorLanguagePair(
                    translator_id=translator.id,
                    source_language=pair.source,
                    target_language=pair.target,
                )
            )
    await db.flush()
    return [("translator", translator.id) for _, translator, _pairs in made]


async def _create_orders(
    db: Any, session: Any, good: list[RowResult], refs: Refs
) -> list[tuple[str, int]]:
    """Clients first, then each order with its document, status history and payment.

    Few flushes: all the clients, all the orders, then everything that hangs
    from an order, then the allocations that need the payments' ids. A row names
    its client, translator and notary by id, or by a ref that ``refs`` resolves
    (the ones the rest of the import created, and the clients made here).
    """
    created = await _add_clients(
        db,
        refs,
        [
            (r.values["client"]["ref"], r.values["client"]["new"])
            for r in good
            if r.values["client"]["new"] is not None
        ],
    )

    def resolved(ref: int | str | None, known: dict[str, int]) -> int | None:
        return known[ref] if isinstance(ref, str) else ref

    orders = [
        Order(
            client_id=resolved(r.values["client"]["ref"], refs.clients),
            order_date=r.values["order_date"],
            due_date=r.values["due_date"],
            identifier=r.values["identifier"],
            urgency=Urgency.STANDARD,
            handover_method=HandoverMethod.SCAN,
            delivery_cost=Decimal("0"),
            source="import",
            notes=r.values["notes"],
            created_by_user_id=session.user_id,
        )
        for r in good
    ]
    db.add_all(orders)
    await db.flush()
    created += [("order", order.id) for order in orders]

    now = datetime.now(UTC)
    payments: list[tuple[ClientPayment, Order, Decimal]] = []
    for result, order in zip(good, orders, strict=True):
        v = result.values
        notary_id = resolved(v["notary_id"], refs.notaries)
        db.add(
            OrderDocument(
                order_id=order.id,
                document_type_id=v["document_type_id"],
                service_type=ServiceType.WRITTEN,
                source_language=v["source_language"],
                target_language=v["target_language"],
                page_count=v["page_count"],
                copy_type=CopyType.ORIGINAL,
                is_notarized=notary_id is not None or v["notary_cost"] > 0,
                price=v["price"],
                translator_cost=v["translator_cost"],
                notary_cost=v["notary_cost"],
                translator_id=resolved(v["translator_id"], refs.translators),
                notary_id=notary_id,
            )
        )
        db.add(
            OrderStatusEvent(
                order_id=order.id,
                status=INITIAL_STATUS,
                changed_at=datetime.combine(v["order_date"], time(), tzinfo=UTC),
                changed_by_user_id=session.user_id,
            )
        )
        if v["status"] and v["status"] != INITIAL_STATUS:
            db.add(
                OrderStatusEvent(
                    order_id=order.id,
                    status=v["status"],
                    changed_at=now,
                    changed_by_user_id=session.user_id,
                    note="Imported",
                )
            )
        if v["paid"]:
            payment = ClientPayment(
                client_id=order.client_id,
                amount=v["paid"],
                payment_date=v["order_date"],
                method=PaymentMethod.OTHER,
                notes="Imported with the order",
                recorded_by_user_id=session.user_id,
            )
            db.add(payment)
            payments.append((payment, order, v["paid"]))
    await db.flush()

    for payment, order, amount in payments:
        db.add(
            ClientPaymentAllocation(
                payment_id=payment.id, order_id=order.id, amount_allocated=amount
            )
        )
        created.append(("payment", payment.id))
    await db.flush()
    return created


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


async def _check_orders_undoable(db: Any, by_type: dict[str, list[int]]) -> None:
    """Refuse, with a sentence, when anything has built on what an orders import made.

    "Built on it" is a payment of someone else's allocated to one of the orders,
    money paid out to a translator or a notary, a file, or a comment.
    """
    order_ids = by_type.get("order", [])
    payment_ids = by_type.get("payment", [])

    others = await db.scalar(
        select(func.count()).where(
            ClientPaymentAllocation.order_id.in_(order_ids),
            ClientPaymentAllocation.payment_id.not_in(payment_ids or [-1]),
        )
    )
    paid_out = await db.scalar(
        select(func.count()).where(TranslatorPaymentAllocation.order_id.in_(order_ids))
    )
    document_ids = (
        (await db.execute(select(OrderDocument.id).where(OrderDocument.order_id.in_(order_ids))))
        .scalars()
        .all()
    )
    files = await db.scalar(
        select(func.count()).where(
            OrderFile.order_document_id.in_(document_ids), OrderFile.deleted_at.is_(None)
        )
    )
    notary_paid = await db.scalar(
        select(func.count()).where(NotaryPaymentAllocation.order_document_id.in_(document_ids))
    )
    comments = await db.scalar(select(func.count()).where(OrderComment.order_id.in_(order_ids)))
    if others or paid_out or notary_paid or files or comments:
        raise ConflictError(
            "Some of the imported orders already have other payments, a translator or notary "
            "payout, files or comments, so this import can no longer be undone."
        )


async def _delete_orders(db: Any, by_type: dict[str, list[int]]) -> None:
    """Remove the orders an import made, with what hangs from them.

    Clients the import created stay when they have gained other orders or payments
    since.
    """
    order_ids = by_type.get("order", [])
    payment_ids = by_type.get("payment", [])
    client_ids = by_type.get("client", [])

    await db.execute(
        delete(ClientPaymentAllocation).where(ClientPaymentAllocation.payment_id.in_(payment_ids))
    )
    await db.execute(delete(ClientPayment).where(ClientPayment.id.in_(payment_ids)))
    await db.execute(delete(OrderStatusEvent).where(OrderStatusEvent.order_id.in_(order_ids)))
    await db.execute(delete(OrderDocument).where(OrderDocument.order_id.in_(order_ids)))
    await db.execute(delete(Order).where(Order.id.in_(order_ids)))

    still_used = select(Order.client_id).union(select(ClientPayment.client_id))
    await db.execute(delete(Client).where(Client.id.in_(client_ids), Client.id.not_in(still_used)))


async def _undo_orders(db: Any, by_type: dict[str, list[int]]) -> None:
    await _check_orders_undoable(db, by_type)
    await _delete_orders(db, by_type)


async def _undo_combined(db: Any, by_type: dict[str, list[int]]) -> None:
    """Orders, then the translators and notaries, then the clients the import made.

    Every check is made before the first delete. A translator or notary stays (and
    the undo is refused) when an order that is not part of this import uses them.
    """
    order_ids = by_type.get("order", [])
    translator_ids = by_type.get("translator", [])
    notary_ids = by_type.get("notary", [])

    await _check_orders_undoable(db, by_type)
    translators_elsewhere = await db.scalar(
        select(func.count(func.distinct(OrderDocument.translator_id))).where(
            OrderDocument.translator_id.in_(translator_ids),
            OrderDocument.order_id.not_in(order_ids),
        )
    )
    translators_paid = await db.scalar(
        select(func.count(func.distinct(TranslatorPayment.translator_id))).where(
            TranslatorPayment.translator_id.in_(translator_ids)
        )
    )
    notaries_elsewhere = await db.scalar(
        select(func.count(func.distinct(OrderDocument.notary_id))).where(
            OrderDocument.notary_id.in_(notary_ids),
            OrderDocument.order_id.not_in(order_ids),
        )
    )
    in_use = max(translators_elsewhere or 0, translators_paid or 0, notaries_elsewhere or 0)
    if in_use:
        raise ConflictError(
            f"{in_use} of the imported translators or notaries are already on other orders or "
            "have payments, so this import can no longer be undone."
        )

    await _delete_orders(db, by_type)
    await db.execute(
        delete(TranslatorLanguagePair).where(
            TranslatorLanguagePair.translator_id.in_(translator_ids)
        )
    )
    await db.execute(delete(Translator).where(Translator.id.in_(translator_ids)))
    await db.execute(delete(Notary).where(Notary.id.in_(notary_ids)))


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
    by_type: dict[str, list[int]] = {}
    for item in items:
        by_type.setdefault(item.entity_type, []).append(item.entity_id)

    if batch.kind == "orders":
        await _undo_orders(db, by_type)
    elif batch.kind == "combined":
        await _undo_combined(db, by_type)
    elif batch.kind == "clients":
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
    elif batch.kind == "notaries":
        on_documents = await db.scalar(
            select(func.count(func.distinct(OrderDocument.notary_id))).where(
                OrderDocument.notary_id.in_(ids)
            )
        )
        if on_documents:
            raise ConflictError(
                f"{on_documents} of the imported notaries are already on orders, so this "
                "import can no longer be undone."
            )
        await db.execute(delete(Notary).where(Notary.id.in_(ids)))
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
